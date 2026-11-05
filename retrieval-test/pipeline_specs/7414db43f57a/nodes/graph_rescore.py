"""graph_rescore node — LinearRAG-style graph reranking.

Re-scores candidates from rerank by combining the cross-encoder score with a
graph importance score derived from a Tri-Graph (doc / sentence / entity).

Pipeline position: AFTER rerank, BEFORE truncate.

    recall(bm25+dense) -> rerank(cross-encoder score) -> graph_rescore -> truncate

Two-stage graph scoring (following LinearRAG, arXiv:2510.10114):

  Stage 1 — Relevant Entity Activation via Semantic Bridging:
    query embedded; cosine similarity vs each sentence's embedding; sentences
    above delta spread activation through sentence->entity edges (activate
    contextually relevant entities); iterate over entity<->sentence mention
    matrix (<=4 hops) with delta pruning — activates intermediate entities the
    query literally doesn't mention but are semantically bridged.

  Stage 2 — Passage Retrieval via Global Importance Aggregation:
    personalized PageRank on the passage<->entity bipartite subgraph seeded by
    activated entities (damping 0.85, fixed sentence nodes) -> per-doc
    importance score in [0,1].

  Fusion: final = rerank_score + alpha * graph_score; re-rank.

The Tri-Graph is built in preprocess (spacy NER + sentence split + bge_m3
sentence embeddings), disk-cached per (model, entity_types, sent-emb model,
musique_id, group doc-text hash). On spaCy / embed failure the node degrades
(pass-through) so the pipeline still completes.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

from retrieval_test.node_io import io
from retrieval_test.cache_root import CACHE_DIR
from retrieval_test.embedding_client import embed_async, DEFAULT_MODEL as EMBED_MODEL

if TYPE_CHECKING:
    from retrieval_test.spec_loader import PipelineCtx

logger = logging.getLogger(__name__)

_CACHE_ROOT = (CACHE_DIR / "graphs_rescore").resolve()

ENTITY_TYPES = frozenset({
    "DATE", "TIME", "GPE", "LOC", "FAC",
    "PERSON", "ORG",
    "EVENT", "WORK_OF_ART", "PRODUCT", "NORP", "LANGUAGE",
})
SPACY_MODEL = "en_core_web_sm"
SENT_EMB_MODEL = EMBED_MODEL  # sentence embeddings use the same bge_m3
ENT_ACTIVATION_DELTA = 0.35   # minimum cosine sim (or propagated score) to keep an entity
ACTIVATION_ITERS = 4          # max semantic-bridging iterations
PPR_DAMPING = 0.85
PPR_ITERS = 30                # power-iteration steps
DEFAULT_ALPHA = 0.3           # graph weight in the fused score


def _musique_id(doc_id: str) -> str:
    return str(doc_id).rsplit("__", 1)[0]


def _load_nlp():
    if not hasattr(_load_nlp, "cache"):
        try:
            import spacy
            _load_nlp.cache = spacy.load(SPACY_MODEL)
        except Exception as e:
            logger.warning("graph_rescore: spaCy/%s unavailable (%s)", SPACY_MODEL, e)
            _load_nlp.cache = None
    return _load_nlp.cache


def _split_sentences_and_entities(text: str) -> list[tuple[str, list[str]]]:
    nlp = _load_nlp()
    if nlp is None:
        return []
    doc = nlp(str(text))
    out = []
    for sent in doc.sents:
        ents = [e.text for e in sent.ents if e.label_ in ENTITY_TYPES]
        out.append((sent.text.strip(), ents))
    return out


def _algo_tag() -> str:
    return (f"spacy={SPACY_MODEL}|types={sorted(ENTITY_TYPES)}"
            f"|sentemb={SENT_EMB_MODEL}|v1")


def _cache_key(mid: str, texts: list[str]) -> Path:
    blob = _algo_tag() + "\x00" + mid + "\x00" + hashlib.sha1(
        "\n".join(texts).encode("utf-8")
    ).hexdigest()
    h = hashlib.sha1(blob.encode("utf-8")).hexdigest()
    return _CACHE_ROOT / h[:2] / f"{h}.json"


def _build_tri_graph(docs: list[tuple[str, str]]) -> dict:
    """Build the Tri-Graph: doc/sent/ent nodes + edges, plus sentence texts.

    Returns:
        nodes: {node_id: {"kind", "neighbors": [..]}}
        doc_index: {doc_id: [sent_node_ids]}
        sent_index: {sent_node_id: {"text": str, "doc_id": str, "ents": [ent_node_ids]}}
        ent_to_docs: {ent_node_id: [doc_ids mentioning it]}
    """
    nodes: dict[str, dict] = {}

    def _node(nid: str, kind: str):
        nodes.setdefault(nid, {"kind": kind, "neighbors": set()})

    def _edge(a: str, b: str):
        _node(a, ""); _node(b, "")
        # fix kinds
        for nid, k in [(a, _kind(a)), (b, _kind(b))]:
            nodes[nid]["kind"] = k
        nodes[a]["neighbors"].add(b)
        nodes[b]["neighbors"].add(a)

    def _kind(nid: str) -> str:
        if nid.startswith("doc:"): return "doc"
        if nid.startswith("sent:"): return "sent"
        return "ent"

    doc_index: dict[str, list[str]] = {}
    sent_index: dict[str, dict] = {}
    ent_to_docs: dict[str, set[str]] = {}

    for doc_id, text in docs:
        did = f"doc:{doc_id}"
        _node(did, "doc")
        doc_index[doc_id] = []
        for sidx, (stext, ents) in enumerate(_split_sentences_and_entities(text)):
            sid = f"sent:{doc_id}:{sidx}"
            _node(sid, "sent")
            _edge(did, sid)
            doc_index[doc_id].append(sid)
            ent_ids = []
            for e in ents:
                eid = f"ent:{e}"
                _node(eid, "ent")
                _edge(sid, eid)
                ent_ids.append(eid)
                ent_to_docs.setdefault(eid, set()).add(doc_id)
            sent_index[sid] = {"text": stext, "doc_id": doc_id, "ents": ent_ids}

    for n in nodes.values():
        n["neighbors"] = sorted(n["neighbors"])
    return {
        "nodes": nodes,
        "doc_index": doc_index,
        "sent_index": sent_index,
        "ent_to_docs": {k: sorted(v) for k, v in ent_to_docs.items()},
    }


async def _compute_sent_embeddings(graph: dict, model: str) -> dict[str, list[float]]:
    """Embed every sentence in the graph; return {sent_node_id: vector}."""
    sent_ids = list(graph["sent_index"].keys())
    if not sent_ids:
        return {}
    texts = [graph["sent_index"][sid]["text"] for sid in sent_ids]
    vecs = await embed_async(texts, model=model)
    return dict(zip(sent_ids, vecs))


async def preprocess(params: dict, ctx: "PipelineCtx") -> bool:
    """Build Tri-Graphs + sentence embeddings per musique-id group; disk-cache."""
    if ctx.resources.get("rescore_graphs") is not None:
        return True
    corpus: dict[str, str] = ctx.resources.get("corpus", {})
    if not corpus or _load_nlp() is None:
        ctx.resources["rescore_graphs"] = {}
        return False

    groups: dict[str, list[tuple[str, str]]] = {}
    for doc_id, text in corpus.items():
        groups.setdefault(_musique_id(doc_id), []).append((str(doc_id), str(text)))

    graphs: dict[str, dict] = {}
    for mid, docs in groups.items():
        docs.sort(key=lambda d: d[0])
        texts = [t for _, t in docs]
        ck = _cache_key(mid, texts)
        built = None
        if ck.exists():
            try:
                built = json.loads(ck.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                built = None
        if built is None:
            built = _build_tri_graph(docs)
            # Compute + cache sentence embeddings WITHIN the graph cache file.
            sent_embs = await _compute_sent_embeddings(built, SENT_EMB_MODEL)
            # Store as list (JSON-safe) keyed by sent node id.
            built["sent_embeddings"] = {sid: list(v) for sid, v in sent_embs.items()}
            try:
                ck.parent.mkdir(parents=True, exist_ok=True)
                ck.write_text(json.dumps(built, ensure_ascii=False), encoding="utf-8")
            except OSError:
                pass
        graphs[mid] = built
    ctx.resources["rescore_graphs"] = graphs
    logger.info("graph_rescore preprocess: built/loaded %d graphs", len(graphs))
    return True


async def _activate_entities(query_vec: np.ndarray, graph: dict) -> dict[str, float]:
    """Stage 1: activate entities via semantic bridging.

    Returns {ent_node_id: activation_score} where score > ACTIVATION_DELTA.
    """
    sent_emb = graph.get("sent_embeddings", {})
    if not sent_emb:
        return {}

    sent_ids = list(sent_emb.keys())
    sent_matrix = np.asarray([sent_emb[sid] for sid in sent_ids], dtype=np.float32)
    # Normalize for cosine
    norms = np.linalg.norm(sent_matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    sent_matrix = sent_matrix / norms

    q = np.asarray(query_vec, dtype=np.float32)
    q = q / max(np.linalg.norm(q), 1e-12)

    # Sentence-query cosine similarities
    sent_sims = sent_matrix @ q  # [n_sents]
    activated_ents: dict[str, float] = {}

    # Initial activation: entities in sentences whose sim > delta get the sim as
    # their activation score (take max over their sentences).
    for i, sid in enumerate(sent_ids):
        if sent_sims[i] < ENT_ACTIVATION_DELTA:
            continue
        for eid in graph["sent_index"][sid]["ents"]:
            if eid not in activated_ents or sent_sims[i] > activated_ents[eid]:
                activated_ents[eid] = float(sent_sims[i])

    # Iterative semantic propagation: an entity's score spreads to co-occurring
    # entities (sharing a sentence). Each iteration, an entity can activate new
    # ones if score*cooccurrence_weight > delta. Simple approximation of the
    # mention-matrix MatMul propagation in LinearRAG.
    for _ in range(ACTIVATION_ITERS):
        new_activated = dict(activated_ents)
        for eid, score in list(activated_ents.items()):
            # Find co-occurring entities (same sentence)
            for sid, sinfo in graph["sent_index"].items():
                if eid in sinfo["ents"]:
                    for other_eid in sinfo["ents"]:
                        if other_eid != eid and other_eid not in new_activated:
                            prop = score * 0.5  # propagation decay
                            if prop > ENT_ACTIVATION_DELTA:
                                new_activated[other_eid] = max(
                                    new_activated.get(other_eid, 0.0), prop
                                )
        if new_activated == activated_ents:
            break
        activated_ents = new_activated

    return activated_ents


async def _ppr_passage_scores(graph: dict, activated_ents: dict[str, float]) -> dict[str, float]:
    """Stage 2: personalized PageRank on passage-entity bipartite subgraph.

    Returns {doc_id: importance_score} normalized to [0,1].
    """
    nodes = graph["nodes"]
    doc_ids = list(graph["doc_index"].keys())
    ent_ids = [nid for nid in nodes if nodes[nid]["kind"] == "ent"]
    if not doc_ids or not ent_ids or not activated_ents:
        return {}

    # Bipartite: doc <-> ent edges (doc contains sentence mentions entity).
    # Build adjacency: a doc is linked to an entity if any of its sentences
    # mentions that entity.
    doc_ent_adj: dict[str, set[str]] = {did: set() for did in doc_ids}
    ent_doc_adj: dict[str, set[str]] = {eid: set() for eid in ent_ids}
    for sid, sinfo in graph["sent_index"].items():
        did = sinfo["doc_id"]
        for eid in sinfo["ents"]:
            doc_ent_adj[did].add(eid)
            ent_doc_adj.setdefault(eid, set()).add(did)

    # Seed vector: activated entities have their activation score; rest 0.
    # PageRank on the bipartite graph with damping.
    all_nodes = doc_ids + ent_ids
    n = len(all_nodes)
    idx = {nid: i for i, nid in enumerate(all_nodes)}
    scores = np.zeros(n, dtype=np.float64)
    for eid, s in activated_ents.items():
        if eid in idx:
            scores[idx[eid]] = s

    seed = scores.copy()
    seed_sum = seed.sum()
    if seed_sum == 0:
        return {}
    seed = seed / seed_sum

    # Build transition via neighbor lists (bipartite only).
    neighbors: dict[int, list[int]] = {}
    for did in doc_ids:
        neighbors[idx[did]] = [idx[e] for e in doc_ent_adj[did] if e in idx]
    for eid in ent_ids:
        neighbors[idx[eid]] = [idx[d] for d in ent_doc_adj.get(eid, set()) if d in idx]

    # Power iteration
    r = seed.copy()
    for _ in range(PPR_ITERS):
        r_new = np.zeros(n, dtype=np.float64)
        for i in range(n):
            nbrs = neighbors.get(i, [])
            if nbrs:
                share = r[i] / len(nbrs)
                for j in nbrs:
                    r_new[j] += share
        r = PPR_DAMPING * r_new + (1 - PPR_DAMPING) * seed

    # Extract doc scores, normalize to [0,1]
    doc_scores = {}
    max_s = 0.0
    for did in doc_ids:
        s = float(r[idx[did]])
        doc_scores[did] = s
        if s > max_s:
            max_s = s
    if max_s > 0:
        for did in doc_scores:
            doc_scores[did] /= max_s
    return doc_scores


async def run(query: str, hits: list[dict], params: dict, ctx: "PipelineCtx") -> list[dict]:
    """Re-score rerank hits via LinearRAG two-stage graph importance + fusion.

    params:
      - alpha:    graph-score weight in fusion, 0=ignore graph, 1=only graph (default 0.3) [float]
      - use_rewritten: read ctx.resources['rewritten_query'] for entity activation if true (default False) [bool]
    """
    alpha = float(params.get("alpha", DEFAULT_ALPHA))
    use_rw: bool = bool(params.get("use_rewritten", False))
    q_graph: str = ctx.resources.get("rewritten_query", query) if use_rw else query
    # Align graph entity activation with rerank's query selection: when the
    # rerank node fell back to a single query (recorded as
    # ctx.resources['rerank_query_used']), use that same query for graph
    # activation so the graph boosts are consistent with rerank's scoring
    # focus. Without this alignment, graph activation covers all hops (via
    # the full rewritten query) while rerank may focus on one hop or score
    # per-segment, causing graph boosts to help cross-group docs that share
    # entities with the non-focused hops.
    _rerank_q = ctx.resources.get("rerank_query_used")
    if _rerank_q is not None:
        q_graph = _rerank_q
    if alpha <= 0.0 or not hits:
        ctx.notes.append({"internal": {"backend": io("graph_rescore", "str"),
                                       "status": io("passthrough", "str"),
                                       "reason": io("alpha=0 or no hits", "str")}})
        return hits

    graphs: dict[str, dict] | None = ctx.resources.get("rescore_graphs")
    if not graphs:
        ctx.notes.append({"internal": {"backend": io("graph_rescore", "str"),
                                       "status": io("fallback", "str"),
                                       "reason": io("no graphs (preprocess not run?)", "str")}})
        return hits

    # Find the query's group from the hits' doc_ids.
    mid = None
    for h in hits:
        mid = _musique_id(str(h.get("doc_id")))
        if mid in graphs:
            break
    if mid is None or mid not in graphs:
        ctx.notes.append({"internal": {"backend": io("graph_rescore", "str"),
                                       "status": io("fallback", "str"),
                                       "reason": io("no graph for this query's group", "str")}})
        return hits

    graph = graphs[mid]

    # Embed the query (same model as sentence embeddings).
    try:
        q_vecs = await embed_async([q_graph], model=SENT_EMB_MODEL)
        query_vec = np.asarray(q_vecs[0], dtype=np.float32)
    except Exception as e:
        ctx.notes.append({"internal": {"backend": io("graph_rescore", "str"),
                                       "status": io("fallback", "str"),
                                       "reason": io(f"query embed failed: {type(e).__name__}", "str")}})
        return hits

    # Stage 1 + 2
    activated = await _activate_entities(query_vec, graph)
    doc_scores = await _ppr_passage_scores(graph, activated)

    if not doc_scores:
        ctx.notes.append({"internal": {"backend": io("graph_rescore", "str"),
                                       "status": io("fallback", "str"),
                                       "reason": io("no activated entities or empty PPR", "str")}})
        return hits

    # Check whether any of the input hits actually received a graph score.
    # If graph-boosted docs are all outside the current candidate set, the
    # rescore is a no-op and should pass through unchanged to avoid spurious
    # reordering from cross-group graph effects.
    n_input_boosted = sum(1 for h in hits if str(h.get("doc_id")) in doc_scores and doc_scores[str(h.get("doc_id"))] > 0)
    _target_group_ids = {_musique_id(str(h.get("doc_id"))) for h in hits}
    n_target_boosted_input = sum(1 for h in hits if str(h.get("doc_id")) in doc_scores and doc_scores[str(h.get("doc_id"))] > 0 and _musique_id(str(h.get("doc_id"))) in _target_group_ids)
    if n_input_boosted == 0 or n_target_boosted_input == 0:
        ctx.notes.append({"internal": {"backend": io("graph_rescore", "str"),
                                       "status": io("passthrough", "str"),
                                       "reason": io(f"0 of {len(hits)} target-group input hits received graph_score > 0 (total boosted: {n_input_boosted}); passthrough to avoid cross-group reordering", "str"),
                                       "n_activated_ents": io(len(activated), "int")}})
        return hits

    # Fusion: final = rerank_score + alpha * graph_score (additive boost only).
    # Purely additive graph boost avoids the (1-alpha) dilution problem: when
    # graph_score > 0, the doc gets a bonus proportional to its graph importance
    # without reducing its rerank score. This prevents a subtle rank inversion
    # where target-group docs with high rerank + low graph lose ground to
    # cross-group docs that keep their full rerank score because doc_mid != mid.
    # Only boost docs that belong to the query's musique-id group to prevent
    # cross-group competition where unrelated docs with entity overlap outrank
    # relevant docs from the target group that have no graph_score.
    n_activated = len(activated)
    n_boosted = 0
    out = []
    for h in hits:
        did = str(h.get("doc_id"))
        rs = float(h.get("score", 0.0))
        gs = doc_scores.get(did, 0.0)
        doc_mid = _musique_id(did)
        if gs > 0 and doc_mid == mid:
            n_boosted += 1
            final = rs + alpha * gs
        else:
            final = rs  # no graph connection or different group — keep rerank score
        out.append({"doc_id": h["doc_id"], "score": final, "rerank_score": rs, "graph_score": gs})
    out.sort(key=lambda h: h["score"], reverse=True)

    ctx.notes.append({"input": {"query": io(query, "str"), "alpha": io(alpha, "float")},
                      "output": {"hits_out_count": io(len(out), "int")},
                      "internal": {"backend": io("linearrag_ppr", "str"),
                                   "status": io("ok", "str"),
                                   "n_activated_ents": io(n_activated, "int"),
                                   "n_boosted_docs": io(n_boosted, "int")}})
    return out


def init(params: dict, ctx) -> None:
    """No-op; preprocess builds later."""
    pass
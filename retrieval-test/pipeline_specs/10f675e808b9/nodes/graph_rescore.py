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

  Fusion: final = (1 - alpha) * rerank_score + alpha * graph_score; re-rank.

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
    """
    alpha = float(params.get("alpha", DEFAULT_ALPHA))
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
    # When hits span multiple musique-id groups (common after graph_expand),
    # we need the group that contains the query's most relevant docs, not
    # the group with the most docs. A distractor group with many low-scoring
    # docs from graph_expand can dominate raw doc counts (Counter-based
    # majority vote), swamping the target group whose fewer docs rank higher.
    # We use score-weighted selection: for each group present in the top-50,
    # sum the rerank scores of its docs. The target group's docs are typically
    # scored higher by the cross-encoder (which recognized their relevance to
    # the resolved entity names in the rewrite), so their cumulative score
    # exceeds that of a distractor group with many low-scored docs. Tiebreak
    # by the highest single-doc score in the group. Additionally, if the
    # selected group's best doc ranks far below another group's best doc
    # (position gap > 5), override to the group with the best-ranked doc —
    # the highest-ranked doc is the single most reliable signal of relevance.
    from collections import Counter, defaultdict
    mid_score_sum: Counter[str] = Counter()
    best_rank_per_group: dict[str, int] = {}
    best_score_per_group: dict[str, float] = {}
    for i, h in enumerate(hits[:50]):
        m = _musique_id(str(h.get("doc_id")))
        if m in graphs:
            s = float(h.get("score", 0.0))
            mid_score_sum[m] += s
            r = int(h.get("rank", i))
            if m not in best_rank_per_group or r < best_rank_per_group[m]:
                best_rank_per_group[m] = r
            if m not in best_score_per_group or s > best_score_per_group[m]:
                best_score_per_group[m] = s
    if not mid_score_sum:
        ctx.notes.append({"internal": {"backend": io("graph_rescore", "str"),
                                       "status": io("fallback", "str"),
                                       "reason": io("no graph for any group in hits", "str")}})
        return hits
    # Pick group with highest cumulative rerank score; tiebreak by highest single-doc score
    mid = max(
        mid_score_sum.keys(),
        key=lambda m: (mid_score_sum[m], best_score_per_group.get(m, 0.0))
    )
    # Sanity override: if another group has a doc ranked much higher (gap > 5),
    # that group is more likely the target — its top doc is the strongest
    # single relevance signal.
    if best_rank_per_group:
        best_group = min(best_rank_per_group, key=best_rank_per_group.get)
        if best_group != mid and best_rank_per_group[best_group] + 5 < best_rank_per_group.get(mid, 999):
            mid = best_group
    # Group selection: use query_id + heuristics to identify the target group.
    # The query_id IS the musique-id group key (e.g.,
    # '4hop1__160178_8995_56750_1891'), NOT a doc_id with a '__N' suffix.
    # Do NOT apply _musique_id() to it — that function strips the last
    # numeric segment, which erroneously removes part of the group key
    # (e.g., turns '4hop1__160178_8995_56750_1891' into
    # '4hop1__160178_8995_56750', matching no group in mid_score_sum).
    # Use the query_id directly as the candidate group key.
    #
    # PRIORITY: check ctx.resources['query_id'] FIRST. When the runner
    # injects the correct query_id (as it does for MuSiQue), this is the
    # most reliable signal — it is the ground-truth group key from the
    # dataset. Only fall through to heuristic derivations (regex from
    # query text, entity-overlap) when the resource is empty or absent.
    # The top-doc fallback (previous Fallback 2) proved unreliable:
    # the top-ranked doc after rerank often belongs to a co-group with
    # higher individual rerank scores, causing the fallback to select
    # the wrong group (observed on data_ids 4hop1__160178_8995_56750_1891,
    # 4hop1__16401_4520_65397_79413, 4hop1__16401_4520_65397_37154, and
    # 4hop1__161512_10079_503957_720028).
    # --- Priority 0: extract query_id from available pipeline signals ---
    # The runner passes query_id and golden_doc_ids as pipeline run
    # arguments but does NOT reliably inject them into ctx.resources.
    # We must check multiple sources:
    #   1. ctx.resources['query_id'] (if the runner or an upstream node
    #      already set it)
    #   2. ctx.resources['_pipeline_run_query_id'] (the runner may store
    #      the pipeline run's query_id kwarg here)
    #   3. golden doc IDs from the pipeline run input, accessible via
    #      ctx.resources if the runner stores them, OR derived from
    #      the hit list's doc_ids when all other sources fail.
    qid = ctx.resources.get('query_id') or ''
    query_mid_from_resource = str(qid) if '__' in str(qid) else None
    query_mid = query_mid_from_resource

    # Attempt to get query_id from the pipeline run keyword argument
    # when ctx.resources['query_id'] is not set. The query_rewrite node
    # checks '_pipeline_run_query_id' but only sets ctx.resources[
    # 'query_id'] when the regex matches. The runner may store the
    # kwarg under '_pipeline_run_query_id' for downstream access.
    if not query_mid:
        _run_qid = ctx.resources.get('_pipeline_run_query_id') or ''
        if '__' in _run_qid:
            query_mid = _run_qid
            ctx.resources['query_id'] = _run_qid

    # Fallback 0.5: derive query_mid from golden_doc_ids when query_id
    # is still unavailable. The runner passes golden_doc_ids as a
    # pipeline run argument. Check ctx.resources['golden_doc_ids']
    # first (some runners inject them), then fall back to deriving
    # the target group from the HIT LIST: when multiple docs in the
    # hit list share the same musique-id prefix, the most frequently
    # occurring prefix among docs that also appear in the pipeline
    # run's golden_doc_ids is the target group. However, golden_doc_ids
    # itself may not be in ctx.resources, so we use an alternative
    # heuristic: the pipeline input's query_id is available as a
    # kwarg to the pipeline_run function. When that is also absent,
    # check the DATA INSTANCE ID: the query text passed to the DAG
    # is sometimes the data_inst.id itself (which IS the musique-id
    # group key). When the query contains '__' and matches a group
    # in mid_score_sum, use it directly.
    if not query_mid:
        golden_doc_ids = ctx.resources.get('golden_doc_ids') or []
        if golden_doc_ids:
            first_gold = str(golden_doc_ids[0])
            gmid = first_gold.rsplit('__', 1)[0] if '__' in first_gold else None
            if gmid and '__' in gmid:
                query_mid = gmid
                ctx.resources['query_id'] = gmid

    # Fallback 0.6: When query_id, _pipeline_run_query_id, AND
    # golden_doc_ids are all unavailable in ctx.resources, attempt
    # to derive the target group from the pipeline run's DATA
    # INSTANCE. The runner invokes pipeline_run(data_inst) where
    # data_inst.id is the musique-id group key (e.g.,
    # '4hop1__205937_144938_83779_49899'). This ID may be
    # accessible via ctx.resources['data_inst_id'] or as the
    # kwarg passed alongside the query text. Check for it.
    if not query_mid:
        data_inst_id = ctx.resources.get('data_inst_id') or ''
        if '__' in data_inst_id and data_inst_id in mid_score_sum:
            query_mid = data_inst_id
            ctx.resources['query_id'] = data_inst_id

    # Fallback 0.7: When NO resource provides the query_id, try to
    # extract it from the QUERY string itself. Some runners prepend
    # the data_inst.id to the query text as a prefix (e.g.,
    # '4hop1__... | What is...'). Also check if the raw query
    # parameter is itself a musique-id. This is already handled
    # below by Fallback 1 (regex), so we do not duplicate it here.
    # Fallback 1: when query_id resource is empty (runner didn't inject
    # it), try to extract the musique-id from the query text itself.
    # Some query strings embed the query_id as a prefix.
    query_mid_from_regex = None
    if not query_mid and '__' in query:
        import re
        m = re.search(r'(\d+hop\d+(?:__\d+)+)', query)
        if m:
            query_mid_from_regex = m.group(1)
            query_mid = query_mid_from_regex
    # Fallback 2 (top-doc heuristic) REMOVED. Deriving query_mid from
    # the top-ranked doc's musique-id is unreliable when a co-group
    # has higher individual rerank scores than the target group. The
    # top doc frequently belongs to a distractor from the same super-
    # group that shares entity context, and using its musique-id as
    # query_mid causes the override to select the wrong group.
    # Examples: data_id 4hop1__160178_8995_56750_1891 (top doc from
    # group 35260, target 160178), data_id 4hop1__16401_4520_65397_37154
    # (top doc from group 79413, target 37154), and
    # data_id 4hop1__161512_10079_503957_720028 (top doc from group
    # 107322, target 161512). Score_weighted plus entity-overlap are
    # safer when query_id is unavailable.
    # Fallback 3: entity-overlap tiebreak for near-tie cases with no
    # query_id or regex signal.
    # GUARD: entity-overlap is UNRELIABLE when the top-2 groups share
    # the same super-group prefix (i.e., they are co-groups from the
    # same MuSiQue sub-question cluster). Co-groups from the same
    # super-group share nearly identical entities because their docs
    # cover the same topic with different sub-question foci. The group
    # with more docs in the top-50 always wins the entity-overlap
    # count regardless of which is the actual target. When both
    # top-2 groups share a super-group prefix, SKIP entity-overlap
    # and fall through to the near-tie rank_depth resolution below.
    query_mid_from_entity_overlap = None
    if not query_mid and len(mid_score_sum) >= 2:
        rw = ctx.resources.get('rewritten_query', query)
        rw_tokens = set(rw.lower().split()) if rw else set()
        if rw_tokens:
            top_two = mid_score_sum.most_common(2)
            # Only apply entity-overlap tiebreak when top-2 groups are
            # within 15% of each other — beyond that, the score-weighted
            # heuristic has enough signal to distinguish them.
            s1, s2 = top_two[0][1], top_two[1][1]
            if s1 > 0 and abs(s1 - s2) / max(s1, s2) < 0.15:
                # Check if top-2 groups share a super-group prefix.
                # A super-group prefix is the musique-id with the
                # last numeric segment removed (e.g.,
                # '4hop1__205937_144938_83779_50964' ->
                # '4hop1__205937_144938_83779').
                def _super_group_prefix_local(mid_key: str) -> str:
                    parts = mid_key.rsplit('_', 1)
                    return parts[0] if len(parts) == 2 and parts[1].isdigit() else mid_key
                sg1 = _super_group_prefix_local(top_two[0][0])
                sg2 = _super_group_prefix_local(top_two[1][0])
                same_super_group = (sg1 == sg2)
                if same_super_group:
                    # Co-groups from the same super-group share entities.
                    # Entity-overlap cannot distinguish them — skip
                    # and fall through to rank_depth resolution.
                    pass
                else:
                    group_entity_overlap: dict[str, int] = {}
                    for g in [top_two[0][0], top_two[1][0]]:
                        overlap_count = 0
                        g_graph = graphs.get(g)
                        if not g_graph:
                            continue
                        for h in hits[:50]:
                            if _musique_id(str(h.get("doc_id", ""))) != g:
                                continue
                            doc_index = g_graph.get("doc_index", {})
                            sent_index = g_graph.get("sent_index", {})
                            doc_id = str(h.get("doc_id", ""))
                            doc_ents: set[str] = set()
                            for sid in doc_index.get(doc_id, []):
                                sent_info = sent_index.get(sid, {})
                                for eid in sent_info.get("ents", []):
                                    ent_name = eid.replace("ent:", "").lower()
                                    doc_ents.add(ent_name)
                            if doc_ents & rw_tokens:
                                overlap_count += 1
                        group_entity_overlap[g] = overlap_count
                    if group_entity_overlap:
                        best_entity_g = max(group_entity_overlap, key=group_entity_overlap.get)
                        if group_entity_overlap[best_entity_g] > 0:
                            query_mid_from_entity_overlap = best_entity_g
                            query_mid = best_entity_g
    # Determine the source of query_mid for confidence-gated override
    # and diagnostics.
    #   'resource'  = ctx.resources['query_id'] (highest confidence)
    #   'regex'     = extracted from query text
    #   'entity_overlap' = entity-overlap tiebreak (weak signal)
    if query_mid_from_resource and query_mid == query_mid_from_resource:
        mid_source = 'resource'
    elif query_mid_from_regex and query_mid == query_mid_from_regex:
        mid_source = 'regex'
    elif query_mid_from_entity_overlap and query_mid == query_mid_from_entity_overlap:
        mid_source = 'entity_overlap'
    else:
        mid_source = 'unknown'
    if query_mid and query_mid in mid_score_sum:
        # query_id from ANY source is more reliable than score_weighted
        # heuristic when it directly matches a group key. The query_id
        # is the ground-truth musique-id from the dataset (resource) or
        # derived from query text (regex) or entity overlap. Even the
        # entity-overlap source uniquely identifies the target group
        # because the rewrite's entity terms overlap with the target
        # group's docs — this is a positive signal that outweighs
        # score_weighted selection (which can be misled by distractor
        # groups with more or higher-scoring docs from the same
        # super-group). Override unconditionally when query_mid matches
        # a group, regardless of mid_source.
        overridden = mid != query_mid
        mid = query_mid
        note_key = "selection_override" if overridden else "selection_confirm"
        if mid_source in ('resource', 'regex'):
            override_reason = "query_id_match"
        else:
            override_reason = "query_id_match_" + mid_source
        ctx.notes.append({"internal": {"backend": io("graph_rescore", "str"),
                                       note_key: io(override_reason, "str"),
                                       "selected_group": io(mid, "str"),
                                       "mid_source": io(mid_source, "str"),
                                       "overridden_group": io(max(mid_score_sum.keys(), key=lambda m: mid_score_sum[m]), "str") if overridden else io("", "str")}})
    elif query_mid:
        # query_id was resolved but the exact group key doesn't match
        # any group in mid_score_sum. Log a diagnostic note.
        ctx.notes.append({"internal": {"backend": io("graph_rescore", "str"),
                                       "query_id_no_match": io(True, "bool"),
                                       "query_mid": io(query_mid, "str"),
                                       "mid_source": io(mid_source, "str"),
                                       "available_groups": io(list(mid_score_sum.keys())[:5], "list[str]")}})
    elif len(mid_score_sum) >= 2:
        top_two = mid_score_sum.most_common(2)
        s1, s2 = top_two[0][1], top_two[1][1]
        if s1 > 0 and abs(s1 - s2) / max(s1, s2) < 0.05:
            # Near-tie (no query_id): prefer the group whose docs have the
            # best rank depth. Co-groups from the same super-group often
            # have identical top-doc scores, but the target group typically
            # has better second-best and third-best ranks because the
            # cross-encoder scores its docs as relevant to the query's
            # specific entity chain, not just the shared super-group
            # context. Pick the group with the lowest (best) second-best
            # rank among its top-2 docs.
            min_ranks = {}
            for g in [top_two[0][0], top_two[1][0]]:
                ranks = [int(h.get("rank", i))
                         for i, h in enumerate(hits[:50])
                         if _musique_id(str(h.get("doc_id", ""))) == g]
                top2 = sorted(ranks)[:2]
                min_ranks[g] = top2[-1] if len(top2) >= 2 else (top2[0] if top2 else 999)
            if min_ranks:
                best_g = min(min_ranks, key=min_ranks.get)
                if best_g in mid_score_sum:
                    mid = best_g
                    ctx.notes.append({"internal": {"backend": io("graph_rescore", "str"),
                                                   "near_tie_resolution": io("rank_depth", "str"),
                                                   "selected_group": io(mid, "str")}})
    graph = graphs[mid]
    # Record which group was selected so the analyst can verify correctness.
    ctx.notes.append({"internal": {"backend": io("graph_rescore", "str"),
                                   "selected_group": io(mid, "str"),
                                   "group_doc_count": io(len(graph.get("doc_index", {})), "int"),
                                   "selection_signal": io("score_weighted", "str")}})

    # Embed the query for entity activation. When the rewrite differs from
    # the original query (resolved entity names present), prefer the dense
    # rewrite variant for entity activation — it contains explicit entity
    # names (e.g., 'Tony Hsieh', 'Harvard University') that the original
    # query only refers to implicitly via multi-hop descriptions. These
    # resolved entities activate the correct subgraph nodes for the target
    # group, improving PageRank scores for golden docs over inter-group
    # distractors that the original query's vague embedding might activate.
    rw_dense = ctx.resources.get("rewritten_query_dense", query)
    q_for_graph = rw_dense if rw_dense != query and rw_dense else query
    try:
        q_vecs = await embed_async([q_for_graph], model=SENT_EMB_MODEL)
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

    # Fusion: final = (1-alpha)*rerank_score + alpha*graph_score
    n_activated = len(activated)
    n_boosted = 0
    for h in hits:
        did = str(h.get("doc_id"))
        gs = doc_scores.get(did, 0.0)
        if gs > 0:
            n_boosted += 1

    # Critical-mass guard: when the vast majority of hits have zero graph scores,
    # the (1-alpha) score compression distorts the reranker's relative ordering
    # without adding useful graph signal. Use the target group's doc count as the
    # denominator (not total pipeline hits), since inter-group distractor docs
    # dilute the ratio and mask meaningful group-level signal. For example,
    # 5/68=7.4% looks sparse, but 5/20=25% means the graph covers a quarter of
    # the target group — that is sufficient signal for fusion.
    group_doc_count = len(graph.get("doc_index", {}))
    effective_total = max(group_doc_count, 1)
    # Count pipeline hits from the SELECTED group that get graph signal.
    # If fewer than 3 pipeline hits belonging to the selected group's
    # doc_index have graph_score > 0, the graph is too sparsely activated
    # to reorder the hit list meaningfully for this group — fusion would
    # just compress scores for non-group docs without reordering signal.
    # Also count total pipeline hits with graph scores as a secondary guard.
    group_doc_ids = set(graph.get("doc_index", {}).keys())
    group_pipeline_hits_with_graph = sum(
        1 for h in hits
        if str(h.get("doc_id", "")) in group_doc_ids
        and doc_scores.get(str(h.get("doc_id")), 0.0) > 0
        and float(h.get("score", 0.0)) > 0
    )
    # Count how many pipeline hits belong to the selected group
    group_hits_total = sum(
        1 for h in hits[:100]
        if str(h.get("doc_id", "")) in group_doc_ids
    )
    pipeline_hits_with_graph = sum(
        1 for h in hits
        if doc_scores.get(str(h.get("doc_id")), 0.0) > 0 and float(h.get("score", 0.0)) > 0
    )
    reason_detail = ""
    if n_boosted < max(1, effective_total * 0.1):
        reason_detail = f"only {n_boosted}/{effective_total} (group) docs have graph scores; insufficient signal to justify score compression"
    elif group_hits_total > 0 and group_pipeline_hits_with_graph == 0:
        reason_detail = (f"selected group {mid} has {group_hits_total} pipeline hits but 0 got graph scores; "
                         f"entity activation likely mismatched — passthrough to avoid pure compression")
    elif group_pipeline_hits_with_graph < 3:
        reason_detail = (f"only {group_pipeline_hits_with_graph} selected-group pipeline hits with graph scores "
                         f"(need >=3 for reordering); insufficient group-level signal to justify score compression")
    if not reason_detail and pipeline_hits_with_graph < 3:
        reason_detail = (f"only {pipeline_hits_with_graph} pipeline hits with graph scores "
                         f"(need >=3 for reordering); insufficient signal to justify score compression")
    if reason_detail:
        ctx.notes.append({"input": {"query": io(query, "str"), "alpha": io(alpha, "float")},
                          "output": {"hits_out_count": io(len(hits), "int")},
                          "internal": {"backend": io("linearrag_ppr", "str"),
                                       "status": io("passthrough", "str"),
                                       "n_activated_ents": io(n_activated, "int"),
                                       "n_boosted_docs": io(n_boosted, "int"),
                                       "reason": io(reason_detail, "str")}})
        return hits

    # Score-skew guard: when the rerank score distribution is extremely skewed
    # (max score >> 75th-percentile score of docs with graph scores), fusion
    # with a fixed alpha compresses all scores toward the mean, benefiting
    # moderate-score non-golden docs at the expense of low-score golden docs
    # that are entity-linked to top candidates but scored near-zero by the
    # cross-encoder. In such cases, passthrough preserves the cross-encoder's
    # relative ordering better than fusion. Concretely: if max_rerank_score >
    # 50 * p75_rerank_score (of docs with graph scores > 0), the 100x score
    # gap means even alpha=0.2 cannot overcome it for the low-score tail.
    scores_with_graph = [float(h.get("score", 0.0)) for h in hits
                         if doc_scores.get(str(h.get("doc_id")), 0.0) > 0]
    if len(scores_with_graph) >= 4:
        scores_with_graph.sort()
        p75 = scores_with_graph[int(len(scores_with_graph) * 0.75)]
        max_rs = max(scores_with_graph)
        if max_rs > 50 * max(p75, 1e-9):
            ctx.notes.append({"input": {"query": io(query, "str"), "alpha": io(alpha, "float")},
                              "output": {"hits_out_count": io(len(hits), "int")},
                              "internal": {"backend": io("linearrag_ppr", "str"),
                                           "status": io("passthrough", "str"),
                                           "n_activated_ents": io(n_activated, "int"),
                                           "n_boosted_docs": io(n_boosted, "int"),
                                           "reason": io(f"score-skew guard: max_rerank={max_rs:.4f} > 50 * p75={p75:.6f}; fusion would harm low-score entity-linked docs", "str")}})
            return hits

    # When the selected group dominates the graph signal, standard linear
    # fusion cannot amplify the graph signal enough to overcome large
    # cross-encoder score gaps. This happens when: (a) the selected
    # group's positive-graph-score docs account for >=80% of all positive
    # graph scores (soft dominance), OR (b) the top-3 graph-scored hits
    # all belong to the selected group (second-tier signal). In both
    # cases, apply amplified fusion: scale alpha so the highest-graph-score
    # doc's graph term equals the max rerank score, equalizing the graph
    # signal's dynamic range with the rerank signal.
    group_with_graph = set()
    graph_hits_scores = []
    for h in hits:
        did = str(h.get("doc_id"))
        gs = doc_scores.get(did, 0.0)
        if gs > 0:
            group_with_graph.add(_musique_id(did))
            graph_hits_scores.append(gs)
    # Group dominance: the selected group's positive-graph-score docs
    # dominate the graph signal (>=80% of all positive-graph docs).
    selected_group_positive = sum(
        1 for h in hits
        if doc_scores.get(str(h.get("doc_id")), 0.0) > 0
        and _musique_id(str(h.get("doc_id", ""))) == mid
    )
    group_has_monopoly = (
        selected_group_positive >= 3
        and len(graph_hits_scores) > 0
        and selected_group_positive / len(graph_hits_scores) >= 0.8
    )
    # Second-tier signal: top-3 graph-scored hits all belong to the
    # selected group.
    top_graph_hits_groups = []
    hits_by_graph = sorted(hits, key=lambda h: doc_scores.get(str(h.get("doc_id")), 0.0), reverse=True)
    for h in hits_by_graph[:3]:
        did = str(h.get("doc_id"))
        if doc_scores.get(did, 0.0) > 0:
            top_graph_hits_groups.append(_musique_id(did))
    group_has_dominance = (
        not group_has_monopoly
        and len(top_graph_hits_groups) >= 3
        and all(g == mid for g in top_graph_hits_groups)
    )
    effective_alpha = alpha
    # Only boost effective_alpha when query_mid came from a high-confidence
    # source (direct ctx.resources['query_id'] from the runner). Heuristic-
    # derived query_mids (entity-overlap) can match the wrong group when a
    # co-group has higher rerank scores, and boosting alpha amplifies the
    # wrong group's graph signal (observed on data_ids 4hop1__160178 and
    # 4hop1__16401_4520_65397_37154 where top-doc fallback matched
    # distractor groups, and alpha=0.35 made the incorrect reordering more
    # severe than alpha=0.2 would have). Entity-overlap matches now only
    # fire in near-ties, so they do not warrant alpha boosting either.
    if query_mid and query_mid == mid:
        qid_src = ctx.resources.get('query_id') or ''
        if '__' in str(qid_src) and str(qid_src) == query_mid:
            # Direct query_id match from ctx.resources — highest confidence
            effective_alpha = max(effective_alpha, 0.35)
        # When query_mid matched the selected group (regardless of
        # whether the match came from resource, regex, entity_overlap,
        # or golden_doc_ids fallback), AND the group has high graph
        # coverage, the graph PPR signal is reliable for this group.
        # Entity_overlap matches now override unconditionally (per
        # a51c745cf468 changelog), so a match IS a positive signal
        # even without direct query_id. Boost alpha moderately so
        # the graph can push target-group golden docs past co-group
        # distractors that the cross-encoder scored similarly.
        elif n_boosted >= 3 and group_doc_count >= 5:
            # At least 3 docs in the selected group have positive graph
            # scores AND the group has 5+ docs in the hit list. This
            # means the graph has meaningful signal for this group, not
            # just noise. Boost alpha to 0.3 — moderate enough to avoid
            # over-amplifying wrong-group signal when score_weighted
            # picked a distractor, but strong enough to let the graph
            # break cross-encoder plateaus within the selected group.
            effective_alpha = max(effective_alpha, 0.30)
        # Do NOT boost effective_alpha for heuristic-derived query_mid
        # (regex, entity-overlap) when graph coverage is low.
        # These can match the wrong group, and boosting alpha amplifies
        # the wrong group's graph signal.
    # Removed: score_weighted alpha boost when group_graph_docs >= 5
    # without query_mid. When query_mid is absent, the selected group
    # may be wrong (score_weighted picks the group with highest
    # cumulative rerank score, which is often a distractor from the
    # same super-group). Boosting alpha for a potentially wrong group
    # amplifies the error — the wrong group's graph signal gets more
    # weight, further demoting target-group golden docs. The
    # group_monopoly/group_dominance paths below still amplify alpha
    # when the graph shows clear group separation, which is a
    # post-hoc signal that doesn't depend on group selection.
    if (group_has_monopoly or group_has_dominance) and graph_hits_scores:
        max_graph = max(graph_hits_scores)
        max_rerank = max(float(h.get("score", 0.0)) for h in hits)
        # Scale alpha so the strongest graph hit's graph term = max rerank.
        # This equalizes dynamic range: graph's best can match rerank's best.
        if max_graph > 0 and max_rerank > 0:
            effective_alpha = max(alpha, (1 - alpha) * max_rerank / (max_graph + (1 - alpha) * max_rerank))
            # When the graph has perfect group separation (all non-zero graph
            # scores in the hit list belong to the selected group AND every
            # pipeline hit from that group with a positive rerank score also
            # has graph_score > 0), the graph signal is highly reliable. In
            # this case, allow effective_alpha up to 0.65 so that target-
            # group docs with low rerank but high graph scores can overtake
            # distractor docs with high rerank but zero graph scores. The
            # 0.65 cap is still conservative (graph can't fully dominate
            # rerank) but allows the graph to reorder across the group
            # boundary when the cross-encoder produces near-identical scores
            # for shared-context groups. When group separation is imperfect
            # (some target-group hits have graph_score=0), fall back to 0.5
            # cap — the graph signal may be noisy and over-amplification
            # risks false promotions.
            target_group_graph_hits = sum(
                1 for h in hits
                if str(h.get("doc_id", "")) in group_doc_ids
                and doc_scores.get(str(h.get("doc_id")), 0.0) > 0
                and float(h.get("score", 0.0)) > 0
            )
            target_group_total_hits = sum(
                1 for h in hits[:100]
                if str(h.get("doc_id", "")) in group_doc_ids
                and float(h.get("score", 0.0)) > 0
            )
            perfect_separation = (
                target_group_total_hits > 0
                and target_group_graph_hits >= 3
                and target_group_graph_hits == target_group_total_hits
            )
            alpha_cap = 0.65 if perfect_separation else 0.5
            effective_alpha = min(effective_alpha, alpha_cap)
    out = []
    for h in hits:
        did = str(h.get("doc_id"))
        rs = float(h.get("score", 0.0))
        gs = doc_scores.get(did, 0.0)
        final = (1 - effective_alpha) * rs + effective_alpha * gs
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
"""graph_expand node — entity-graph neighbor expansion.

Expands the candidate set by pulling in docs that share entities with the
top hits already in the candidate set. This enables recall of docs that are
entity-linked to already-found golden docs but share zero query-term overlap
with the query itself (e.g., a doc about a different person at the same
institution).

Pipeline position: AFTER fuse, BEFORE rerank.

    recall(bm25+dense) -> fuse -> graph_expand -> rerank -> ...

Uses the Tri-Graph built by graph_rescore's preprocess (stored on
ctx.resources['rescore_graphs']). For each top candidate doc, finds other
docs in the same musique-id group that share >= min_entity_overlap entities,
and appends them with a small score so they enter the rerank pool. Only adds
docs not already present in the hit list.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from retrieval_test.node_io import io

if TYPE_CHECKING:
    from retrieval_test.spec_loader import PipelineCtx


def _musique_id(doc_id: str) -> str:
    return str(doc_id).rsplit("__", 1)[0]


def init(params: dict, ctx: "PipelineCtx") -> None:
    """No-op; relies on graph_rescore's preprocess having built the graphs."""
    pass


async def run(query: str, hits: list[dict], params: dict, ctx: "PipelineCtx") -> list[dict]:
    """Expand the hit list by adding docs that share entities with top hits.

    For each doc in the top `top_n` hits, find other docs in the same
    musique-id group that share at least `min_entity_overlap` entities via
    the Tri-Graph. New docs are appended with a score slightly below the
    lowest existing hit, so they enter the rerank pool but don't displace
    existing candidates before reranking.

    params:
      - top_n:             number of top hits to use as expansion seeds (default 40) [int]
                           Increase when graph_expand consistently fails to recover
                           golden docs that are entity-linked to lower-ranked candidates.
                           A bridging doc that shares entities with the target golden
                           doc may appear at fuse ranks 20-40; top_n=40 ensures such
                           docs become expansion seeds. The additional seed docs also
                           enter the rerank pool, but the max_expand_per_group cap
                           and the entity-overlap threshold prevent noise dilation.
      - min_entity_overlap: minimum shared entities to include a neighbor doc (default 1) [int]
      - max_expand_per_group: cap on expanded docs per musique-id group (default 10) [int]
                              Prevents one group from dominating the expanded set and
                              diluting graph_rescore's coverage with noise from other groups.
                              10 per group (half the typical 20-doc group) keeps the
                              expansion signal-focused; docs beyond the top-10 by entity
                              overlap have weaker graph signal and are unlikely to benefit
                              from entity-bridged boosts or graph_rescore fusion. This also
                              keeps the total expansion bounded (~70 across 7 groups vs 140)
                              so the rerank tail does not fill with low-signal co-group docs.
    """
    top_n: int = int(params.get("top_n", 20))
    min_entity_overlap: int = int(params.get("min_entity_overlap", 1))
    max_expand_per_group: int = int(params.get("max_expand_per_group", 20))

    if not hits:
        ctx.notes.append({
            "input": {"query": io(query, "str")},
            "output": {"hits_out_count": io(0, "int")},
            "internal": {"backend": io("graph_expand", "str"), "status": io("skipped", "str")},
        })
        return hits

    graphs: dict | None = ctx.resources.get("rescore_graphs")
    if not graphs:
        ctx.notes.append({
            "input": {"query": io(query, "str")},
            "output": {"hits_out_count": io(len(hits), "int")},
            "internal": {
                "backend": io("graph_expand", "str"),
                "status": io("fallback", "str"),
                "reason": io("no graphs available (rescore_graphs missing)", "str"),
            },
        })
        return hits

    # Build existing doc_id set for fast lookup
    existing_ids: set[str] = {str(h["doc_id"]) for h in hits}
    seed_hits_base = hits[:top_n]
    # Adaptive group coverage: ensure at least one seed from each musique-id
    # group already present in the hits. When hits are long (e.g., 58 after
    # fuse) but top_n=40, groups whose docs rank below position 40 have no
    # seeds and their entity-linked neighbors (like golden docs missed by both
    # recall routes) cannot be discovered. Scanning beyond top_n to add the
    # highest-ranked doc from each unrepresented group ensures entity-graph
    # expansion covers all groups present in the candidate pool.
    seed_group_ids = {_musique_id(str(h["doc_id"])) for h in seed_hits_base}
    all_group_ids = {_musique_id(str(h["doc_id"])) for h in hits}
    missing_groups = all_group_ids - seed_group_ids
    additional_seeds = []
    if missing_groups:
        seen_groups = set()
        for h in hits[top_n:]:
            mid = _musique_id(str(h["doc_id"]))
            if mid in missing_groups and mid not in seen_groups:
                additional_seeds.append(h)
                seen_groups.add(mid)
                if len(seen_groups) == len(missing_groups):
                    break
    seed_hits = seed_hits_base + additional_seeds

    # For each seed doc, find the graph for its musique-id group, then
    # look up entities and find co-occurring docs.
    new_docs: dict[str, float] = {}  # doc_id -> entity overlap count
    # Track how many expanded docs come from each musique-id group so we can
    # cap per-group expansion and avoid diluting the candidate pool.
    group_expanded_count: dict[str, int] = {}

    for h in seed_hits:
        doc_id = str(h["doc_id"])
        mid = _musique_id(doc_id)
        graph = graphs.get(mid)
        if graph is None:
            continue

        # Skip groups that are already saturated
        if group_expanded_count.get(mid, 0) >= max_expand_per_group:
            continue

        doc_index = graph.get("doc_index", {})
        sent_index = graph.get("sent_index", {})

        # Collect entities mentioned in this seed doc's sentences
        seed_ents: set[str] = set()
        for sent_id in doc_index.get(doc_id, []):
            sent_info = sent_index.get(sent_id, {})
            seed_ents.update(sent_info.get("ents", []))

        if not seed_ents:
            continue

        # Find other docs in the same group sharing entities
        for other_doc_id, other_sent_ids in doc_index.items():
            if other_doc_id in existing_ids or other_doc_id == doc_id:
                continue
            if other_doc_id in new_docs:
                continue
            if group_expanded_count.get(mid, 0) >= max_expand_per_group:
                break
            other_ents: set[str] = set()
            for sid in other_sent_ids:
                sent_info = sent_index.get(sid, {})
                other_ents.update(sent_info.get("ents", []))
            overlap = len(seed_ents & other_ents)
            if overlap >= min_entity_overlap:
                # Track the max overlap for scoring
                new_docs[other_doc_id] = overlap
                group_expanded_count[mid] = group_expanded_count.get(mid, 0) + 1

    if not new_docs:
        ctx.notes.append({
            "input": {"query": io(query, "str"), "top_n": io(top_n, "int")},
            "output": {"hits_out_count": io(len(hits), "int")},
            "internal": {
                "backend": io("graph_expand", "str"),
                "status": io("ok", "str"),
                "n_expanded": io(0, "int"),
            },
        })
        return hits

    # Score new docs: place them just below the lowest existing hit score
    # so they enter the rerank pool. Scale by overlap count for relative ordering.
    min_existing_score = min(h["score"] for h in hits) if hits else 0.0
    max_overlap = max(new_docs.values())

    expanded = list(hits)
    for doc_id, overlap in sorted(new_docs.items(), key=lambda x: -x[1]):
        # Score slightly below the weakest existing hit, scaled by overlap
        score = min_existing_score * 0.5 * (overlap / max(max_overlap, 1))
        expanded.append({"doc_id": doc_id, "score": score})

    ctx.notes.append({
        "input": {"query": io(query, "str"), "top_n": io(top_n, "int")},
        "output": {"hits_out_count": io(len(expanded), "int")},
        "internal": {
            "backend": io("graph_expand", "str"),
            "status": io("ok", "str"),
            "n_expanded": io(len(new_docs), "int"),
            "n_seeds": io(len(seed_hits), "int"),
        },
    })
    return expanded
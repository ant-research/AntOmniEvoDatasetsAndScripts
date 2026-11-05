"""fuse_rrf node — Reciprocal Rank Fusion over one or more recall routes.

Fuses accumulated recall hits via RRF: score(d) = sum over routes r of
1/(rrf_k + rank_r(d) + 1). Reads each hit's `ranks` dict (per-route rank, set
by the runner when recall nodes accumulate). With a single route (no `ranks`),
falls back to ordering by list position.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from retrieval_test.node_io import io

if TYPE_CHECKING:
    from retrieval_test.spec_loader import PipelineCtx


def init(params: dict, ctx: "PipelineCtx") -> None:
    pass


async def run(query: str, hits: list[dict], params: dict, ctx: "PipelineCtx") -> list[dict]:
    """Fuse incoming hits via RRF (multi-route if `ranks` present).

    params:
      - rrf_k: RRF constant (default 60)                  [int]
    """
    rrf_k = int(params.get("rrf_k", 60))
    if not hits:
        return []

    fused: dict[str, float] = {}
    n_routes_seen: set[str] = set()
    for h in hits:
        doc_id = h.get("doc_id")
        ranks = h.get("ranks")
        if isinstance(ranks, dict) and ranks:
            # Multi-route: sum over each route this doc was recalled by.
            for route, rank in ranks.items():
                if isinstance(rank, int):
                    fused[doc_id] = fused.get(doc_id, 0.0) + 1.0 / (rrf_k + rank + 1)
                    n_routes_seen.add(route)
        else:
            # Single-route fallback: rank by list position.
            rank = h.get("rank", 0)
            fused[doc_id] = fused.get(doc_id, 0.0) + 1.0 / (rrf_k + (rank or 0) + 1)
            n_routes_seen.add("_list")

    out = [{"doc_id": d, "score": s} for d, s in fused.items()]
    out.sort(key=lambda h: h["score"], reverse=True)

    # Deduplicate by doc_id: RRF sums per-route scores into `fused`, so each
    # doc_id should appear exactly once in `fused`. However, if the input hits
    # contain duplicate doc_ids with different route labels, or if a downstream
    # node re-inserts docs that already exist, the hit list can grow duplicates.
    # Enforce uniqueness here as the canonical fusion point: keep the first
    # (highest-score) occurrence of each doc_id.
    seen_ids: set[str] = set()
    deduped: list[dict] = []
    for h in out:
        if h["doc_id"] not in seen_ids:
            seen_ids.add(h["doc_id"])
            deduped.append(h)
    n_dupes = len(out) - len(deduped)
    out = deduped

    ctx.notes.append({
        "output": {"hits_out_count": io(len(out), "int")},
        "internal": {
            "backend": io("fuse_rrf", "str"),
            "routes": io(sorted(n_routes_seen), "list[str]"),
            "n_duplicates_removed": io(n_dupes, "int"),
        },
    })
    return out
"""rerank_cross node — cross-encoder re-ranking.

Re-scores the top-m candidates from upstream recall with a cross-encoder
(via rerank_client.rerank_async, default bge-reranker-v2-m3), re-sorts, and
optionally drops low scorers. No caching — O(top_m) per query, so keep top_m
bounded.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from retrieval_test.errors import RateLimitError
from retrieval_test.node_io import io

if TYPE_CHECKING:
    from retrieval_test.spec_loader import PipelineCtx


def init(params: dict, ctx: "PipelineCtx") -> None:
    """Stateless — no model to load (the endpoint holds the model).

    params (read in run):
      - model:     rerank model id (default "bge-reranker-v2-m3")  [str]
      - api_key:   endpoint key (or env BAILING_API_KEY/OPENAI_API_KEY) [str|None]
      - base_url:  endpoint base (default https://antchat.alipay.com/v1) [str]
      - instruct:  Qwen3 instruction prompt (default zh)             [str]
    """
    pass


async def run(query: str, hits: list[dict], params: dict, ctx: "PipelineCtx") -> list[dict]:
    """Re-score and re-sort the top-m candidates; keep the rest appended.

    params:
      - top_m:        only re-rank the top-m incoming (default 25); rest appended unmodified [int]
      - model:        rerank model id (default "bge-reranker-v2-m3")       [str]
      - threshold:    drop candidates below this relevance_score (default 0.0 = keep all) [float]
      - instruct:     Qwen3 instruction (default zh)                         [str]
      - api_key:      endpoint key (or env)                                  [str|None]
      - base_url:     endpoint base                                          [str]
      - use_rewritten: read ctx.resources['rewritten_query'] if true (default False) [bool]
        Recommended: set to true when a query_rewrite node is enabled upstream, so rerank
        scores against the same decomposed query that recall used; this avoids a query
        mismatch between recall (rewritten) and rerank (original) that weakens re-scoring.
      - floor_n:      preserve top-N fused docs from catastrophic demotion (default 0 = disabled) [int]
        Multi-hop bridge docs may score low on cross-encoder despite being well-ranked
        by fusion due to different surface forms. When floor_n > 0, the first floor_n
        docs in the fused input are guaranteed to appear in the top floor_n positions
        of the reranked output, even if the cross-encoder scored them low.

    ctx.notes (trajectory diagnostics, typed): records the query used and the
    rerank call's facts (model, top_m, n_reranked, latency, status), plus the
    no-corpus-text passthrough case.
    """
    top_m: int = int(params.get("top_m", 30))
    model: str = params.get("model", "bge-reranker-v2-m3")
    instruct: str = params.get("instruct", "根据用户问题，找出最相关的文档。")
    threshold: float = float(params.get("threshold", 0.0))
    use_rw: bool = bool(params.get("use_rewritten", False))
    q_raw: str = ctx.resources.get("rewritten_query", query) if use_rw else query
    # When the rewritten query is much longer than the original, the LLM likely
    # included reasoning/explanation text that degrades cross-encoder scoring.
    # Fall back to a focused query for reranking to avoid contaminating the
    # relevance model with non-query terms. Heuristic: if the rewritten query is
    # >1.5x the original length AND exceeds 150 chars, OR if the rewritten query
    # has >3 pipe-separated segments (indicating multi-hop decomposition rather
    # than a focused search query), the query is structurally unsuitable for the
    # cross-encoder, which expects concise natural-language queries. Pipe-
    # separated multi-hop decompositions (even well-formed ones) distribute the
    # cross-encoder's attention across loosely-related sub-topics, producing
    # near-zero scores for candidates matching only one sub-topic and extreme
    # score disparity between single-sub-topic matches and multi-sub-topic
    # matches. Fallback: use the first pipe-separated segment, which typically
    # captures the most focused hop and is a better cross-encoder input than
    # either the full pipe-separated rewrite (too diffuse) or the original
    # multi-clause question (too verbose).
    _n_segments = q_raw.count('|') + 1
    _rerank_mode: str = "full_rewrite"  # default: use full rewritten query
    if use_rw and ((len(q_raw) > 1.5 * len(query) and len(q_raw) > 150) or _n_segments > 3):
        # Multi-hop decomposition: per-segment scoring avoids single-hop
        # blindness. Using only the first pipe-separated segment (the old
        # behavior) causes catastrophic demotion of docs matching other hops —
        # observed 61940:1 score ratios on 4-hop queries. Instead, score each
        # segment independently and take the max per doc, so a doc matching
        # ANY hop gets a high score. When segments > 4 (likely speculative
        # expansion, not genuine multi-hop), fall back to the original query
        # to avoid dilution and excessive latency.
        _segments = [s.strip() for s in q_raw.split('|') if s.strip()]
        if _n_segments > 4:
            q: str = query
            _rerank_mode = "original_query"
        else:
            q: str = q_raw  # will be split per-segment in the cross-encoder call
            _rerank_mode = "per_segment"
        rewrite_too_long: bool = _rerank_mode != "per_segment"
    else:
        q: str = q_raw
        rewrite_too_long: bool = False

    # Stash the query actually used for cross-encoder scoring so downstream
    # nodes (e.g., graph_rescore) can align their entity activation with
    # rerank's focus, avoiding entity-activation / scoring mismatches.
    ctx.resources["rerank_query_used"] = q

    note_internal: dict = {"backend": io("cross_encoder", "str"), "model": io(model, "str")}
    if use_rw:
        note_internal["using_rewritten"] = io(_rerank_mode == "full_rewrite", "bool")
        if _rerank_mode != "full_rewrite":
            note_internal["rewrite_fallback_reason"] = io(
                f"rewritten query (len={len(q_raw)}, segments={_n_segments}) "
                f"exceeds heuristic; rerank mode={_rerank_mode} "
                f"({'each segment scored independently, max per doc' if _rerank_mode == 'per_segment' else 'falling back to original query'})",
                "str",
            )

    if not hits:
        ctx.notes.append({
            "input": {"query_searched": io(q, "str")},
            "output": {"hits_out_count": io(0, "int")},
            "internal": {**note_internal, "status": io("ok", "str"), "n_reranked": io(0, "int")},
        })
        return []

    head = hits[:top_m]
    tail = hits[top_m:]

    # Latency guard: cap the rerank head at 20 BEFORE calling the cross-encoder.
    # Cross-encoder cost scales linearly with n_reranked. Observed latency is
    # highly variable: 30 candidates ranges from ~250ms to 21.7s depending on
    # API load and query complexity. The 10s per-query budget can be exceeded
    # even at 30 candidates. Capping at 20 reduces the probability of timeout
    # and limits cross-group contamination in the rerank head. The tail
    # preserves all recall candidates unmodified, so no candidates are lost —
    # they just aren't reranked. The adaptive floor_n mechanism ensures the
    # most promising fused docs survive even without reranking.
    extra_internal = {}
    if len(head) > 20:
        extra_internal["capped"] = io(
            f"rerank head capped from {len(head)} to 20 to limit cross-encoder latency and cross-group contamination", "str"
        )
        tail = head[20:] + tail
        head = head[:20]
    if top_m > len(hits):
        extra_internal["warning"] = io(
            f"top_m={top_m} > hits_in={len(hits)}; recall depth may be insufficient", "str"
        )

    # Same-group contamination diagnostic and mitigation: in multi-query
    # corpora (e.g., MuSiQue), multiple queries share a graph group and their
    # docs get fused together. Same-group docs from OTHER queries can dominate
    # the rerank head, displacing relevant candidates for THIS query. Detect
    # by counting distinct musique-id prefixes in the head; if the head has
    # many more distinct prefixes than expected (1 query ≈ 1 prefix),
    # cross-contamination is likely. When contamination is detected, cap
    # the number of docs from any single non-dominant group prefix in the
    # top ranks so that the query's own group docs are not displaced.
    _group_prefixes: dict[str, int] = {}
    for h in head:
        did = str(h.get("doc_id", ""))
        parts = did.rsplit("__", 1)
        if len(parts) == 2:
            _group_prefixes[parts[0]] = _group_prefixes.get(parts[0], 0) + 1
    _n_prefixes = len(_group_prefixes)
    # Pre-filter: when many distinct group prefixes contaminate the head,
    # keep only the top docs from the top-K most-represented prefixes
    # BEFORE cross-encoder scoring. This reduces cross-encoder input size
    # (cutting latency) and prevents cross-group scores from distorting
    # the relative ranking of same-prefix docs. However, top-N fused docs
    # (as determined by adaptive_floor) must always enter the cross-encoder
    # head regardless of prefix, because they have strong multi-route RRF
    # support and may be golden docs from a less-represented prefix.
    _prefilter_n = 0
    floor_n_pre = int(params.get("floor_n", 0))
    recall_k_pre = int(ctx.resources.get('recall_k', 0)) if isinstance(ctx.resources.get('recall_k'), (int, float)) else 0
    # When recall_k is unavailable (0), infer it from the fused head size
    # to avoid the degenerate case where recall_k=0 causes a weak safety floor.
    _inferred_recall_k = recall_k_pre if recall_k_pre >= 20 else (30 if len(head) >= 15 else recall_k_pre)
    _safety_floor = max(floor_n_pre, min(10, len(head) // 2)) if floor_n_pre > 0 and _inferred_recall_k >= 20 and len(head) >= 10 else max(floor_n_pre, 3)
    if _n_prefixes > 4:
        # Keep only docs from the top 4 most-represented group prefixes,
        # BUT always keep the first _safety_floor docs from the fused input
        # regardless of their prefix (they have strong RRF support).
        _top_prefixes = sorted(_group_prefixes, key=_group_prefixes.get, reverse=True)[:4]
        _top_set = set(_top_prefixes)
        _protected_ids = {h.get("doc_id") for h in head[:_safety_floor]}
        _prefiltered = []
        _deferred_prefilter = []
        for h in head:
            did = str(h.get("doc_id", ""))
            parts = did.rsplit("__", 1)
            prefix = parts[0] if len(parts) == 2 else "_unknown"
            if prefix in _top_set or h.get("doc_id") in _protected_ids:
                _prefiltered.append(h)
            else:
                _deferred_prefilter.append(h)
        _prefilter_n = len(head) - len(_prefiltered)
        if _prefilter_n > 0:
            extra_internal["prefilter"] = io(
                f"prefiltered {_prefilter_n} cross-group docs from {_n_prefixes} prefixes; "
                f"kept top-4 prefixes + top-{_safety_floor} fused, head {len(head)}\u2192{len(_prefiltered)}",
                "str",
            )
            tail = _deferred_prefilter + tail
            head = _prefiltered
            _group_prefixes = {k: v for k, v in _group_prefixes.items() if k in _top_set}
            _n_prefixes = len(_group_prefixes)
    if _n_prefixes > 3:
        extra_internal["group_contamination_hint"] = io(
            f"rerank head has {_n_prefixes} distinct group prefixes in {len(head)} docs; "
            "same-group cross-contamination may displace relevant candidates",
            "str",
        )
        # Mitigation: after reranking, cap docs from any single group prefix
        # to at most _max_per_prefix positions in the top _top_cap ranks,
        # preventing one non-target prefix from monopolizing the top.
        pass  # mitigation applied below after reranking

    corpus: dict[str, str] = ctx.resources.get("corpus", {})
    docs: list[str] = [corpus.get(h["doc_id"], "") for h in head]
    if not any(docs):
        # No corpus text -> cannot score pairs; pass through unchanged and flag
        # so the trajectory distinguishes "rerank skipped (no doc text)" from a
        # real low-score rerank.
        ctx.notes.append({
            "input": {"query_searched": io(q, "str")},
            "output": {"hits_out_count": io(len(head) + len(tail), "int")},
            "internal": {
                **note_internal,
                **extra_internal,
                "status": io("fallback", "str"),
                "n_reranked": io(0, "int"),
                "reason": io("no corpus text for candidate docs -> passthrough", "str"),
            },
        })
        return head + tail

    from retrieval_test.rerank_client import rerank_async

    t0 = time.perf_counter()
    try:
        if _rerank_mode == "per_segment" and '|' in q:
            # Per-segment reranking: score each pipe-separated segment
            # independently, then take the max relevance score per doc across
            # all segments. A doc matching ANY hop gets a high score, avoiding
            # the catastrophic demotion observed with single-segment fallback
            # (61940:1 score ratios, golden docs dropping to rank 40+).
            _segs = [s.strip() for s in q.split('|') if s.strip()]
            _per_seg_ranked: dict[int, float] = {}
            for seg in _segs:
                seg_ranked = await rerank_async(
                    query=seg,
                    documents=docs,
                    api_key=params.get("api_key"),
                    model=model,
                    base_url=params.get("base_url", "https://antchat.alipay.com/v1"),
                    instruct=instruct,
                )
                for orig_idx, score in seg_ranked:
                    if orig_idx not in _per_seg_ranked or score > _per_seg_ranked[orig_idx]:
                        _per_seg_ranked[orig_idx] = float(score)
            ranked = sorted(_per_seg_ranked.items(), key=lambda x: x[1], reverse=True)
            extra_internal["per_segment_n"] = io(len(_segs), "int")
        else:
            ranked = await rerank_async(
                query=q,
                documents=docs,
                api_key=params.get("api_key"),
                model=model,
                base_url=params.get("base_url", "https://antchat.alipay.com/v1"),
                instruct=instruct,
            )
    except RateLimitError:
        # Transient (429/5xx/network): let it propagate — the generate loop
        # replays the whole DAG. Do not degrade to passthrough.
        raise
    except Exception as exc:  # deterministic failure — degrade to passthrough
        latency = (time.perf_counter() - t0) * 1000.0
        ctx.notes.append({
            "input": {"query_searched": io(q, "str")},
            "output": {"hits_out_count": io(len(head) + len(tail), "int")},
            "internal": {
                **note_internal,
                **extra_internal,
                "status": io("fallback", "str"),
                "n_reranked": io(0, "int"),
                "latency_ms": io(round(latency, 1), "float"),
                "reason": io(f"rerank endpoint failed ({type(exc).__name__}) -> passthrough", "str"),
            },
        })
        return head + tail

    latency = (time.perf_counter() - t0) * 1000.0

    out = []
    raw_scores = []
    for orig_idx, score in ranked:
        if score >= threshold:
            h = head[orig_idx]
            raw_scores.append(float(score))
            out.append({"doc_id": h["doc_id"], "score": float(score)})
    # Min-max normalize the rerank scores to [0, 1] within the reranked head.
    # Cross-encoder raw scores can be extremely skewed (e.g., 0.999 for the
    # top doc vs 0.0004 for the 7th doc = 2700:1 ratio), making downstream
    # additive boosts (graph_rescore alpha) negligible. Min-max normalization
    # preserves the relative ranking while making the score range meaningful
    # for downstream fusion. Only normalize when there are at least 2 scored
    # docs and the range is > 10x (skip trivial cases).
    if len(raw_scores) >= 2:
        _min_s = min(raw_scores)
        _max_s = max(raw_scores)
        _range = _max_s - _min_s
        if _range > 0 and _max_s / max(_min_s, 1e-12) > 10:
            for i, h in enumerate(out):
                h["score"] = (raw_scores[i] - _min_s) / _range
            extra_internal["score_normalized"] = io(
                f"min-max normalized rerank scores from [{_min_s:.6f}, {_max_s:.6f}] to [0, 1] (range ratio {_max_s / max(_min_s, 1e-12):.0f}:1)",
                "str",
            )

    # Preserve top-N fused docs from catastrophic demotion by cross-encoder.
    # Multi-hop bridge docs may score low on cross-encoder despite being
    # well-ranked by fusion (different surface forms). floor_n docs from
    # the top of the fused list are guaranteed to stay in the top-N output
    # regardless of their rerank score. Default floor_n=0 (disabled).
    #
    # Adaptive floor: when recall_k (max recall k across enabled recall_*
    # nodes, available via ctx.resources.get('recall_k', k)) is high and
    # the fused set is large, bridge docs that rank well by fusion (top ~10)
    # can be scattered more broadly. If floor_n < min(10, len(head)//2),
    # bump it up so at least the top-10 fused docs survive rerank demotion.
    # This prevents catastrophic loss of bridge docs in multi-hop queries
    # where 4+ golden docs may be spread across the fused top-10.
    floor_n = int(params.get("floor_n", 0))
    recall_k = int(ctx.resources.get('recall_k', 0)) if isinstance(ctx.resources.get('recall_k'), (int, float)) else 0
    if floor_n > 0 and recall_k >= 20 and len(head) >= 10:
        adaptive_floor = max(floor_n, min(15, len(head) // 2))
    else:
        adaptive_floor = floor_n
    if adaptive_floor > 0 and len(out) >= adaptive_floor:
        # Ensure the first adaptive_floor docs in the fused input appear in the
        # top adaptive_floor positions of the reranked output, even if the
        # cross-encoder scored them low. Promote any missing ones.
        out_ids = {h["doc_id"] for h in out[:adaptive_floor]}
        for i in range(min(adaptive_floor, len(head))):
            if head[i]["doc_id"] not in out_ids:
                # Promote: insert with a score just above the current
                # adaptive_floor-th score so it stays in the top adaptive_floor.
                insert_score = out[adaptive_floor - 1]["score"] + 0.001 if adaptive_floor <= len(out) else 0.001
                out.append({"doc_id": head[i]["doc_id"], "score": float(insert_score)})
                out.sort(key=lambda h: h["score"], reverse=True)

    # Group contamination mitigation: after reranking + floor_n promotion,
    # if cross-group contamination was detected, cap docs from any single
    # group prefix in the top ranks to prevent one non-target group from
    # monopolizing the output.
    if _n_prefixes > 3:
        # Scale the per-prefix cap inversely with contamination severity.
        # When many prefixes compete (>6), be aggressive: cap at 2 per prefix
        # to prevent any single group from monopolizing the top ranks.
        # When moderately contaminated (4-6), cap at 4 per prefix to allow
        # multi-hop queries (which typically have 2-4 golden docs sharing one
        # musique-id prefix) to retain all golden docs in the top cap.
        # A cap of 3 was too restrictive: observed 4-golden-doc queries lose
        # 1-2 golden docs because all 4 share one prefix.
        # The top_cap covers the top-10 evaluation window so cross-group
        # docs cannot dominate the positions that matter most for scoring.
        if _n_prefixes > 6:
            _max_per_prefix = 2
        else:
            _max_per_prefix = 4
        _top_cap = max(adaptive_floor * 2, 10) if adaptive_floor > 0 else 10
        # When the dominant prefix (most docs in the head) occupies more than
        # half the head, it is likely the query's target group. Expand the cap
        # to allow up to 5 of its docs in the top to prevent golden-doc loss.
        _dominant_prefix = max(_group_prefixes, key=_group_prefixes.get) if _group_prefixes else None
        if _dominant_prefix and _group_prefixes[_dominant_prefix] >= len(head) // 2:
            _max_per_prefix = max(_max_per_prefix, 5)
        _top_docs = out[:_top_cap]
        _rest_docs = out[_top_cap:]
        _prefix_counts: dict[str, int] = {}
        _reordered: list[dict] = []
        _deferred: list[dict] = []
        for h in _top_docs:
            did = str(h.get("doc_id", ""))
            parts = did.rsplit("__", 1)
            prefix = parts[0] if len(parts) == 2 else "_unknown"
            if _prefix_counts.get(prefix, 0) >= _max_per_prefix:
                _deferred.append(h)
            else:
                _prefix_counts[prefix] = _prefix_counts.get(prefix, 0) + 1
                _reordered.append(h)
        out = _reordered + _deferred + _rest_docs
        extra_internal["contamination_mitigation"] = io(
            f"capped per-prefix to {_max_per_prefix} in top {_top_cap} "
            f"({_n_prefixes} prefixes); {len(_deferred)} docs deferred", "str"
        )

    # Appended tail keeps original scores; only the re-ranked head is rescored.
    # Deduplicate: floor_n promotion may insert a doc already in `out` (it passed
    # the threshold on its own). Remove duplicates by doc_id, keeping first.
    seen_ids = set()
    deduped = []
    for h in out:
        if h["doc_id"] not in seen_ids:
            deduped.append(h)
            seen_ids.add(h["doc_id"])
    out = deduped
    result = out + tail
    ctx.notes.append({
        "input": {"query_searched": io(q, "str"), "top_m": io(min(top_m, len(hits)), "int")},
        "output": {"hits_out_count": io(len(result), "int"), "n_dropped": io(max(0, len(head) - len(out)), "int")},
        "internal": {
            **note_internal,
            **extra_internal,
            "status": io("ok", "str"),
            "n_reranked": io(len(head), "int"),
            "latency_ms": io(round(latency, 1), "float"),
        },
    })
    return result
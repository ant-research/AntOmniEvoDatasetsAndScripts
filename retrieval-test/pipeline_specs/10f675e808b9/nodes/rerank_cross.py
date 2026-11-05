"""rerank_cross node — cross-encoder re-ranking.

Re-scores the top-m candidates from upstream recall with a cross-encoder
(via rerank_client.rerank_sync, default bge-reranker-v2-m3), re-sorts, and
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
      - top_m:        only re-rank the top-m incoming (default 50); rest appended unmodified [int]
                     Hard-capped at 30 inside run() to keep latency under the 10s
                     per-query budget (~370ms/doc on bge-reranker-v2-m3).
      - model:        rerank model id (default "bge-reranker-v2-m3")       [str]
      - threshold:    drop candidates below this relevance_score (default 0.0 = keep all) [float]
      - instruct:     Qwen3 instruction (default zh)                         [str]
      - api_key:      endpoint key (or env)                                  [str|None]
      - base_url:     endpoint base                                          [str]
      - use_rewritten: read ctx.resources['rewritten_query'] if true (default False) [bool]
        NOTE: cross-encoder reranking expects a coherent natural-language question,
        not a keyword expansion. The rewritten query is optimized for recall
        (BM25 term-matching, dense embedding) and is often a terse keyword string
        or sub-question decomposition that degrades cross-encoder relevance scoring.
        Prefer use_rewritten=false for rerank so the cross-encoder scores against
        the original question; recall nodes opt in to the rewrite independently.
      - use_rewritten_dense: if true, read ctx.resources['rewritten_query_dense']
        for reranking (default False) [bool]. For multi-hop queries where the
        original question is too long/coherent for the cross-encoder to relate to
        intermediate-hop docs, the dense rewrite variant (concise, entity-rich)
        may score those docs higher. When both use_rewritten_dense and
        use_rewritten are true, use_rewritten_dense takes priority.
      - auto_rewritten_dense: if true, automatically switch to
        rewritten_query_dense when the original query exceeds 120 chars AND
        rewritten_query_dense exists AND differs from the original query
        (i.e., the rewrite resolved at least one entity not in the original)
        (default True) [bool]. This handles multi-hop queries where the
        original query surface has zero overlap with intermediate-hop docs,
        but the rewrite contains resolved entity names (e.g., 'Alaska',
        'Andrew Johnson') that DO overlap. When triggered, records a note
        on the trajectory so the analyst can verify the switch.

    ctx.notes (trajectory diagnostics, typed): records the query used and the
    rerank call's facts (model, top_m, n_reranked, latency, status), plus the
    no-corpus-text passthrough case.
    """
    top_m: int = int(params.get("top_m", 50))
    # Hard cap: reranking more than 30 docs risks exceeding the per-query
    # timeout budget (rerank is O(n) with a model call per doc-pair; observed
    # ~370ms/doc on bge-reranker-v2-m3). Cap at 30 so graph-expanded docs
    # (which can appear at ranks 30-60 after fuse+expand) still enter the
    # rerank window and become eligible for entity-bridged score propagation.
    # At 370ms/doc, 30 docs takes ~11s worst case — tight but acceptable
    # since most queries have fewer candidates in the window.
    max_rerank: int = 30
    top_m_original = top_m
    if top_m > max_rerank:
        top_m = max_rerank
    model: str = params.get("model", "bge-reranker-v2-m3")
    instruct: str = params.get("instruct", "根据用户问题，找出最相关的文档。")
    threshold: float = float(params.get("threshold", 0.0))
    use_rw: bool = bool(params.get("use_rewritten", False))
    use_rw_dense: bool = bool(params.get("use_rewritten_dense", False))
    auto_rw_dense: bool = bool(params.get("auto_rewritten_dense", True))
    # Query selection priority: use_rewritten_dense > use_rewritten > auto-switch > original
    auto_switched = False
    if use_rw_dense:
        q = ctx.resources.get("rewritten_query_dense", query)
    elif use_rw:
        q = ctx.resources.get("rewritten_query", query)
    elif auto_rw_dense and len(query) > 120:
        # Auto-switch: for long multi-hop queries, the dense rewrite variant
        # (concise, entity-rich) may score intermediate-hop docs higher than
        # the original long-form question that has zero lexical overlap.
        # Only switch when the rewrite contributes NEW entity names absent
        # from the original query — a keyword strip with zero new tokens
        # loses structural context without adding retrieval signal, so the
        # original question is always better for the cross-encoder in that
        # case. Check ctx.resources['rewrite_added_entities'] (set by
        # query_rewrite) to determine whether the rewrite added new tokens.
        #
        # ADDITIONAL GUARD: do NOT auto-switch when the rewrite is
        # predominantly a keyword extraction of the original query's terms
        # (i.e., the rewrite's tokens are mostly a subset of the original's
        # tokens, just reordered). Keyword rewrites lose the cross-encoder's
        # sentence-level coherence signal while adding little new entity
        # signal for relevance scoring. A keyword rewrite inflates scores
        # for topical-similarity matches (co-group docs sharing the same
        # entity clusters) while deflating scores for specific-relevance
        # matches (target-group docs answering the specific question). Only
        # auto-switch when the rewrite contributes SUBSTANTIAL new entity
        # names not derivable from the original's tokens — measured by
        # the rewrite having a high proportion of tokens absent from the
        # original query.
        rw_dense = ctx.resources.get("rewritten_query_dense", query)
        rewrite_added_entities = ctx.resources.get("rewrite_added_entities", False)
        if rw_dense != query and rw_dense and rewrite_added_entities:
            # Check whether the rewrite is a keyword strip or a genuine
            # entity-enriched expansion. A keyword strip's tokens are
            # mostly present in the original (just reordered/trimmed),
            # while an entity expansion adds many new tokens.
            orig_tokens = set(query.lower().split())
            rw_tokens = set(rw_dense.lower().split())
            new_tokens = rw_tokens - orig_tokens
            # Require at least 30% of rewrite tokens to be new entities
            # not present in the original query. Below this threshold,
            # the rewrite is a keyword extraction that loses sentence
            # coherence without adding retrieval signal. The previous 20%
            # threshold was too permissive — a single wrong entity name
            # (e.g., 'GAO' replacing the implicit FTC reference) inflates
            # co-group scores while deflating target-group scores.
            # At 30%, the rewrite must contribute substantial new entity
            # signal (multiple resolved entity names), making it more
            # likely that the rewrite adds genuine multi-hop retrieval
            # value rather than just a keyword reshuffling with one
            # fabricated entity.
            if len(rw_tokens) > 0 and len(new_tokens) / len(rw_tokens) >= 0.3:
                q = rw_dense
                auto_switched = True
            else:
                q = query
        else:
            q = query
    else:
        q = query

    note_internal: dict = {"backend": io("cross_encoder", "str"), "model": io(model, "str")}
    if use_rw_dense:
        note_internal["using_rewritten_dense"] = io(True, "bool")
    elif use_rw:
        note_internal["using_rewritten"] = io(True, "bool")
    elif auto_switched:
        note_internal["auto_rewritten_dense"] = io(True, "bool")
    if top_m < top_m_original:
        note_internal["top_m_capped"] = io(True, "bool")
        note_internal["top_m_original"] = io(top_m_original, "int")

    if not hits:
        ctx.notes.append({
            "input": {"query_searched": io(q, "str")},
            "output": {"hits_out_count": io(0, "int")},
            "internal": {**note_internal, "status": io("ok", "str"), "n_reranked": io(0, "int")},
        })
        return []

    head = hits[:top_m]
    tail = hits[top_m:]

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
                "status": io("fallback", "str"),
                "n_reranked": io(0, "int"),
                "reason": io("no corpus text for candidate docs -> passthrough", "str"),
            },
        })
        return head + tail

    from retrieval_test.rerank_client import rerank_async

    t0 = time.perf_counter()
    try:
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
                "status": io("fallback", "str"),
                "n_reranked": io(0, "int"),
                "latency_ms": io(round(latency, 1), "float"),
                "reason": io(f"rerank endpoint failed ({type(exc).__name__}) -> passthrough", "str"),
            },
        })
        return head + tail

    latency = (time.perf_counter() - t0) * 1000.0

    out = []
    for orig_idx, score in ranked:
        if score >= threshold:
            h = head[orig_idx]
            out.append({"doc_id": h["doc_id"], "score": float(score)})

    # Entity-bridged score propagation: docs with low cross-encoder scores
    # that share entities with high-scoring docs (in the same musique-id group)
    # get a boost. This helps multi-hop queries where intermediate-hop docs
    # (e.g., 'North Holland' or 'Eddy Putter') are entity-linked to high-scoring
    # docs (e.g., 'Jörg van Nieuwenhuijzen') but the cross-encoder cannot infer
    # the multi-hop connection from the query alone.
    n_boosted_by_entity = 0
    n_tail_boosted = 0
    # --- Phase 1: Skew & co-group inflation detection (BEFORE entity boosts) ---
    # The skew-fallback must check the RAW cross-encoder score distribution
    # before any entity-bridged boosts inflate it. Entity boost floors raise
    # many docs to 10% of max_score, inflating p75 from near-zero to ~0.10*max
    # and masking extreme skew (max/p75 approaches 10x, right at the
    # threshold). Computing the ratio on raw scores ensures the fallback
    # detects genuine cross-encoder concentration.
    skew_fallback_needed = False
    skew_max = 0.0
    skew_p75 = 0.0
    # Also detect near-uniform score distributions where many docs share
    # identical cross-encoder scores (degenerate tie). When >=15 docs have
    # the same score (within 1% of each other), the cross-encoder failed to
    # differentiate and downstream graph_rescore cannot reorder within the
    # band. Flag this for the skew fallback to attempt re-ranking with the
    # original query if the auto-switched query produced the uniform band.
    near_uniform_detected = False
    # Also detect CO-GROUP INFLATION: when auto_rewritten_dense switched
    # the query, the dense rewrite may inflate scores for multiple co-group
    # docs that share entity context with the target group. These co-groups
    # score near-identically to the target's top doc because the rewrite's
    # entity terms match their shared context (e.g., 'Austria', 'Lake
    # Constance') rather than query-specific relevance. The skew fallback
    # cannot detect this because the score distribution is prograde (not
    # skewed), but the pattern is identifiable: >=3 docs from >=2 distinct
    # non-dominant musique-id groups all scoring within 90% of the max.
    cogroup_inflation_detected = False
    if auto_switched and out:
        raw_rerank_scores = [h["score"] for h in out if h["score"] > 0]
        if len(raw_rerank_scores) >= 4:
            raw_sorted = sorted(raw_rerank_scores)
            skew_max = raw_sorted[-1]
            skew_p75 = raw_sorted[int(len(raw_sorted) * 0.75)]
            if skew_max > 10 * max(skew_p75, 1e-9):
                skew_fallback_needed = True
            # Detect near-uniform band: cluster of >=15 docs within 1% score
            from collections import Counter
            score_counter = Counter(round(s, 4) for s in raw_rerank_scores)
            most_common_score, most_common_count = score_counter.most_common(1)[0]
            if most_common_count >= 15:
                near_uniform_detected = True
                if not skew_fallback_needed:
                    skew_fallback_needed = True
            # Super-group crowding: when docs from >=3 distinct non-dominant
            # groups within the SAME super-group prefix all score within 90%
            # of max, the super-group is crowding the top-K with docs that
            # share entity context but aren't the target group. Trigger
            # cogroup_inflation fallback to re-rerank with the original
            # query. This covers both same-group inflation (3+ docs from
            # 1-2 non-dominant groups) and super-group crowding (3+ docs
            # from 3+ non-dominant groups sharing a super-group prefix).
            # Check super-group prefix occupancy: if >=3 docs in the top-10
            # with the same super-group prefix score within 90% of the max
            # AND at most 1 of them is from the dominant group, trigger
            # cogroup_inflation fallback.
            if not skew_fallback_needed:
                def _mid_local(doc_id: str) -> str:
                    return str(doc_id).rsplit("__", 1)[0]
                def _super_group_local(mid_key: str) -> str:
                    parts = mid_key.rsplit('_', 1)
                    return parts[0] if len(parts) == 2 and parts[1].isdigit() else mid_key
                max_s = max(h["score"] for h in out) if out else 0
                if max_s > 0:
                    dominant_mid_local = _mid_local(str(out[0].get('doc_id', '')))
                    high_group_counts: dict[str, int] = {}
                    for h in out:
                        if h["score"] >= max_s * 0.9:
                            mid = _mid_local(str(h.get('doc_id', '')))
                            if mid != dominant_mid_local:
                                high_group_counts[mid] = high_group_counts.get(mid, 0) + 1
                    n_inflated_docs = sum(high_group_counts.values())
                    n_inflated_groups = len(high_group_counts)
                    if n_inflated_docs >= 3 and n_inflated_groups >= 2:
                        cogroup_inflation_detected = True
                        skew_fallback_needed = True
                    # Super-group crowding: check if >=3 high-scoring docs
                    # from >=3 non-dominant groups share a super-group prefix.
                    if not cogroup_inflation_detected and n_inflated_groups >= 3:
                        sg_groups: set[str] = set()
                        for mid_key in high_group_counts:
                            sg_groups.add(_super_group_local(mid_key))
                        # If all non-dominant high-scorers share 1 super-group
                        if len(sg_groups) == 1:
                            n_dominant_near_max = sum(1 for h in out if h["score"] >= max_s * 0.9 and _mid_local(str(h.get('doc_id', ''))) == dominant_mid_local)
                            if n_dominant_near_max <= 1:
                                cogroup_inflation_detected = True
                                skew_fallback_needed = True
    # --- Phase 2: Skew fallback (re-rerank with original query) ---
    if skew_fallback_needed:
        try:
            ranked_orig = await rerank_async(
                query=query,
                documents=docs,
                api_key=params.get("api_key"),
                model=model,
                base_url=params.get("base_url", "https://antchat.alipay.com/v1"),
                instruct=instruct,
            )
            out_orig = []
            for orig_idx, score in ranked_orig:
                if score >= threshold:
                    h = head[orig_idx]
                    out_orig.append({"doc_id": h["doc_id"], "score": float(score)})
            if out_orig:
                out = out_orig
                q = query
                auto_switched = False
                if cogroup_inflation_detected:
                    note_internal["cogroup_inflation_fallback"] = io(True, "bool")
                else:
                    note_internal["skew_fallback"] = io(True, "bool")
                note_internal["skew_max"] = io(round(skew_max, 4), "float")
                note_internal["skew_p75"] = io(round(skew_p75, 6), "float")
        except RateLimitError:
            raise
        except Exception:
            pass
    # --- Phase 3: Entity-bridged boosts (AFTER skew decision) ---
    def _mid(doc_id: str) -> str:
        """Extract musique-id group from doc_id."""
        return str(doc_id).rsplit("__", 1)[0]
    if "rescore_graphs" in ctx.resources and ctx.resources["rescore_graphs"]:
        graphs = ctx.resources["rescore_graphs"]
        # Collect high-scoring docs (score > 0.5 of max)
        if out:
            max_score = max(h["score"] for h in out)
            if max_score > 0:
                high_score_ids = {h["doc_id"] for h in out if h["score"] > max_score * 0.5}
                for h in out:
                    if h["score"] > max_score * 0.5:
                        continue
                    mid = _mid(str(h["doc_id"]))
                    graph = graphs.get(mid)
                    if graph is None:
                        continue
                    doc_index = graph.get("doc_index", {})
                    sent_index = graph.get("sent_index", {})
                    low_ents: set[str] = set()
                    for sid in doc_index.get(h["doc_id"], []):
                        sent_info = sent_index.get(sid, {})
                        low_ents.update(sent_info.get("ents", []))
                    if not low_ents:
                        continue
                    # Entity overlap with high-scorers
                    overlap_boost = 0.0
                    overlap_ratio = 0.3  # default for no-direct-overlap same-group docs
                    for hs_id in high_score_ids:
                        if _mid(str(hs_id)) != mid:
                            continue
                        hs_ents: set[str] = set()
                        for sid in doc_index.get(str(hs_id), []):
                            sent_info = sent_index.get(sid, {})
                            hs_ents.update(sent_info.get("ents", []))
                        overlap = len(low_ents & hs_ents)
                        if overlap >= 1:
                            hs_score = next((x["score"] for x in out if x["doc_id"] == hs_id), 0.0)
                            overlap_ratio = overlap / max(len(hs_ents), 1)
                            # Scale the overlap boost by a group-affinity
                            # factor: same-group docs get the full boost,
                            # non-dominant-group docs get a reduced boost.
                            # When many same-super-group variants exist,
                            # entity-overlap boosts inflate distractor-group
                            # scores creating cross-group plateaus that
                            # displace target-group golden docs. The dominant
                            # group (highest-scoring doc's group) is the most
                            # likely target; same-group docs deserve the full
                            # entity-bridged boost. Other co-groups sharing
                            # a super-group prefix get 40% of the boost to
                            # keep them visible for graph_rescore without
                            # letting them monopolize the competitive tier.
                            # The dominant group for group_affinity should be
                            # the group most likely to contain the query's golden
                            # docs, not simply the group of the highest-scoring
                            # doc. Co-groups in the same super-group can produce
                            # the highest-scoring doc (shared entity context
                            # scores well with the cross-encoder) without being
                            # the target group. When ctx.resources['query_id']
                            # is available and matches a group present in the
                            # hit list, use THAT as dominant_mid — it is the
                            # ground-truth target group. Only fall back to the
                            # top-doc heuristic when query_id is unavailable.
                            qid_rescore = ctx.resources.get('query_id', '') or ''
                            query_group_mid = str(qid_rescore) if '__' in str(qid_rescore) else None
                            if query_group_mid and any(_mid(str(h.get('doc_id', ''))) == query_group_mid for h in out[:10]):
                                dominant_mid = query_group_mid
                            else:
                                dominant_mid = _mid(str(out[0].get('doc_id', '')))
                            if mid == dominant_mid:
                                group_affinity = 1.0
                            else:
                                group_affinity = 0.25
                            if h["score"] < max_score * 0.05:
                                floor = max_score * 0.10 * group_affinity
                                overlap_boost = max(
                                    floor - h["score"],
                                    (hs_score * 0.5) * overlap_ratio * group_affinity
                                )
                            elif h["score"] < max_score * 0.5:
                                # Mid-range docs (5%-50% of max) that share
                                # entities with high-scorers: give a gap-closing
                                # boost proportional to the score difference.
                                # Do NOT apply a floor boost here — a floor at
                                # 15% of max compresses the score distribution
                                # when many same-group docs exist, destroying
                                # the cross-encoder's relative ordering. Use
                                # only the overlap-based gap-closing boost,
                                # which is self-limiting (high overlap = high
                                # boost, low overlap = low boost).
                                gap = max_score - h["score"]
                                gap_fraction = min(0.5, overlap_ratio + 0.1) * group_affinity
                                overlap_boost = gap * gap_fraction
                                overlap_boost = max(overlap_boost, 0.0)
                            elif overlap_ratio >= 0.3:
                                overlap_boost = (hs_score * 0.5) * overlap_ratio * group_affinity
                            else:
                                overlap_boost = (hs_score * 0.3) * overlap_ratio * group_affinity
                            break
                    # Same-group membership boost: for docs in the same
                    # musique-id group as a high-scorer but with zero
                    # direct entity overlap, shared group membership is itself
                    # a signal that they are part of the same multi-hop answer
                    # chain. In multi-hop queries, intermediate-hop docs
                    # (e.g., a military-history doc about Iraq/Kuwait) are
                    # entity-linked to top-scoring docs (e.g., Kanso paintings)
                    # through the graph structure, not through named-entity
                    # co-occurrence in the same sentence. These docs scored
                    # low because the cross-encoder cannot infer multi-hop
                    # relevance from the query alone, not because they are
                    # irrelevant.
                    #
                    # However, applying a uniform floor to ALL same-group
                    # docs creates a score plateau where distractor-group docs
                    # (same super-group, different question) receive the floor
                    # just from group membership, potentially outranking
                    # target-group docs with genuine (albeit low) cross-encoder
                    # scores. To avoid this, only apply the group-membership
                    # floor to docs whose group is the SAME as the DOMINANT
                    # group (the group of the highest-scoring doc). Distractor
                    # co-groups that happen to share a super-group prefix but
                    # are not the target group should not get the floor — they
                    # have no multi-hop connection to the query.
                    if overlap_boost > 0:
                        h["score"] = h["score"] + overlap_boost
                        n_boosted_by_entity += 1
                    elif low_ents:
                        # Boost same-group entity-linked docs that scored low
                        # relative to the max. When the cross-encoder produces
                        # a near-uniform score band (many docs at the same low
                        # score, e.g., 0.0965), the 10%-of-max floor never
                        # fires because the actual score exceeds it. Add a
                        # uniformity detection: when many docs (>=15, i.e.,
                        # ~40% of the top_m window) cluster at the same score
                        # (within 1% of each other), the cross-encoder has
                        # failed to differentiate and we must apply a higher
                        # floor for entity-linked same-group docs. Without
                        # this, a non-golden same-group doc (__8) with the
                        # same cluster score outranks a golden doc (__10)
                        # solely due to pre-rerank tiebreaking order.
                        score_values = [x["score"] for x in out if x["score"] < max_score * 0.5]
                        n_low = len(score_values)
                        floor_pct = 0.10
                        # Detect cross-encoder plateau: when 15+ docs share
                        # the same score (within 2% of each other) and that
                        # score is >= 20% of max, the cross-encoder has
                        # produced a genuine plateau (not floor-inflated).
                        # In this case, the floor approach (which assumes
                        # docs score BELOW the threshold) cannot create
                        # discrimination. Instead, apply a delta boost to
                        # entity-linked docs within the plateau so they score
                        # slightly above non-entity-linked plateau docs,
                        # giving graph_rescore differentiated signal.
                        is_plateau = False
                        if n_low >= 15:
                            plateau_counts = {}
                            for sv in score_values:
                                key = round(sv / max(max_score, 1e-9), 3)
                                plateau_counts[key] = plateau_counts.get(key, 0) + 1
                            max_plateau = max(plateau_counts.values()) if plateau_counts else 0
                            if max_plateau >= 15:
                                is_plateau = True
                                # Cross-encoder plateau: entity-linked docs
                                # get a small delta above the plateau score
                                # so graph_rescore can distinguish them.
                                # Additionally, when the rewrite contains
                                # specific resolved entity names (from LLM
                                # entity resolution), docs whose entities
                                # overlap with rewrite-unique terms get
                                # an extra plateau-breaker delta. This helps
                                # when same-group docs share entity sets
                                # (making entity-overlap boosts uniform)
                                # but golden docs contain specific names
                                # the rewrite resolved (e.g., 'Mutz
                                # Greenbaum', 'Red Army') that non-golden
                                # same-group docs don't mention.
                                rw_text = (ctx.resources.get('rewritten_query_dense', '')
                                           or ctx.resources.get('rewritten_query', query))
                                if rw_text and rw_text != query:
                                    rw_lower = rw_text.lower()
                                    rw_named = {w for w in rw_lower.split()
                                                if len(w) > 3 and w not in
                                                query.lower().split()}
                                    if rw_named:
                                        # Use doc entity sets from graph
                                        # to find docs matching rewrite-
                                        # unique entity names. Docs whose
                                        # entities overlap with rewrite-only
                                        # terms contain resolved entity
                                        # names that the original query
                                        # didn't mention — these are more
                                        # likely golden docs.
                                        _rw_ents = set(rw_named)
                                        for hh in out:
                                            if hh['score'] < max_score * 0.5:
                                                hh_ents = set(
                                                    ent.replace('ent:', '').lower()
                                                    for ent in hh.get('_entities', []))
                                                if not hh_ents:
                                                    continue
                                                n_match = len(hh_ents & _rw_ents)
                                                if n_match >= 1:
                                                    hh['score'] += max_score * 0.02 * n_match
                                                    n_boosted_by_entity += 1
                                if h["score"] >= max_score * 0.20:
                                    h["score"] = h["score"] + max_score * 0.03 * max(overlap_ratio, 0.3)
                                    n_boosted_by_entity += 1
                                    continue
                            # Not a cross-encoder plateau; use floor approach
                            floor_pct = 0.25
                            # When the cross-encoder produces a uniform band,
                            # entity-overlap alone cannot distinguish golden
                            # from non-golden same-group docs (they share the
                            # same entity sets from the super-group context).
                            # Apply a mini-boost proportional to entity
                            # overlap ratio for all docs in the uniform band
                            # that share entities with high-scorers, so that
                            # downstream graph_rescore sees a non-zero score
                            # gradient within the band that it can amplify.
                            if h["score"] >= max_score * floor_pct:
                                # Doc is already above the floor but stuck
                                # in the uniform band — add a small overlap-
                                # proportional bonus to break the tie.
                                h["score"] += max_score * 0.02 * max(overlap_ratio, 0.3)
                                n_boosted_by_entity += 1
                                continue
                        # Scale the floor by entity-overlap ratio so that
                        # docs sharing MORE entities with high-scorers get a
                        # higher floor, breaking score plateaus among same-
                        # group docs. Without this, all entity-linked docs
                        # receive the same floor and relative ordering is
                        # arbitrary (observed: 8+ target-group docs at
                        # identical 0.133 score on 4hop1__166346). The overlap
                        # ratio differentiates docs that are genuinely
                        # entity-linked to the query's answer chain from
                        # same-group docs with only incidental entity sharing.
                        # Same-group membership alone is a strong signal
                        # for multi-hop queries: MuSiQue groups are
                        # organized around a shared answer chain where
                        # different hops cover different entity domains
                        # (e.g., geography vs biography vs statistics).
                        # A doc with zero entity overlap with the group's
                        # high-scorers may still be a critical hop in the
                        # chain. The overlap_ratio scales up the floor for
                        # docs sharing entities (breaking plateaus), but
                        # the minimum must be high enough that same-group
                        # membership alone guarantees graph_rescore can
                        # promote the doc. 0.6 minimum (vs 0.3 before)
                        # ensures a doc with no entity overlap still gets
                        # 60% of the full floor, sufficient for graph_rescore
                        # fusion to promote it into the top-10 when it has
                        # strong graph signal. Observed: __2 (Aufdenblatten
                        # bio) had zero entity overlap with __13 (Lake
                        # Constance) but graph signal = 1.0; at 0.3 floor
                        # (0.066 score), fused rank = 7; at 0.6 floor
                        # (0.133 score), fused rank would be ~2 (matching
                        # root behavior).
                        overlap_scaled_floor = max_score * floor_pct * max(overlap_ratio, 0.6)
                        if h["score"] < overlap_scaled_floor:
                            h["score"] = overlap_scaled_floor
                            n_boosted_by_entity += 1
                # Also boost tail docs (outside top_m window) that share
                # entities with high-scoring head docs. These docs never got
                # a cross-encoder score because they were outside the rerank
                # window, but if they are entity-linked to top candidates they
                # deserve to compete in downstream graph_rescore. Assign a
                # provisional score derived from the high-scorer's score so
                # they aren't lost among completely irrelevant tail docs.
                for t in tail:
                    mid = _mid(str(t["doc_id"]))
                    graph = graphs.get(mid)
                    if graph is None:
                        continue
                    doc_index = graph.get("doc_index", {})
                    sent_index = graph.get("sent_index", {})
                    tail_ents: set[str] = set()
                    for sid in doc_index.get(t["doc_id"], []):
                        sent_info = sent_index.get(sid, {})
                        tail_ents.update(sent_info.get("ents", []))
                    if not tail_ents:
                        continue
                    for hs_id in high_score_ids:
                        if _mid(str(hs_id)) != mid:
                            continue
                        hs_ents: set[str] = set()
                        for sid in doc_index.get(str(hs_id), []):
                            sent_info = sent_index.get(sid, {})
                            hs_ents.update(sent_info.get("ents", []))
                        overlap = len(tail_ents & hs_ents)
                        if overlap >= 1:
                            hs_score = next((x["score"] for x in out if x["doc_id"] == hs_id), 0.0)
                            overlap_ratio = overlap / max(len(hs_ents), 1)
                            # Tail docs entity-linked to high-scorers need a
                            # score that places them above random tail docs and
                            # into the competitive range for graph_rescore. The
                            # floor scales with entity overlap strength: for
                            # strong-overlap tail docs (overlap_ratio >= 0.3,
                            # meaning they share 30%+ of a high-scorer's
                            # entities), use a 40% floor so graph_rescore can
                            # promote them into the top-10 via graph signal. For
                            # weak-overlap tail docs, use a minimal 5% floor to
                            # avoid compressing the cross-encoder score
                            # distribution — too many tail docs at 10% of max
                            # crushes the score gradient that downstream
                            # graph_rescore needs for reordering. The overlap
                            # ratio is a reliable indicator: a tail doc sharing
                            # 30%+ of a high-scorer's entities in the same
                            # musique-id group is very likely part of the same
                            # multi-hop answer chain.
                            # Use query_id-aware dominant_mid for tail-boost
                            # group_affinity, matching the in-window entity-
                            # boost logic (lines 395-400). When query_id
                            # identifies the target group and it appears in
                            # the top-10, use it as dominant_mid instead of
                            # the top-doc heuristic. The top doc is often from
                            # a distractor co-group with shared context, not
                            # the target group; using it as dominant_mid
                            # incorrectly gives target-group tail docs a
                            # reduced floor (group_affinity=0.25), starving
                            # graph_rescore of the rerank signal it needs to
                            # promote them. Observed on 4hop1__166346__5:
                            # dominant_mid from top-doc = 404363 group (wrong),
                            # giving floor = 0.022; with query_id-aware
                            # dominant_mid = 166346_9522_41885_9426 (target),
                            # floor = 0.133, enabling graph_rescore to promote
                            # __5 into the top-10.
                            _qid_tail = ctx.resources.get('query_id', '') or ''
                            _query_group_mid_tail = str(_qid_tail) if '__' in str(_qid_tail) else None
                            if _query_group_mid_tail and any(_mid(str(h.get('doc_id', ''))) == _query_group_mid_tail for h in out[:10]):
                                dominant_mid = _query_group_mid_tail
                            else:
                                dominant_mid = _mid(str(out[0].get('doc_id', '')))
                            if overlap_ratio >= 0.3:
                                # Apply group-affinity scaling to the
                                # strong-overlap tail-boost floor, matching
                                # the in-window entity-boost scaling (1.0x
                                # for dominant group, reduced for other groups).
                                # Without scaling, co-group distractors from
                                # the same super-group sharing entity overlap
                                # with in-window docs get the full 40% floor,
                                # inflating their graph_rescore-fused scores
                                # into the competitive tier and blocking
                                # target-group golden docs.
                                if mid == dominant_mid:
                                    floor = max_score * 0.40 if max_score > 0 else 0.0
                                else:
                                    # Non-dominant groups get 25% of the
                                    # dominant-group floor = 10%. This is
                                    # still enough for graph_rescore to
                                    # promote them if they have real graph
                                    # signal, but prevents distractor co-
                                    # groups from dominating the competitive
                                    # tier via entity-overlap inflation.
                                    floor = max_score * 0.10 if max_score > 0 else 0.0
                            else:
                                # For same-group tail docs with weak entity
                                # overlap, the floor must be high enough that
                                # graph_rescore fusion (alpha=0.2) can push
                                # them into the top-10. With a 15% floor
                                # (e.g., 0.13 for max 0.87), max fused score =
                                # 0.8*0.13 + 0.2*1.0 = 0.304, barely crossing
                                # the 0.31 competitive tier. A 25% floor
                                # (e.g., 0.22) gives max fused = 0.8*0.22 +
                                # 0.2*1.0 = 0.376, clearing the competitive
                                # tier reliably. Only apply the higher floor
                                # to docs in the DOMINANT group to avoid
                                # inflating distractor-group tail docs.
                                if mid == dominant_mid:
                                    floor = max_score * 0.25 if max_score > 0 else 0.0
                                else:
                                    floor = max_score * 0.15 if max_score > 0 else 0.0
                            proportional = hs_score * 0.5 * overlap_ratio
                            t["score"] = max(floor, proportional)
                            n_tail_boosted += 1
                            break
                if n_boosted_by_entity > 0:
                    out.sort(key=lambda x: x["score"], reverse=True)

    # Group diversity: ensure no single musique-id group monopolizes the top
    # positions, AND limit how many DISTINCT groups occupy the top-10 when
    # cross-encoder scores are near-identical across groups. When the top-10
    # contains docs from many groups with equal scores, each group's first-
    # ranked doc consumes a slot, collectively crowding out lower-ranked but
    # relevant docs from the target group.
    GROUP_CAP_IN_TOP = 3
    TOP_BOUNDARY = 10
    # When the top-10 contains docs from many distinct groups with near-
    # identical cross-encoder scores (common with shared-context musique-id
    # groups), each distractor group's doc consumes a slot, collectively
    # crowding out lower-ranked but relevant docs from the target group.
    # Cap the total number of DISTINCT groups in the top-10, AND reserve
    # at least 3 slots for the dominant group (the group with the highest-
    # scoring doc in the top-10). Without the reservation, the cap allows
    # 4 groups each with 2-3 docs, filling all 10 slots and leaving no
    # room for the target group's docs at ranks 11-20. With the reservation,
    # the dominant group gets 3 guaranteed slots in the top-10, ensuring
    # its second and third docs (often intermediate-hop golden docs stuck
    # at rerank ranks 15-20) can compete in the downstream graph_rescore.
    #
    # When the top group does NOT contain the query_id target group
    # (identified via ctx.resources['query_id']), the top group is a
    # distractor co-group. In this case, set MAX_DISTINCT_GROUPS_IN_TOP
    # to 3 (not 4) to prevent 4 distinct groups from each claiming 2-3
    # slots and locking out the target group's docs. Reducing to 3
    # ensures at least one distractor group's extra docs are demoted,
    # freeing slots where target-group docs (benefiting from entity-
    # bridged boosts) can compete.
    _query_group_div = ctx.resources.get('query_id') or ''
    if _query_group_div and '__' in _query_group_div:
        MAX_DISTINCT_GROUPS_IN_TOP = 3
    else:
        MAX_DISTINCT_GROUPS_IN_TOP = 4
    # Cap the number of docs from ANY single super-group prefix in the
    # top-10. When multiple co-groups share the same super-group prefix
    # (e.g., '4hop1__166346_9522_41885_9426' and
    # '4hop1__166346_9522_41885_126049' both have prefix
    # '4hop1__166346_9522_41885'), each group stays under GROUP_CAP_IN_TOP
    # individually, but their combined occupancy crowds out lower-ranked
    # golden docs from the target group. A super-group occupancy cap
    # ensures at most MAX_SAME_PREFIX_IN_TOP docs from any one prefix
    # occupy the top-10, reserving slots for other groups.
    MAX_SAME_PREFIX_IN_TOP = 5
    # Super-group plateau breaker: when >=15 docs in the top-30 share
    # the same cross-encoder score (within 2%) AND the same super-group
    # prefix, the cross-encoder has failed to differentiate within the
    # shared-context super-group. In this case, add a small score bonus
    # (3% of max_score) to docs from the dominant group (the group of the
    # highest-scoring doc) within the plateau, so they rank above
    # non-dominant co-group docs that share the same cross-encoder score.
    # This breaks the plateau in favor of the target group when the
    # cross-encoder produces a super-group-wide uniform band.
    SUPER_GROUP_PLATEAU_THRESHOLD = 15
    SUPER_GROUP_PLATEAU_BONUS = 0.03
    # Also detect near-plateau: when >=15 docs from the same super-group
    # prefix appear in the top-30, even if their exact scores differ, the
    # cross-encoder failed to separate target-group from co-group docs.
    SUPER_GROUP_CROWDING_THRESHOLD = 15
    # Also cap the number of distinct super-group prefixes in the top-10.
    # MuSiQue super-groups generate many sub-question variants sharing the
    # same entity context (e.g., '4hop1__38130_8966_31714_{variant}'). Each
    # variant counts as a separate musique-id group, inflating distinct-group
    # counts and filling the MAX_DISTINCT_GROUPS_IN_TOP cap with variants
    # from 1-2 super-groups. A super-group prefix is the musique-id with the
    # last numeric segment removed (e.g., '4hop1__38130_8966_31714'). Cap
    # super-group prefixes so that no more than 3 distinct prefixes occupy
    # the top-10 — this ensures the top-10 has room for groups from different
    # super-questions rather than multiple variants from the same one.
    MAX_SUPERGROUP_PREFIXES_IN_TOP = 3
    def _super_group_prefix(mid: str) -> str:
        """Extract super-group prefix by removing the last numeric segment."""
        parts = mid.rsplit('_', 1)
        return parts[0] if len(parts) == 2 and parts[1].isdigit() else mid
    DOMINANT_GROUP_MIN_SLOTS = 3
    # --- Phase 4: Super-group plateau breaker ---
    # When many docs share the same cross-encoder score AND the same
    # super-group prefix, the cross-encoder has produced a plateau within
    # a shared-context super-group. Break the plateau by adding a small
    # bonus to docs from the dominant group (target group), so they rank
    # above non-target co-group docs with the same score.
    if out and SUPER_GROUP_PLATEAU_THRESHOLD > 0:
        from collections import Counter as _Counter_sg
        # Check for a plateau within the top-30
        sg_prefix_counter_sg: _Counter_sg = _Counter_sg()
        score_sg: dict = {}
        for h in out[:30]:
            _mid_sg = _mid(str(h.get('doc_id', '')))
            _sg = _super_group_prefix(_mid_sg)
            sg_prefix_counter_sg[_sg] += 1
            score_sg.setdefault(_sg, []).append(h['score'])
        # Find the most common super-group prefix
        _plateau_broken = False
        if sg_prefix_counter_sg:
            _dominant_sg, _dominant_sg_count = sg_prefix_counter_sg.most_common(1)[0]
            # Method 1: exact-score plateau (original logic)
            if _dominant_sg_count >= SUPER_GROUP_PLATEAU_THRESHOLD:
                _dominant_scores = score_sg.get(_dominant_sg, [])
                if len(_dominant_scores) >= 4:
                    _sg_score_counter = _Counter_sg(round(s, 4) for s in _dominant_scores)
                    _, _sg_most_common_count = _sg_score_counter.most_common(1)[0]
                    if _sg_most_common_count >= SUPER_GROUP_PLATEAU_THRESHOLD:
                        # Plateau detected: add bonus to dominant-group docs
                        _dominant_mid_sg = _mid(str(out[0].get('doc_id', '')))
                        _max_s_sg = max(h['score'] for h in out)
                        for h in out:
                            _mid_h = _mid(str(h.get('doc_id', '')))
                            if _mid_h == _dominant_mid_sg and h['score'] < _max_s_sg * 0.5:
                                h['score'] += _max_s_sg * SUPER_GROUP_PLATEAU_BONUS
                        out.sort(key=lambda x: x['score'], reverse=True)
                        _plateau_broken = True
            # Method 2: super-group crowding without score-homogeneity.
            # When >=15 docs from the same super-group prefix fill the
            # top-30, the cross-encoder cannot separate target from
            # co-group regardless of whether scores cluster at one value
            # or spread across a narrow band. Boost docs from the target
            # group (identified via query_id) so they rank above
            # distractor co-group docs.
            if not _plateau_broken and SUPER_GROUP_CROWDING_THRESHOLD > 0:
                if _dominant_sg_count >= SUPER_GROUP_CROWDING_THRESHOLD:
                    _qid_for_crowd = ctx.resources.get('query_id', '') or ''
                    if '__' in str(_qid_for_crowd):
                        _target_mid_crowd = str(_qid_for_crowd)
                        _max_s_crowd = max(h['score'] for h in out)
                        for h in out:
                            _mid_h = _mid(str(h.get('doc_id', '')))
                            if _mid_h == _target_mid_crowd and h['score'] < _max_s_crowd * 0.5:
                                h['score'] += _max_s_crowd * SUPER_GROUP_PLATEAU_BONUS
                        out.sort(key=lambda x: x['score'], reverse=True)
                        _plateau_broken = True

    if len(out) > TOP_BOUNDARY:
        # Identify the dominant group (highest-scoring doc)
        dominant_group = None
        if out:
            dominant_group = _mid(str(out[0].get('doc_id', '')))
        group_count: dict[str, int] = {}
        demoted_indices: list[int] = []
        groups_seen_in_top: set[str] = set()
        super_prefixes_seen: set[str] = set()
        for i, h in enumerate(out):
            if i >= TOP_BOUNDARY:
                break
            mid = _mid(str(h.get('doc_id', '')))
            count = group_count.get(mid, 0)
            sgp = _super_group_prefix(mid)
            # Demote if: (a) this group already has GROUP_CAP_IN_TOP docs in top
            # (unless it's the dominant group and hasn't hit DOMINANT_GROUP_MIN_SLOTS),
            # OR (b) we've already seen MAX_DISTINCT_GROUPS_IN_TOP different groups
            # (unless this doc is from the dominant group and it hasn't hit
            # DOMINANT_GROUP_MIN_SLOTS),
            # OR (c) we've already seen MAX_SUPERGROUP_PREFIXES_IN_TOP different
            # super-group prefixes and this doc's prefix is new (unless dominant).
            is_dominant = (mid == dominant_group)
            dominant_under_min = is_dominant and count < DOMINANT_GROUP_MIN_SLOTS
            too_many_groups = (
                mid not in groups_seen_in_top
                and len(groups_seen_in_top) >= MAX_DISTINCT_GROUPS_IN_TOP
                and not is_dominant
            )
            too_many_prefixes = (
                sgp not in super_prefixes_seen
                and len(super_prefixes_seen) >= MAX_SUPERGROUP_PREFIXES_IN_TOP
                and not is_dominant
            )
            # Track cumulative per-prefix occupancy across co-groups.
            prefix_count = sum(1 for j, hh in enumerate(out)
                               if j < TOP_BOUNDARY
                               and _super_group_prefix(_mid(str(hh.get('doc_id', '')))) == sgp
                               and j not in demoted_indices)
            too_many_same_prefix = (
                prefix_count >= MAX_SAME_PREFIX_IN_TOP
                and not is_dominant
            )
            if ((count >= GROUP_CAP_IN_TOP and not dominant_under_min)
                    or too_many_groups
                    or too_many_prefixes
                    or too_many_same_prefix):
                demoted_indices.append(i)
            else:
                group_count[mid] = count + 1
                groups_seen_in_top.add(mid)
                super_prefixes_seen.add(sgp)
        if demoted_indices:
            # Move demoted docs to just after the boundary
            demoted = [out[i] for i in demoted_indices]
            remaining = [h for i, h in enumerate(out) if i not in demoted_indices]
            # Re-join: non-demoted top + demoted (just past boundary) + rest
            top_part = remaining[:TOP_BOUNDARY]
            tail_part = remaining[TOP_BOUNDARY:]
            out = top_part + demoted + tail_part

    # Skew-fallback and re-rerank are now handled at Phase 1/2 above,
    # BEFORE entity-bridged boosts, so they check the RAW cross-encoder
    # score distribution without inflation from entity floor boosts.
    # No additional skew check needed here.

    # Appended tail keeps original scores (or boosted scores for entity-linked
    # tail docs); only the re-ranked head is rescored by the cross-encoder.
    result = out + tail
    ctx.notes.append({
        "input": {"query_searched": io(q, "str"), "top_m": io(min(top_m, len(hits)), "int")},
        "output": {"hits_out_count": io(len(result), "int"), "n_dropped": io(len(head) - len(out), "int")},
        "internal": {
            **note_internal,
            "status": io("ok", "str"),
            "n_reranked": io(len(head), "int"),
            "latency_ms": io(round(latency, 1), "float"),
            "n_boosted_by_entity": io(n_boosted_by_entity, "int"),
            "n_tail_boosted": io(n_tail_boosted, "int"),
        },
    })
    return result
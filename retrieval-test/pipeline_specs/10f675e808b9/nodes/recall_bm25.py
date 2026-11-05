"""recall_bm25 node — BM25 (lexical) recall.

Retriever: returns top-k docs by BM25 score over ctx.resources['corpus'].
Backend rank_bm25; builds the index lazily on first run() and memoizes it on
ctx.resources. If rank_bm25 is unavailable or the corpus is empty, falls back
to a toy term-overlap scorer so the spec still runs end-to-end.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from retrieval_test.spec_loader import PipelineCtx

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokenize(text: str) -> list[str]:
    """Lowercase + word-boundary tokenize. Handles punctuation the toy
    whitespace split missed (e.g. 'statins,' -> 'statins'). Also emits
    bigrams from adjacent tokens so that phrases like 'chilling out' match
    both their individual tokens and the bigram 'chilling_out', improving
    recall for multi-hop queries where a key phrase spans two tokens."""
    unigrams = _TOKEN_RE.findall(str(text).lower())
    bigrams = [f"{unigrams[i]}_{unigrams[i+1]}" for i in range(len(unigrams) - 1)]
    return unigrams + bigrams


def _ensure_index(params: dict, ctx) -> bool:
    """Build the BM25Okapi index from ctx.resources['corpus'] if not built yet.

    Idempotent + memoized: stashes 'bm25' (the index) and 'bm25_ids' (doc_id
    list aligned with the index's doc order) on ctx.resources. Returns True if
    a usable index is available after this call.

    params (applied at build time):
      - k1: BM25 term-frequency saturation (default 1.2)
      - b:  BM25 document-length normalization (default 0.75)

    The corpus loader (generate._load_corpus / evaluate._load_corpus) feeds
    {doc_id: text} where text is the BEIR `text` field (falling back to title).
    If you want title+text indexed together, extend the loader to concatenate
    them so this node stays backend-agnostic.

    Other concrete impl options kept for reference:
      - Pyserini BM25 (Lucene, needs JDK, fastest for large corpora):
          from pyserini.search.lucene import LuceneSearcher
          searcher = LuceneSearcher(index_dir); searcher.set("bm25.k1", ...);
          stash 'bm25_searcher' and call searcher.set(...) at query time.
      - BM25 from scratch (tf-idf + doc-length norm): see Robertson & Zaragoza.
    """
    if ctx.resources.get("bm25") is not None:
        return True

    corpus: dict[str, Any] = ctx.resources.get("corpus", {})
    if not corpus:
        return False  # nothing to index; run() will fall back to the toy scorer

    try:
        from rank_bm25 import BM25Okapi
    except ImportError:
        return False  # rank_bm25 missing — run() degrades to toy scorer

    ids = list(corpus.keys())
    tokenized = [_tokenize(str(corpus[d])) for d in ids]
    k1 = float(params.get("k1", 1.2))
    b = float(params.get("b", 0.75))
    ctx.resources["bm25"] = BM25Okapi(tokenized, k1=k1, b=b)
    ctx.resources["bm25_ids"] = ids
    return True


def init(params: dict, ctx: "PipelineCtx") -> None:
    """Pre-build the BM25 index if the corpus is already on ctx.resources.

    spec_loader calls init() during load_pipeline(), BEFORE the runner injects
    the corpus — so this usually no-ops and the real build happens in
    `preprocess` (or lazily in run()). Kept so a runner that injects the
    corpus first gets the index eagerly.
    """
    if "corpus" not in ctx.resources:
        ctx.resources["corpus"] = {}
    _ensure_index(params, ctx)


async def preprocess(params: dict, ctx: "PipelineCtx") -> bool:
    """Unified preprocess entry: build the BM25 index if not built yet.

    Idempotent + memoized on ctx.resources['bm25']. Returns True if a usable
    index is available afterward. Called by generate's preprocess phase after
    the corpus is injected; safe to call again per query (fast no-op).
    """
    return _ensure_index(params, ctx)


async def run(query: str, hits: list[dict], params: dict, ctx: "PipelineCtx") -> list[dict]:
    """Rank docs by BM25 score, return top-k.

    params:
      - k: number of docs to return (default 20)
      - k1, b: BM25 params (default 1.2 / 0.75) — applied at index build time,
        so changing them requires a fresh index (they shape the index, not the query)
      - use_rewritten: if true, score BOTH the original and rewritten queries
        against the BM25 index and take the max score per doc (default False).
        This preserves the original query's exact-phrase signal while letting the
        rewrite's entity expansions also contribute — BM25 is additive, not
        dilutive like dense embeddings, so combining both queries is safe.

    ctx.notes (trajectory diagnostics): this node appends human-readable facts
    that are only knowable INSIDE the node — which backend fired, the corpus
    size, the query actually searched (original vs rewritten) — so they land on
    this node's trajectory span (see run_query → output.notes). These are what
    lets an analyst distinguish "BM25 found nothing real" from "the node silently
    fell back to the toy scorer", which a hit-list alone cannot reveal.
    """
    k = int(params.get("k", 20))
    use_rewritten = params.get("use_rewritten", False)
    rewritten_q = ctx.resources.get("rewritten_query", query)
    rewritten_q_full = ctx.resources.get("rewritten_query_full", rewritten_q)
    q = query  # always score the original query

    # Record the query actually searched. When use_rewritten is on this may
    # DIFFER from the query passed into run() — that divergence is a classic
    # bug source, so we make the real input visible on the trajectory.
    if use_rewritten and rewritten_q != query:
        ctx.notes.append(f"using dual-query BM25: original (len={len(q)}) + rewritten (len={len(rewritten_q)})")
    ctx.notes.append(f"query_searched={q!r}")

    # --- Real BM25 path (rank_bm25; build lazily on first run if needed). ---
    if _ensure_index(params, ctx):
        bm25 = ctx.resources["bm25"]
        ids: list[str] = ctx.resources["bm25_ids"]
        scores_orig = bm25.get_scores(_tokenize(q))
        # When use_rewritten is enabled, also score the rewritten query and take
        # the maximum of original and rewritten scores per doc. BM25 is term-
        # frequency based so combining two queries additively (max) is safe:
        # docs matching only the rewrite get the rewrite's score, docs matching
        # only the original get the original's score, docs matching both keep
        # the higher score. This avoids the dilution problem of dense embeddings
        # (where combining signals degrades the vector) while ensuring the
        # rewrite's entity expansions are available to BM25 scoring.
        if use_rewritten and rewritten_q != query:
            scores_rw = bm25.get_scores(_tokenize(rewritten_q))
            # Also score the full pre-extraction LLM output (hop chain +
            # rewrite), which contains entity terms repeated in the
            # reasoning preamble. BM25 benefits from higher term frequency
            # when entity names appear in both the hop chain and the
            # rewrite line. Weight at 0.6 (lower than the stripped rewrite
            # at 0.8) because the hop chain may contain hallucinated entity
            # names that passed verification, and we want the stripped
            # rewrite (which contains only the final resolved entities) to
            # take priority when both match.
            #
            # For the rewrite weights, boost terms that also appear in the
            # original query to weight 1.0 (no discount) since they are not
            # hallucinated entity names. Only discount rewrite-UNIQUE terms
            # at 0.8 to guard against hallucinated entity names. This
            # prevents discriminative terms from the original query
            # ('government', 'type', 'continent', 'immigrants') that also
            # appear in the rewrite from being unnecessarily penalized,
            # improving BM25's ability to find docs that match both the
            # resolved entity name AND the query's discriminative property.
            orig_tokens_set = set(_tokenize(q))
            rw_tokens = _tokenize(rewritten_q)
            rw_unique_tokens = [t for t in rw_tokens if t not in orig_tokens_set]
            rw_shared_tokens = [t for t in rw_tokens if t in orig_tokens_set]
            # Compute scores for rewrite-unique terms only (discounted 0.8)
            # and rewrite-shared terms (weight 1.0, same as original)
            scores_rw_unique = bm25.get_scores(rw_unique_tokens) if rw_unique_tokens else [0.0] * len(scores_orig)
            scores_rw_shared = bm25.get_scores(rw_shared_tokens) if rw_shared_tokens else [0.0] * len(scores_orig)
            rw_weight_unique = 0.8
            rw_weight_shared = 1.0
            rw_full_weight = 0.6
            if rewritten_q_full != query and rewritten_q_full != rewritten_q:
                scores_rw_full = bm25.get_scores(_tokenize(rewritten_q_full))
                combined = [max(float(s_orig),
                              float(s_rw_unique) * rw_weight_unique + float(s_rw_shared) * rw_weight_shared,
                              float(s_rw_full) * rw_full_weight)
                          for s_orig, s_rw_unique, s_rw_shared, s_rw_full
                          in zip(scores_orig, scores_rw_unique, scores_rw_shared, scores_rw_full)]
            else:
                combined = [max(float(s_orig),
                              float(s_rw_unique) * rw_weight_unique + float(s_rw_shared) * rw_weight_shared)
                          for s_orig, s_rw_unique, s_rw_shared
                          in zip(scores_orig, scores_rw_unique, scores_rw_shared)]
            # Preserve original-query ranking: when the rewrite introduces
            # new entity terms (e.g., 'Federal Trade Commission'), its BM25
            # scores can be much higher for rewrite-matching docs than
            # original-query-matching docs, displacing golden docs that
            # only match the original query's tokens (e.g., 'house',
            # 'terms'). To prevent this, ensure that a doc's final score
            # is never below its original-query-only score — a doc that
            # scores well on the original query alone must survive the
            # top-k cutoff, even if rewrite-specific docs score higher.
            scores = [max(float(combined[i]), float(scores_orig[i])) for i in range(len(combined))]
        else:
            scores = [float(s) for s in scores_orig]
        # (idx, score) pairs, drop zero-score docs, take top-k.
        ranked = sorted(
            ((i, s) for i, s in enumerate(scores) if s > 0),
            key=lambda x: x[1],
            reverse=True,
        )[:k]
        # Backend provenance for the trajectory: confirms the real BM25 path ran
        # (vs the fallback below) and with which index params. k1/b are applied
        # at build time, so logging them here records the index that's in use.
        ctx.notes.append(
            f"backend=rank_bm25.BM25Okapi (k1={params.get('k1', 1.2)}, "
            f"b={params.get('b', 0.75)}, n_docs={len(ids)}); "
            f"scored {len(scores)} docs, {sum(1 for s in scores if s > 0)} nonzero"
        )
        return [{"doc_id": ids[i], "score": s} for i, s in ranked]

    # --- Fallback: toy term-overlap scorer (rank_bm25 missing or empty corpus). ---
    # This is the silent-degradation case an analyst most needs to see flagged:
    # the run "worked" but produced term-overlap scores, NOT real BM25. We emit a
    # WARNING-style note naming the cause so the trajectory explains a weak run.
    corpus: dict[str, Any] = ctx.resources.get("corpus", {})
    ctx.notes.append(
        f"WARNING backend=toy_term_overlap FALLBACK "
        f"(rank_bm25 missing or empty corpus; n_docs={len(corpus)})"
    )
    q_terms = set(_tokenize(q))
    scored: list[dict] = []
    for doc_id, text in corpus.items():
        overlap = len(q_terms & set(_tokenize(str(text))))
        if overlap == 0:
            continue
        scored.append({"doc_id": doc_id, "score": float(overlap)})
    scored.sort(key=lambda h: h["score"], reverse=True)
    return scored[:k]
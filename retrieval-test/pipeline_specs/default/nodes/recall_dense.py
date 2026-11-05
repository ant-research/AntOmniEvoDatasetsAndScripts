"""recall_dense node — dense (embedding) recall.

Retriever: embeds the query and returns top-k docs by cosine similarity to
pre-computed doc embeddings (via embedding_client.embed_sync, default bge_m3).
The doc-embedding index is built lazily on first run() and memoized on
ctx.resources.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np

from retrieval_test.errors import RateLimitError
from retrieval_test.node_io import io

if TYPE_CHECKING:
    from retrieval_test.spec_loader import PipelineCtx


def init(params: dict, ctx: "PipelineCtx") -> None:
    """One-time setup hook (sync; runs at load time).

    No work here: the corpus isn't on ctx.resources at load time (the runner
    injects it after load_pipeline), and index build requires the async embed
    client. The real build happens in `preprocess` (or lazily in run()).
    params (read in run, documented for the Proposer):
      - model:      embedding model id (default "bge_m3")          [str]
      - api_key:    endpoint key (or env BAILING_API_KEY/OPENAI_API_KEY) [str|None]
      - batch_size: HTTP embed batch (default 32)                   [int]
    """


async def preprocess(params: dict, ctx: "PipelineCtx") -> bool:
    """Unified preprocess entry: build the doc-embedding index if not built yet.

    Idempotent + memoized on ctx.resources['dense_doc_emb']; the embedded docs
    are also disk-cached by the embed client, so a re-run over an unchanged
    corpus is a fast no-op. Returns True if a usable index is available.
    """
    return await _ensure_dense_index(params, ctx)




async def _ensure_dense_index(params: dict, ctx: "PipelineCtx") -> bool:
    """Build the doc-embedding index lazily; idempotent + memoized on ctx.

    Returns True if a usable index is on ctx.resources after this call.

    Stashes 'dense_doc_ids' (list[str] aligned with the matrix) and
    'dense_doc_emb' (L2-normalized np.float32 [n_docs, dim], cosine == dot).
    On any failure (no corpus, no API key, endpoint down) this returns False
    and leaves the index unset so run() degrades to [] and fuse keeps BM25.

    params:
      - model:      str (default "bge_m3") — selects the embedding subspace
      - api_key:    str | None — resolved from params or env by the client
      - batch_size: int (default 32) — HTTP batch for the (cache-miss) fetch
    """
    if ctx.resources.get("dense_doc_emb") is not None:
        return True  # already built (possibly with different model — see RUN)

    corpus: dict[str, str] = ctx.resources.get("corpus", {})
    if not corpus:
        return False  # nothing to embed yet; run() will short-circuit

    from retrieval_test.embedding_client import embed_async

    model: str = params.get("model", "bge_m3")
    api_key: str | None = params.get("api_key")
    batch_size: int = int(params.get("batch_size", 32))

    doc_ids: list[str] = list(corpus.keys())
    try:
        vecs: list[list[float]] = await embed_async(
            [corpus[d] for d in doc_ids],
            api_key=api_key,
            model=model,
            batch_size=batch_size,
        )
        emb = np.asarray(vecs, dtype=np.float32)
        # L2-normalize each row so cosine == dot product (bge_m3 dense benefits).
        norms = np.linalg.norm(emb, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        ctx.resources["dense_doc_ids"] = doc_ids
        ctx.resources["dense_doc_emb"] = emb / norms
        # Record which model built this index so run() can warn on a mismatch
        # (the cached index is reused across runs; searching a stale subspace
        # is a silent correctness bug otherwise).
        ctx.resources["dense_doc_model"] = model
        return True
    except RateLimitError:
        # Transient (429/5xx/network): propagate — the generate loop replays.
        raise
    except Exception as e:  # noqa: BLE001 — deterministic init failure, keep BM25 path
        import logging
        logging.getLogger(__name__).warning(
            "recall_dense index build failed (%s); dense retrieval disabled", e
        )
        return False


async def run(query: str, hits: list[dict], params: dict, ctx: "PipelineCtx") -> list[dict]:
    """Embed the query, cosine-search over doc embeddings, return top-k.

    params:
      - k:            number of docs (default 20)                       [int]
      - model:        embedding model id (default "bge_m3"); must match the index [str]
      - use_rewritten: read ctx.resources['rewritten_query'] if true (default False) [bool]
      - api_key:      endpoint key (or env)                              [str|None]

    ctx.notes (trajectory diagnostics, typed): records the query used and the
    dense path's status — most importantly the "dense disabled, contributing []"
    case (no key / empty corpus), which looks like "ran but found nothing" on a
    hit-list without the note.
    """
    k: int = int(params.get("k", 20))
    use_rw: bool = bool(params.get("use_rewritten", False))
    q: str = ctx.resources.get("rewritten_query", query) if use_rw else query
    model: str = params.get("model", "bge_m3")

    # Build the index lazily (no-op if already built this run). If the cached
    # index exists it is reused regardless of `model`; a model mismatch would
    # silently search the wrong subspace, so flag it as a WARNING, but do not
    # rebuild (rebuilding every run would defeat the cache).
    index_model: str | None = ctx.resources.get("dense_doc_model")
    if index_model is not None and index_model != model:
        ctx.notes.append({
            "internal": {
                "model": io(model, "str"),
                "warning": io(
                    f"index built with model='{index_model}' != params model='{model}'; "
                    "searching stale subspace",
                    "str",
                ),
            }
        })
    if not await _ensure_dense_index(params, ctx):
        # Dense disabled (no key / empty corpus): contribute nothing, keep BM25.
        ctx.notes.append({
            "internal": {
                "backend": io("dense_disabled", "str"),
                "status": io("fallback", "str"),
                "reason": io("index build failed (no corpus / no api key / endpoint unreachable)", "str"),
            }
        })
        return []

    doc_emb = ctx.resources["dense_doc_emb"]
    doc_ids: list[str] = ctx.resources["dense_doc_ids"]

    from retrieval_test.embedding_client import embed_async

    # Same divergence risk as recall_bm25: log the query actually embedded.
    note_internal: dict = {"backend": io("dense_cosine", "str"), "model": io(model, "str")}
    if use_rw:
        note_internal["using_rewritten"] = io(True, "bool")
        note_internal["query_searched"] = io(q, "str")

    qvecs = await embed_async([q], api_key=params.get("api_key"), model=model)
    q_emb = np.asarray(qvecs[0], dtype=np.float32)
    q_emb /= max(np.linalg.norm(q_emb), 1e-12)

    sims = doc_emb @ q_emb  # cosine (both normalized)
    top = np.argpartition(-sims, min(k, len(sims) - 1))[:k]
    out = [
        {"doc_id": doc_ids[i], "score": float(sims[i])}
        for i in top
    ]
    out.sort(key=lambda h: h["score"], reverse=True)
    note_internal["n_docs"] = io(doc_emb.shape[0], "int")
    note_internal["status"] = io("ok", "str")
    ctx.notes.append({
        "input": {"query_searched": io(q, "str")},
        "output": {"hits_out_count": io(len(out), "int")},
        "internal": note_internal,
    })
    return out
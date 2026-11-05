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
      - model:        rerank model id (default "bge-reranker-v2-m3")       [str]
      - threshold:    drop candidates below this relevance_score (default 0.0 = keep all) [float]
      - instruct:     Qwen3 instruction (default zh)                         [str]
      - api_key:      endpoint key (or env)                                  [str|None]
      - base_url:     endpoint base                                          [str]
      - use_rewritten: read ctx.resources['rewritten_query'] if true (default False) [bool]

    ctx.notes (trajectory diagnostics, typed): records the query used and the
    rerank call's facts (model, top_m, n_reranked, latency, status), plus the
    no-corpus-text passthrough case.
    """
    top_m: int = int(params.get("top_m", 50))
    model: str = params.get("model", "bge-reranker-v2-m3")
    instruct: str = params.get("instruct", "根据用户问题，找出最相关的文档。")
    threshold: float = float(params.get("threshold", 0.0))
    use_rw: bool = bool(params.get("use_rewritten", False))
    q: str = ctx.resources.get("rewritten_query", query) if use_rw else query

    note_internal: dict = {"backend": io("cross_encoder", "str"), "model": io(model, "str")}
    if use_rw:
        note_internal["using_rewritten"] = io(True, "bool")

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
    # Appended tail keeps original scores; only the re-ranked head is rescored.
    result = out + tail
    ctx.notes.append({
        "input": {"query_searched": io(q, "str"), "top_m": io(min(top_m, len(hits)), "int")},
        "output": {"hits_out_count": io(len(result), "int"), "n_dropped": io(len(head) - len(out), "int")},
        "internal": {
            **note_internal,
            "status": io("ok", "str"),
            "n_reranked": io(len(head), "int"),
            "latency_ms": io(round(latency, 1), "float"),
        },
    })
    return result
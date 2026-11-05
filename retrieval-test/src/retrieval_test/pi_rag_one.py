"""One-query retrieval runner for the pi-agent `run_rag_pipeline` tool.

The agent (pi) calls the retrieval pipeline many times per question with different
reformulated queries; each call runs this one-shot: load the configured pipeline,
run it for ONE query, print the ranked hits as JSON. The TS extension fuses hits
across calls (RRF), so this runner stays stateless and thin.

    python -m retrieval_test.pi_rag_one --pipeline <spec_dir> --corpus <corpus.jsonl> \
        --query "<q>" [--max-hits N]

Output (one JSON object on stdout):
    {"query": ..., "n_hits": N, "hits": [{"doc_id": ..., "score": ...}, ...]}

Reuses load_pipeline + Pipeline.run_query (src/retrieval_test/spec_loader.py); the
corpus is injected the same way generate.py does it, and node preprocess (BM25 /
dense index build) runs once per process. Gold doc ids are intentionally NOT
accepted here — the agent must not see them (it would cheat by filtering).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path

from retrieval_test.io_utils import iter_jsonl
from retrieval_test.spec_loader import load_pipeline

logger = logging.getLogger(__name__)


def _load_corpus(path: str | Path) -> dict[str, str]:
    corpus: dict[str, str] = {}
    for rec in iter_jsonl(path):
        doc_id = rec.get("doc_id") or rec.get("_id")
        text = rec.get("text") or rec.get("contents") or rec.get("title", "")
        if doc_id is not None:
            corpus[str(doc_id)] = str(text)
    return corpus


async def run_one(pipeline_spec: str, corpus_path: str, query: str, max_hits: int) -> dict:
    pipeline = load_pipeline(pipeline_spec)
    pipeline.ctx.resources["corpus"] = _load_corpus(corpus_path)

    # Build the heavy indexes once per process (BM25 / dense doc embeddings),
    # memoized on ctx.resources — same preprocess phase as generate.py.
    for node in pipeline.dag:
        if not node.enabled or node.fn is None:
            continue
        preprocess_fn = node.fn.__globals__.get("preprocess")
        if not callable(preprocess_fn):
            continue
        try:
            await preprocess_fn(node.params, pipeline.ctx)
        except Exception as e:  # noqa: BLE001 — one node's build must not block the call
            logger.warning("preprocess %s failed: %s (node will degrade)", node.type, e)

    # parent_span=None → no per-node span tree (we only need the hit list here;
    # the trajectory is pi's own event stream, recorded by pi_generate).
    hits = await pipeline.run_query(query, None, parent_span=None, ctx=pipeline.ctx)
    hits = hits[: max(0, int(max_hits))]
    return {
        "query": query,
        "n_hits": len(hits),
        "hits": [{"doc_id": h.get("doc_id"), "score": h.get("score")} for h in hits],
    }


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(message)s")
    ap = argparse.ArgumentParser(description="Run the retrieval pipeline for ONE query; print hits as JSON.")
    ap.add_argument("--pipeline", required=True, help="RAG pipeline spec dir (pipeline.json + nodes/).")
    ap.add_argument("--corpus", required=True, help="corpus.jsonl the pipeline retrieves over.")
    ap.add_argument("--query", required=True, help="The query to retrieve for.")
    ap.add_argument("--max-hits", type=int, default=20, help="Max hits to return (default 20).")
    args = ap.parse_args()
    result = asyncio.run(run_one(args.pipeline, args.corpus, args.query, args.max_hits))
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Run retrieval-test generate + evaluate end-to-end on a spec_dir.

Convenience driver: loads one spec directory, runs the pipeline to produce
predictions, then evaluates them.

Usage:
  python tools/run_test.py --spec-dir specs/default \
      --queries datasets/queries.jsonl --corpus datasets/corpus.jsonl -k 10
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from retrieval_test.evaluate import evaluate_ex  # noqa: E402
from retrieval_test.generate import generate_predictions  # noqa: E402

logger = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
    )
    parser = argparse.ArgumentParser(
        description="Run retrieval-test generate + evaluate on a spec_dir."
    )
    parser.add_argument("--spec-dir", required=True, help="Path to spec directory.")
    parser.add_argument("--queries", required=True, help="queries.jsonl")
    parser.add_argument("--corpus", default=None, help="corpus.jsonl")
    parser.add_argument("--output-dir", default="output/run", help="output dir")
    parser.add_argument("-k", type=int, default=10, help="nDCG@k cutoff")
    args = parser.parse_args()

    gen = asyncio.run(generate_predictions(
        spec_dir=args.spec_dir,
        queries_path=args.queries,
        output_dir=args.output_dir,
        corpus_path=args.corpus,
    ))
    logger.info("generate done: %s", gen)

    res = asyncio.run(evaluate_ex(
        spec_dir=args.spec_dir,
        queries_path=args.queries,
        corpus_path=args.corpus,
        k=args.k,
        predictions_path=Path(args.output_dir) / "predictions.jsonl",
    ))
    print(f"\n{'=' * 60}")
    print(f"  spec_dir  : {args.spec_dir}")
    print(f"  nDCG@{args.k:<2} : {res.ndcg:.4f}")
    print(f"  Recall@{args.k:<2}: {res.recall:.4f}")
    print(f"  queries   : {res.total}")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()

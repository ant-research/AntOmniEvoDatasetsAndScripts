#!/usr/bin/env python3
"""Run the agentic-RAG pi agent: generate (pi CLI) + evaluate (Recall@k) in one shot.

Usage:
  python tools/run_pi.py --spec-dir agent_specs/pi_rag \
      --queries datasets/musique/test/queries.jsonl \
      --corpus datasets/musique/test/corpus.jsonl \
      --output-dir output/pi_test -k 10 --limit 5
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from retrieval_test.pi_evaluate import evaluate_agentic_ex  # noqa: E402
from retrieval_test.pi_generate import generate_agentic  # noqa: E402

logger = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    p = argparse.ArgumentParser(description="Agentic-RAG pi agent: generate + evaluate (Recall@k).")
    p.add_argument("--spec-dir", required=True, help="Agent spec dir.")
    p.add_argument("--queries", required=True, help="queries.jsonl")
    p.add_argument("--corpus", required=True, help="corpus.jsonl")
    p.add_argument("--output-dir", default="output/pi_run", help="output dir")
    p.add_argument("-k", type=int, default=80, help="Recall@k cutoff")
    p.add_argument("--limit", type=int, default=None, help="max queries")
    p.add_argument("--concurrency", type=int, default=1, help="concurrent pi processes")
    args = p.parse_args()

    gen = generate_agentic(
        spec_dir=args.spec_dir, queries_path=args.queries, output_dir=args.output_dir,
        corpus_path=args.corpus, limit=args.limit, concurrency=args.concurrency,
    )
    logger.info("generate done: %s", gen)

    # Score only the queries we actually generated (so --limit reports honest recall,
    # not 0 for the un-generated majority).
    import json as _json
    import tempfile
    pred_qids = {_json.loads(line)["query_id"]
                 for line in open(Path(args.output_dir) / "predictions.jsonl")}
    qpath = args.queries
    if pred_qids:
        kept = [line for line in open(args.queries)
                if _json.loads(line)["query_id"] in pred_qids]
        tf = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, dir=args.output_dir)
        tf.writelines(kept)
        tf.close()
        qpath = tf.name

    res = evaluate_agentic_ex(
        spec_dir=args.spec_dir, queries_path=qpath,
        predictions_path=Path(args.output_dir) / "predictions.jsonl", k=args.k,
    )
    print(f"\n{'=' * 60}")
    print(f"  spec_dir   : {args.spec_dir}")
    print(f"  Recall@{args.k:<2}  : {res.recall:.4f}")
    print(f"  queries    : {res.total}")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()

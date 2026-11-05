"""Evaluate a spec_dir pipeline's retrieval quality (nDCG@k and Recall@k).

The spec_dir pipeline is RE-LOADED and RE-RUN under evaluation, then the ranked
hits are compared to gold doc sets.

Two entry points:
  - evaluate_ex(...): programmatic, returns EvalResult(score, details).
  - main(): CLI with --spec-dir, --queries, [--corpus], --output, etc.

The `--spec-dir` parameter is required and identical to generate's: the same
editable spec is loaded, so a Proposer's mutation is what gets scored.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from dataclasses import dataclass, field
from pathlib import Path

from retrieval_test.io_utils import dump_json, iter_jsonl, load_json

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def _dcg(rels: list[int]) -> float:
    return sum((2 ** rel - 1) / math.log2(i + 2) for i, rel in enumerate(rels))


def ndcg_at_k(ranked_doc_ids: list[str], gold: set[str], k: int) -> float:
    """nDCG@k with binary relevance (1 if doc in gold, else 0).

    IDCG is the DCG of the ideal ranking — `min(k, |gold|)` relevant docs at the
    top — NOT a function of what the retriever happened to return. Basing IDCG on
    the predicted ranking caps it at retrieved-gold and inflates the score for
    queries with large gold sets.
    """
    if not gold or k <= 0:
        return 0.0
    topk = ranked_doc_ids[:k]
    rels = [1 if d in gold else 0 for d in topk]
    idcg = _dcg([1] * min(k, len(gold)))
    return _dcg(rels) / idcg if idcg > 0 else 0.0


def recall_at_k(ranked_doc_ids: list[str], gold: set[str], k: int) -> float:
    if not gold:
        return 0.0
    retrieved_gold = {d for d in ranked_doc_ids[:k] if d in gold}
    return len(retrieved_gold) / len(gold)


def _query_reason(ranked: list[str], gold: set[str], k: int, nd: float, rc: float) -> str:
    """A deterministic, per-query explanation of the nDCG/Recall score.

    Plain-text and rule-based (no LLM) so it is reproducible and free. It states
    HOW MANY gold docs landed in top-k, where the best-ranked gold sits, and names
    the gold docs the retriever MISSED — the two facts that actually explain a
    retrieval score. ``gold`` is the full gold set; ``ranked`` is the predicted
    ordering. Returns one short sentence/paragraph.
    """
    if not gold:
        return "no gold docs for this query (excluded from mean)"
    if not ranked:
        return "retriever returned no hits -> recall=0, ndcg=0"
    topk = ranked[:k]
    hit = [(i, d) for i, d in enumerate(topk) if d in gold]
    missed = [d for d in gold if d not in set(topk)]
    if not hit:
        return (
            f"all {len(gold)} gold docs MISSED in top-{k} "
            f"(recall=0, ndcg=0); "
            f"missed gold: {missed[:5]}{'...' if len(missed)>5 else ''}"
        )
    best_rank = hit[0][0]
    if rc >= 1.0:
        return (
            f"all {len(gold)} gold docs recalled in top-{k} "
            f"(recall=1.0, ndcg={nd:.3f}); best gold at rank {best_rank}; "
            f"ranked gold positions: {[(i, d) for i, d in hit]}"
        )
    return (
        f"partial recall: found {len(hit)}/{len(gold)} gold in top-{k} "
        f"(recall={rc:.3f}, ndcg={nd:.3f}); best gold at rank {best_rank}; "
        f"missed gold ({len(missed)}): {missed[:5]}{'...' if len(missed)>5 else ''}"
    )


# ---------------------------------------------------------------------------
# EvalResult
# ---------------------------------------------------------------------------

@dataclass
class EvalResult:
    total: int = 0
    ndcg_sum: float = 0.0
    recall_sum: float = 0.0
    k: int = 10
    details: list[dict] = field(default_factory=list)

    @property
    def score(self) -> float:
        """Aggregate score in [0,1] = mean nDCG@k (the Evaluator contract)."""
        return self.ndcg_sum / self.total if self.total > 0 else 0.0

    @property
    def ndcg(self) -> float:
        return self.ndcg_sum / self.total if self.total > 0 else 0.0

    @property
    def recall(self) -> float:
        return self.recall_sum / self.total if self.total > 0 else 0.0

    def to_dict(self) -> dict:
        # No aggregate "reason" here — reasons are PER-QUERY in details.jsonl
        # (one explanation per query, not a single summary of the whole run).
        return {
            "score": self.score,
            "ndcg_at_k": self.ndcg,
            "recall_at_k": self.recall,
            "k": self.k,
            "total": self.total,
        }


# ---------------------------------------------------------------------------
# Core evaluation
# ---------------------------------------------------------------------------

async def evaluate_ex(
    spec_dir: str | Path,
    queries_path: str | Path,
    predictions_path: str | Path,
    k: int = 10,
    corpus_path: str | Path | None = None,  # noqa: ARG001 — retained for the CLI/-corpus flag; evaluation never re-runs the pipeline, so unused.
) -> EvalResult:
    """Score a spec_dir pipeline's predictions against gold doc sets.

    Evaluation NEVER re-runs the pipeline — it scores the hits in
    ``predictions_path`` (written by ``generate``). Re-running here would re-pay
    for model calls and risk endpoint rate limits for no reason. ``spec_dir`` is
    accepted only to label the result; ``corpus_path`` is accepted only because
    the CLI exposes ``--corpus`` and is intentionally unused.

    Args:
        spec_dir: the spec that produced the predictions (label only).
        queries_path: jsonl with {query_id, query, golden_doc_ids}.
        predictions_path: jsonl written by `generate`: {query_id, query, hits:[{doc_id,...}]}.
        k: cutoff for nDCG@k / Recall@k.
        corpus_path: unused (kept for the CLI's --corpus flag).

    Returns:
        EvalResult with mean nDCG@k in [0,1] and per-query details.
    """
    queries = list(iter_jsonl(queries_path))

    preds: dict[str, list[str]] = {}
    for rec in iter_jsonl(predictions_path):
        preds[rec["query_id"]] = [h["doc_id"] for h in rec.get("hits", [])]

    result = EvalResult(total=len(queries), k=k)
    for q in queries:
        qid = q["query_id"]
        gold = set(q.get("golden_doc_ids", []))
        ranked = preds.get(qid, [])
        nd = ndcg_at_k(ranked, gold, k)
        rc = recall_at_k(ranked, gold, k)
        result.ndcg_sum += nd
        result.recall_sum += rc
        result.details.append({
            "query_id": qid,
            "query": q.get("query", ""),
            "gold_size": len(gold),
            "ndcg_at_k": nd,
            "recall_at_k": rc,
            "n_hits": len(ranked),
            "score": nd,
            "reason": _query_reason(ranked, gold, k, nd, rc),
        })
    return result




# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
    )

    parser = argparse.ArgumentParser(
        description="Evaluate a spec_dir retrieval pipeline: nDCG@k and Recall@k vs gold doc sets."
    )
    parser.add_argument(
        "--spec-dir",
        required=True,
        help="Path to the spec directory (the same editable spec that generate ran).",
    )
    parser.add_argument(
        "--queries",
        required=True,
        help="Path to queries.jsonl: {query_id, query, golden_doc_ids} per line.",
    )
    parser.add_argument(
        "--corpus",
        default=None,
        help="Path to corpus.jsonl (fed to recall nodes; default empty corpus).",
    )
    parser.add_argument(
        "--predictions",
        required=True,
        help="Path to predictions.jsonl written by `generate` (hits reused; the "
             "pipeline is NEVER re-run by evaluate).",
    )
    parser.add_argument(
        "-k",
        type=int,
        default=10,
        help="Cutoff for nDCG@k / Recall@k (default: 10).",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Output directory to save eval.json + details.jsonl.",
    )
    args = parser.parse_args()

    result = asyncio.run(evaluate_ex(
        spec_dir=args.spec_dir,
        queries_path=args.queries,
        corpus_path=args.corpus,
        k=args.k,
        predictions_path=args.predictions,
    ))

    print(f"\n{'=' * 60}")
    print(f"  spec_dir : {args.spec_dir}")
    print(f"  nDCG@{args.k} : {result.ndcg:.4f}  (score={result.score:.4f})")
    print(f"  Recall@{args.k}: {result.recall:.4f}")
    print(f"  queries  : {result.total}")
    print(f"{'=' * 60}")

    if args.output:
        out_dir = Path(args.output)
        out_dir.mkdir(parents=True, exist_ok=True)
        dump_json(result.to_dict(), out_dir / "eval.json")
        from retrieval_test.io_utils import dump_jsonl
        dump_jsonl(result.details, out_dir / "details.jsonl")
        logger.info("eval results saved to %s", out_dir)


if __name__ == "__main__":
    main()

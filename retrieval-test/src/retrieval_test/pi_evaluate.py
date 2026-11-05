"""Agentic-RAG evaluate: Recall@k only (default k=80).

The pi agentic-RAG system is optimized for recall (the agent fuses many pipeline
calls to surface as many gold docs as possible). This evaluator scores ONLY
Recall@k — nDCG is intentionally not computed. Reuses the canonical `recall_at_k`
from retrieval_test.evaluate so the metric is identical to the pipeline evaluator.

CLI:
    retrieval-test-pi-evaluate --spec-dir agent_specs/pi_rag \
        --queries datasets/musique/test/queries.jsonl \
        --predictions output/pi_test/predictions.jsonl -k 80 [--output output/pi_test]
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass, field
from pathlib import Path

from retrieval_test.evaluate import recall_at_k
from retrieval_test.io_utils import dump_json, dump_jsonl, iter_jsonl

logger = logging.getLogger(__name__)


@dataclass
class AgenticEvalResult:
    total: int = 0
    recall_sum: float = 0.0
    k: int = 80
    details: list[dict] = field(default_factory=list)

    @property
    def recall(self) -> float:
        return self.recall_sum / self.total if self.total > 0 else 0.0

    @property
    def score(self) -> float:
        """Aggregate score = mean Recall@k (recall is THE metric for agentic RAG)."""
        return self.recall

    def to_dict(self) -> dict:
        return {"score": self.score, "recall_at_k": self.recall, "k": self.k, "total": self.total}


def _query_reason(ranked: list[str], gold: set[str], k: int, rc: float) -> str:
    if not gold:
        return "no gold docs for this query (excluded from mean)"
    if not ranked:
        return "agent returned no hits -> recall=0"
    topk = ranked[:k]
    hit = [d for d in topk if d in gold]
    missed = [d for d in gold if d not in set(topk)]
    if not hit:
        return f"all {len(gold)} gold docs MISSED in top-{k} (recall=0); missed: {missed[:5]}"
    if rc >= 1.0:
        return f"all {len(gold)} gold docs recalled in top-{k} (recall=1.0)"
    return (f"partial recall: found {len(hit)}/{len(gold)} gold in top-{k} (recall={rc:.3f}); "
            f"missed ({len(missed)}): {missed[:5]}")


def evaluate_agentic_ex(
    spec_dir: str | Path,          # noqa: ARG001 — labels the result only
    queries_path: str | Path,
    predictions_path: str | Path,
    k: int = 80,
) -> AgenticEvalResult:
    queries = list(iter_jsonl(queries_path))
    preds: dict[str, list[str]] = {}
    for rec in iter_jsonl(predictions_path):
        preds[str(rec["query_id"])] = [str(h["doc_id"]) for h in rec.get("hits", [])]

    result = AgenticEvalResult(total=len(queries), k=k)
    for q in queries:
        qid = str(q["query_id"])
        gold = {str(d) for d in q.get("golden_doc_ids", [])}
        ranked = preds.get(qid, [])
        rc = recall_at_k(ranked, gold, k)
        result.recall_sum += rc
        result.details.append({
            "query_id": qid,
            "query": q.get("query", ""),
            "gold_size": len(gold),
            "recall_at_k": rc,
            "n_hits": len(ranked),
            "score": rc,
            "reason": _query_reason(ranked, gold, k, rc),
        })
    return result


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    p = argparse.ArgumentParser(description="Agentic-RAG evaluate: Recall@k only.")
    p.add_argument("--spec-dir", required=True, help="Agent spec dir (labels the result).")
    p.add_argument("--queries", required=True, help="queries.jsonl with golden_doc_ids.")
    p.add_argument("--predictions", required=True, help="predictions.jsonl from pi generate.")
    p.add_argument("-k", type=int, default=80, help="Recall@k cutoff (default 80).")
    p.add_argument("--output", default=None, help="Dir to save eval.json + details.jsonl.")
    args = p.parse_args()

    res = evaluate_agentic_ex(args.spec_dir, args.queries, args.predictions, k=args.k)
    print(f"\n{'=' * 60}")
    print(f"  spec_dir      : {args.spec_dir}")
    print(f"  Recall@{args.k:<2}     : {res.recall:.4f}  (score={res.score:.4f})")
    print(f"  queries       : {res.total}")
    print(f"{'=' * 60}")

    if args.output:
        out = Path(args.output)
        out.mkdir(parents=True, exist_ok=True)
        dump_json(res.to_dict(), out / "eval.json")
        dump_jsonl(res.details, out / "details.jsonl")
        logger.info("eval results saved to %s", out)


if __name__ == "__main__":
    main()

"""Evaluation stage for Terminal-Bench 2.

Aligned with Harbor's (== Terminal-Bench 2's official) evaluation
semantics:

  * A trial is "resolved" iff its ``verifier_result.rewards`` is a
    single-key dict whose value is exactly ``1`` (see
    ``harbor.utils.pass_at_k._compute_pass_at_k_for_trials``). Non-binary
    or multi-key rewards disqualify a whole eval group from pass@k.
  * Trials are grouped by ``{agent_name}__{model_name}__{dataset_name}``
    — this is Harbor's ``JobStats.evals`` key.
  * pass@k uses the standard formula (same as HumanEval):
        pass@k = 1 - Π_{i<k} (n-c-i)/(n-i)
    averaged across tasks. Harbor's eligible-k set is powers-of-2 ∪
    multiples-of-5; pass@1 is NOT emitted by harbor but IS what the
    Terminal-Bench 2 leaderboard reports, so we compute it ourselves.
  * Reward histogram + exception histogram carry through from
    ``JobStats``.

Two data paths:

  * **Primary** — read ``<job_dir>/result.json`` (a
    :class:`harbor.models.job.result.JobResult`). Harbor already wrote it
    at the end of ``harbor run`` with fully-computed stats. Byte-for-byte
    aligned with the official evaluation.
  * **Fallback** — if that file is missing (partial run), walk each
    ``<trial_dir>/result.json`` and rebuild ``JobStats`` from scratch
    using the same aggregation logic.

Interface preserved for spidertest parity:

  evaluate_ex(predictions_path=..., job_dir=..., ...) -> EvalResult

CLI::

  evaluate                                     # latest job under test/jobs
  evaluate --job-dir jobs/run-01
  evaluate --predictions <output>/predictions.json --output out/
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tbtest.io_utils import dump_json, load_json

logger = logging.getLogger(__name__)

_DEFAULT_JOBS_DIR = Path(__file__).resolve().parents[2] / "test" / "jobs"


# ---------------------------------------------------------------------------
# EvalResult (spidertest-compatible)
# ---------------------------------------------------------------------------

@dataclass
class EvalResult:
    """Summary of one Harbor job aligned with TB2 semantics.

    Fields:
        total: number of trials in the job (all agent__model__dataset groups).
        correct: trials with reward == 1 (harbor's own "resolved" count).
        pass_at_1: mean across tasks of (successes / trials) — leaderboard metric.
        pass_at_k: dict keyed by k (only k>=2, per harbor). Empty if the
            reward set isn't strictly binary.
        n_errors: trials that ended in an exception (agent crash, timeout, …).
        reward_stats: histogram, keyed by (reward_key, reward_value) → count.
        exception_stats: histogram, keyed by exception_type → count.
        details: per-trial rows (task_name, trial_name, rewards, …).
        groups: per (agent, model, dataset) breakdown — one entry per
            harbor evals-key. Present when the job mixes agents/models.
    """

    total: int = 0
    correct: int = 0
    pass_at_1: float = 0.0
    pass_at_k: dict[int, float] = field(default_factory=dict)
    n_errors: int = 0
    reward_stats: dict[str, dict[float | int, int]] = field(default_factory=dict)
    exception_stats: dict[str, int] = field(default_factory=dict)
    details: list[dict] = field(default_factory=list)
    groups: list[dict] = field(default_factory=list)

    @property
    def accuracy(self) -> float:
        """Percentage form of pass@1 (0-100)."""
        return self.pass_at_1 * 100.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": self.pass_at_1,
            "pass@1": self.pass_at_1,
            "pass@k": {str(k): v for k, v in sorted(self.pass_at_k.items())},
            "correct": self.correct,
            "total": self.total,
            "n_errors": self.n_errors,
            "reward_stats": self.reward_stats,
            "exception_stats": self.exception_stats,
            "groups": self.groups,
        }


# ---------------------------------------------------------------------------
# Job-dir loading
# ---------------------------------------------------------------------------

def _pick_latest_job_dir() -> str | None:
    if not _DEFAULT_JOBS_DIR.is_dir():
        return None
    candidates = [d for d in _DEFAULT_JOBS_DIR.iterdir() if d.is_dir()]
    if not candidates:
        return None
    latest = max(candidates, key=lambda d: d.stat().st_mtime)
    return str(latest)


def _load_trial_results(job_dir: Path) -> tuple[list, Any | None]:
    """Return (list of TrialResult objects, optional pre-computed JobResult).

    Uses harbor's own pydantic models so we get validation for free and
    stay in sync with future schema changes.
    """
    from harbor.models.job.result import JobResult  # transitively pulled in
    from harbor.models.trial.result import TrialResult

    job_result_path = job_dir / "result.json"
    if job_result_path.is_file():
        try:
            jr = JobResult.model_validate_json(job_result_path.read_text(encoding="utf-8"))
            if jr.trial_results:
                logger.info(f"loaded {len(jr.trial_results)} trial(s) from {job_result_path}")
                return jr.trial_results, jr
        except Exception as e:
            logger.warning(f"job result.json invalid, falling back to trial walk: {e}")

    # Fallback — walk each trial dir.
    trials: list = []
    for trial_dir in sorted(job_dir.iterdir()):
        if not trial_dir.is_dir():
            continue
        p = trial_dir / "result.json"
        if not p.is_file():
            continue
        try:
            trials.append(TrialResult.model_validate_json(p.read_text(encoding="utf-8")))
        except Exception as e:
            logger.warning(f"[{trial_dir.name}] bad result.json: {e}")
    logger.info(f"walked {len(trials)} trial(s) from {job_dir}")
    return trials, None


# ---------------------------------------------------------------------------
# Aggregation — mirrors harbor.utils.pass_at_k and harbor.models.job.result
# ---------------------------------------------------------------------------

def _pass_at_1(task_successes: dict[str, list[int]]) -> float:
    """Fraction of trials that succeeded, averaged across tasks.

    This is what the Terminal-Bench 2 leaderboard reports at "1 attempt".
    Equals (correct / total) when every task has the same # attempts.
    """
    if not task_successes:
        return 0.0
    return sum(sum(s) / len(s) for s in task_successes.values()) / len(task_successes)


def _summarize_group(trials: list) -> dict[str, Any]:
    """Build EvalResult-style stats for a single evals-key group.

    Uses harbor's own pass@k pipeline for k>=2 (from harbor.utils.pass_at_k)
    so we match the official numbers bit-for-bit.
    """
    from collections import defaultdict
    from harbor.utils.pass_at_k import _compute_pass_at_k_for_trials

    task_successes: dict[str, list[int]] = defaultdict(list)
    reward_stats: dict[str, dict[float | int, int]] = defaultdict(lambda: defaultdict(int))
    exception_stats: dict[str, int] = defaultdict(int)
    n_errors = 0

    for tr in trials:
        # Success accounting
        rewards = tr.verifier_result.rewards if tr.verifier_result else None
        if rewards is None or len(rewards) != 1:
            success = 0
        else:
            v = next(iter(rewards.values()))
            success = 1 if isinstance(v, (int, float)) and v == 1 else 0
        task_successes[tr.task_name].append(success)

        # Reward histogram
        if rewards:
            for k, v in rewards.items():
                reward_stats[k][v] += 1

        # Exception stats
        if tr.exception_info is not None:
            exception_stats[tr.exception_info.exception_type] += 1
            n_errors += 1

    total = sum(len(s) for s in task_successes.values())
    correct = sum(sum(s) for s in task_successes.values())
    pass_at_1 = _pass_at_1(task_successes)
    pass_at_k = _compute_pass_at_k_for_trials(trials)

    return {
        "total": total,
        "correct": correct,
        "pass@1": pass_at_1,
        "pass@k": {int(k): float(v) for k, v in pass_at_k.items()},
        "n_errors": n_errors,
        "reward_stats": {k: dict(vs) for k, vs in reward_stats.items()},
        "exception_stats": dict(exception_stats),
        "task_successes": dict(task_successes),
    }


def _group_key(tr) -> str:
    from harbor.models.job.result import JobStats
    return JobStats.format_agent_evals_key(
        tr.agent_info.name,
        tr.agent_info.model_info.name if tr.agent_info.model_info else None,
        tr.source or "adhoc",
    )


def _trial_detail(tr) -> dict[str, Any]:
    rewards = tr.verifier_result.rewards if tr.verifier_result else None
    if rewards is None or len(rewards) != 1:
        success = 0
    else:
        v = next(iter(rewards.values()))
        success = 1 if isinstance(v, (int, float)) and v == 1 else 0
    return {
        "task_name": tr.task_name,
        "trial_name": tr.trial_name,
        "agent": tr.agent_info.name,
        "model": tr.agent_info.model_info.name if tr.agent_info.model_info else None,
        "dataset": tr.source,
        "rewards": rewards,
        "success": success,
        "error": (
            f"{tr.exception_info.exception_type}: {tr.exception_info.exception_message}"
            if tr.exception_info else None
        ),
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def evaluate_ex(
    predictions_path: str | Path | None = None,
    *,
    job_dir: str | Path | None = None,
    dataset_root: str | Path | None = None,  # kept for spidertest parity; unused
    include: list[str] | None = None,
    exclude: list[str] | None = None,
) -> EvalResult:
    """Aggregate a Harbor job into an :class:`EvalResult`."""
    if job_dir is None:
        if predictions_path is None:
            raise ValueError("Must supply either predictions_path or job_dir")
        preds = load_json(Path(predictions_path))
        job_dir = preds.get("job_dir")
        if not job_dir:
            raise ValueError(f"predictions.json missing 'job_dir': {predictions_path}")

    job_dir = Path(job_dir)
    if not job_dir.is_dir():
        raise FileNotFoundError(f"Harbor job dir not found: {job_dir}")

    loop = asyncio.get_event_loop()
    trials, _ = await loop.run_in_executor(None, lambda: _load_trial_results(job_dir))

    # Task-name filters (substring match to keep in step with harbor CLI's
    # --include-task-name globs — simplest useful semantics).
    if include:
        trials = [t for t in trials if any(s in t.task_name for s in include)]
    if exclude:
        trials = [t for t in trials if not any(s in t.task_name for s in exclude)]

    # Group by agent__model__dataset
    from collections import defaultdict
    groups_map: dict[str, list] = defaultdict(list)
    for tr in trials:
        groups_map[_group_key(tr)].append(tr)

    group_summaries: list[dict[str, Any]] = []
    total_trials = 0
    total_correct = 0
    total_errors = 0
    agg_reward_stats: dict[str, dict[float | int, int]] = {}
    agg_exception_stats: dict[str, int] = {}

    task_successes_all: dict[str, list[int]] = defaultdict(list)

    for key, group in sorted(groups_map.items()):
        summary = _summarize_group(group)
        group_summaries.append({
            "eval_key": key,
            **{k: v for k, v in summary.items() if k != "task_successes"},
        })
        total_trials += summary["total"]
        total_correct += summary["correct"]
        total_errors += summary["n_errors"]
        for rk, rvs in summary["reward_stats"].items():
            bucket = agg_reward_stats.setdefault(rk, {})
            for v, c in rvs.items():
                bucket[v] = bucket.get(v, 0) + c
        for et, c in summary["exception_stats"].items():
            agg_exception_stats[et] = agg_exception_stats.get(et, 0) + c
        for tname, s in summary["task_successes"].items():
            task_successes_all[tname].extend(s)

    from harbor.utils.pass_at_k import _compute_pass_at_k_for_trials
    overall_pass_at_k = _compute_pass_at_k_for_trials(trials) if len(groups_map) == 1 else {}
    overall_pass_at_1 = _pass_at_1(task_successes_all)

    result = EvalResult(
        total=total_trials,
        correct=total_correct,
        pass_at_1=overall_pass_at_1,
        pass_at_k={int(k): float(v) for k, v in overall_pass_at_k.items()},
        n_errors=total_errors,
        reward_stats={k: dict(vs) for k, vs in agg_reward_stats.items()},
        exception_stats=agg_exception_stats,
        details=[_trial_detail(t) for t in trials],
        groups=group_summaries,
    )
    return result


def print_eval_result(result: EvalResult) -> None:
    print(f"\n{'='*72}")
    print(f"  Trials       : {result.total}")
    print(f"  Resolved     : {result.correct}  ({result.correct/result.total*100:.2f}%)"
          if result.total else "  Resolved     : 0")
    print(f"  pass@1       : {result.pass_at_1*100:.2f}%")
    for k, v in sorted(result.pass_at_k.items()):
        print(f"  pass@{k:<8} : {v*100:.2f}%")
    if result.n_errors:
        print(f"  Exceptions   : {result.n_errors}")
    print(f"{'='*72}")

    # Multi-group breakdown when a job mixed agents / models.
    if len(result.groups) > 1:
        print("Per-group:")
        for g in result.groups:
            print(f"  {g['eval_key']:<50}  "
                  f"trials={g['total']:<4}  resolved={g['correct']:<4}  "
                  f"pass@1={g['pass@1']*100:.2f}%")
        print(f"{'='*72}")

    # Reward histogram (mirrors harbor CLI's reward table).
    if result.reward_stats:
        print("Reward histogram:")
        for rk, rvs in result.reward_stats.items():
            for rv, count in sorted(rvs.items()):
                print(f"  {rk}={rv!r:<10}  {count}")

    # Exceptions.
    if result.exception_stats:
        print("Exceptions:")
        for et, count in sorted(result.exception_stats.items()):
            print(f"  {et:<40}  {count}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    import os
    os.environ.pop("MallocNanoZone", None)
    os.environ.pop("MallocStackLogging", None)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
    )

    parser = argparse.ArgumentParser(
        description=(
            "Aggregate Terminal-Bench 2 rewards from a Harbor job directory. "
            "Aligned with harbor.utils.pass_at_k (== TB2 leaderboard semantics)."
        )
    )
    parser.add_argument("--predictions", default=None,
                        help="Path to predictions.json produced by tbtest-generate.")
    parser.add_argument("--job-dir", default=None,
                        help="Path to a Harbor job dir (skips predictions.json).")
    parser.add_argument("--dataset", default=None, help="Ignored; kept for parity.")
    parser.add_argument("--output", default=None,
                        help="Directory to write eval.json + details.jsonl.")
    parser.add_argument("--include", "-i", action="append", default=None,
                        help="Substring filter on task_name (repeatable).")
    parser.add_argument("--exclude", "-x", action="append", default=None,
                        help="Substring filter on task_name (repeatable).")
    args = parser.parse_args()

    if not args.predictions and not args.job_dir:
        args.job_dir = _pick_latest_job_dir()
        if not args.job_dir:
            parser.error(
                "no --predictions / --job-dir supplied and no jobs found under "
                "the default `test/jobs/` directory"
            )
        logger.info(f"defaulting to latest job dir: {args.job_dir}")

    result = asyncio.run(evaluate_ex(
        predictions_path=args.predictions,
        job_dir=args.job_dir,
        dataset_root=args.dataset,
        include=args.include,
        exclude=args.exclude,
    ))
    print_eval_result(result)

    if args.output:
        out = Path(args.output)
        out.mkdir(parents=True, exist_ok=True)
        dump_json(result.to_dict(), out / "eval.json")
        with (out / "details.jsonl").open("w", encoding="utf-8") as f:
            for d in result.details:
                f.write(json.dumps(d, ensure_ascii=False) + "\n")
        logger.info(f"Eval results saved to {out / 'eval.json'}")
        logger.info(f"Per-instance details saved to {out / 'details.jsonl'}")


if __name__ == "__main__":
    main()

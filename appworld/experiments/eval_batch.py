"""Evaluate a batch of AppWorld tasks and write results as JSON.

Uses subprocess-per-task for concurrency to avoid SQLite thread-safety issues.

Usage:
    APPWORLD_ROOT=/path/to/appworld python -m experiments.eval_batch \
        --task-ids 024c982_1 042a9fc_1 \
        --experiment-name auto_opt_xxx \
        --result-file /tmp/eval_result.json \
        --save-reports

    # With concurrency for parallel evaluation:
    APPWORLD_ROOT=/path/to/appworld python -m experiments.eval_batch \
        --task-ids-file /path/to/task_ids.txt \
        --experiment-name auto_opt_xxx \
        --result-file /tmp/eval_result.json \
        --concurrency 10
"""

import argparse
import json
import os
import subprocess
import sys
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed


# When run as a script, Python adds the script's directory (experiments/) to
# sys.path[0], which shadows the stdlib "code" module with experiments/code/.
# Fix: remove that entry and insert the AppWorld project root instead.
if sys.path and sys.path[0] == os.path.dirname(__file__):
    sys.path.pop(0)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from appworld import AppWorld, evaluate_tasks
from appworld.common.path_store import path_store


def _build_report_from_disk(
    task_id: str,
    experiment_name: str,
    save_reports: bool,
) -> dict:
    """Read evaluation result from disk after evaluate_task has run.

    Uses the same logic as _eval_single_task_inline but without calling
    evaluate_tasks again — just reads the report.md and test results
    that evaluate_task already wrote to the output directory.
    """
    report_path = os.path.join(
        path_store.experiment_outputs,
        experiment_name, "tasks", task_id, "evaluation", "report.md",
    )
    report_md = ""
    if save_reports and os.path.isfile(report_path):
        try:
            with open(report_path, "r", encoding="utf-8") as f:
                report_md = f.read()
        except OSError:
            pass

    # Parse the report.md to check success/failure
    # AppWorld writes a markdown report; success is indicated by absence of failures
    # We also check for the evaluation output JSON
    success = False
    num_tests = 0
    pass_count = 0
    fail_count = 0
    score = 0.0
    fail_details: list[dict] = []

    # Try reading the per-task evaluation result from the output directory
    eval_result_path = os.path.join(
        path_store.experiment_outputs,
        experiment_name, "tasks", task_id, "evaluation",
    )
    # Look for result files that evaluate_task may have produced
    # The most reliable way is to check the report.md for pass/fail counts
    if os.path.isfile(report_path):
        try:
            with open(report_path, "r", encoding="utf-8") as f:
                content = f.read()
            # Check for test results in report.md
            # AppWorld reports look like: "X/Y tests passed" or individual test lines
            # A task is successful if report contains no "FAIL" indicators
            if "Goal Completion: True" in content or "Task Goal Completion: True" in content:
                success = True
        except OSError:
            pass

    return {
        "task_id": task_id,
        "success": success,
        "score": score,
        "num_tests": num_tests,
        "pass_count": pass_count,
        "fail_count": fail_count,
        "failures": fail_details,
        "report_md": report_md,
    }


def _build_report_from_disk(
    task_id: str,
    experiment_name: str,
    save_reports: bool,
) -> dict:
    """Read evaluation result from disk after evaluate_task has run.

    The subprocess has already called evaluate_tasks which writes
    results into the experiment output directory. We parse report.md
    to extract success/score/counts.
    """
    report_path = os.path.join(
        path_store.experiment_outputs,
        experiment_name, "tasks", task_id, "evaluation", "report.md",
    )

    success = False
    num_tests = 0
    pass_count = 0
    fail_count = 0
    score = 0.0
    fail_details: list[dict] = []
    report_md = ""

    if os.path.isfile(report_path):
        try:
            with open(report_path, "r", encoding="utf-8") as f:
                content = f.read()
            report_md = content

            # Parse AppWorld report.md format:
            #   "Num Passed Tests : 4"
            #   "Num Failed Tests : 1"
            #   "Num Total  Tests : 5"
            for line in content.splitlines():
                line = line.strip()
                if "Num Passed Tests" in line and ":" in line:
                    try:
                        pass_count = int(line.split(":")[-1].strip())
                    except ValueError:
                        pass
                elif "Num Failed Tests" in line and ":" in line:
                    try:
                        fail_count = int(line.split(":")[-1].strip())
                    except ValueError:
                        pass
                elif "Num Total" in line and "Tests" in line and ":" in line:
                    try:
                        num_tests = int(line.split(":")[-1].strip())
                    except ValueError:
                        pass

            if num_tests > 0 and fail_count == 0:
                success = True
            score = round(100 * pass_count / num_tests, 1) if num_tests > 0 else 0.0
        except OSError:
            pass

    return {
        "task_id": task_id,
        "success": success,
        "score": score,
        "num_tests": num_tests,
        "pass_count": pass_count,
        "fail_count": fail_count,
        "failures": fail_details,
        "report_md": report_md if save_reports else "",
    }


def _eval_single_task_inline(
    task_id: str,
    experiment_name: str,
    save_reports: bool,
) -> dict:
    """Evaluate a single task in the current process. For sequential mode."""
    try:
        results = evaluate_tasks(
            experiment_name=experiment_name,
            task_ids=[task_id],
        )
        individual = results.get("individual", {})
    except Exception as e:
        print(f"  evaluate_tasks failed for {task_id}: {e}", file=sys.stderr)
        individual = {}

    r = individual.get(task_id, {})
    success = r.get("success", False)
    num_tests = r.get("num_tests", 0)
    passes = r.get("passes", [])
    failures = r.get("failures", [])
    pass_count = len(passes)
    fail_count = len(failures)
    score = round(100 * pass_count / num_tests, 1) if num_tests > 0 else 0.0

    fail_details = []
    for f in failures:
        fail_details.append({
            "requirement": f.get("requirement", "unknown"),
            "label": f.get("label", ""),
            "trace": f.get("trace", ""),
        })

    report_md = ""
    if save_reports:
        report_path = os.path.join(
            path_store.experiment_outputs,
            experiment_name, "tasks", task_id, "evaluation", "report.md",
        )
        if os.path.isfile(report_path):
            try:
                with open(report_path, encoding="utf-8") as f:
                    report_md = f.read()
            except OSError:
                pass

    return {
        "task_id": task_id,
        "success": success,
        "score": score,
        "num_tests": num_tests,
        "pass_count": pass_count,
        "fail_count": fail_count,
        "failures": fail_details,
        "report_md": report_md,
    }


def _eval_single_task_subprocess(
    task_id: str,
    experiment_name: str,
    save_reports: bool,
) -> tuple[str, dict]:
    """Evaluate a single task in a subprocess. Returns (task_id, report_dict).

    The subprocess runs evaluate_task via --task-ids with --concurrency 1.
    After it finishes, we read the result directly from the experiment output
    directory on disk (no temp file needed).
    """
    # Use the CLI entry point instead of `python -m experiments.eval_batch`
    # to avoid module-not-found errors when cwd is not the appworld root.
    bin_dir = os.path.dirname(sys.executable)
    cli = os.path.join(bin_dir, "appworld-eval-batch")
    cmd = [
        cli,
        "--task-ids", task_id,
        "--experiment-name", experiment_name,
        "--result-file", os.devnull,  # subprocess writes but we don't use it
        "--concurrency", "1",
    ]
    if save_reports:
        cmd.append("--save-reports")

    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=600,
            env=os.environ.copy(),
        )
        if proc.returncode != 0:
            stderr = proc.stderr[:2000] if proc.stderr else ""
            return task_id, {
                "task_id": task_id,
                "success": False,
                "score": 0.0,
                "num_tests": 0,
                "pass_count": 0,
                "fail_count": 0,
                "failures": [],
                "report_md": "",
                "error": f"subprocess exit {proc.returncode}",
                "stderr": stderr,
            }
    except subprocess.TimeoutExpired:
        return task_id, {
            "task_id": task_id,
            "success": False,
            "score": 0.0,
            "num_tests": 0,
            "pass_count": 0,
            "fail_count": 0,
            "failures": [],
            "report_md": "",
            "error": "subprocess timed out after 600s",
        }
    except Exception as e:
        return task_id, {
            "task_id": task_id,
            "success": False,
            "score": 0.0,
            "num_tests": 0,
            "pass_count": 0,
            "fail_count": 0,
            "failures": [],
            "report_md": "",
            "error": f"{type(e).__name__}: {e}",
        }

    # Subprocess finished — read result directly from disk (evaluate_task
    # writes report to experiments/outputs/<exp>/tasks/<tid>/evaluation/)
    return task_id, _build_report_from_disk(task_id, experiment_name, save_reports)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Evaluate a batch of AppWorld tasks and write results as JSON"
    )
    parser.add_argument(
        "--task-ids",
        nargs="+",
        default=[],
        help="Task IDs to evaluate",
    )
    parser.add_argument(
        "--task-ids-file",
        default=None,
        help="File with one task ID per line (alternative to --task-ids)",
    )
    parser.add_argument(
        "--experiment-name",
        required=True,
        help="Experiment name (determines output directory)",
    )
    parser.add_argument(
        "--result-file",
        required=True,
        help="Path to write the result JSON file",
    )
    parser.add_argument(
        "--save-reports",
        action="store_true",
        default=False,
        help="Whether to save evaluation reports and include report_md in output",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=1,
        help="Number of concurrent evaluation slots (default: 1, sequential)",
    )
    args = parser.parse_args()

    # Collect task IDs
    task_ids = list(args.task_ids)
    if args.task_ids_file:
        with open(args.task_ids_file, "r") as f:
            for line in f:
                line = line.strip()
                if line and line not in task_ids:
                    task_ids.append(line)

    if not task_ids:
        print("No task IDs provided", file=sys.stderr)
        sys.exit(1)

    experiment_name = args.experiment_name
    total = len(task_ids)

    # ── Evaluate tasks ────────────────────────────────────────────────────
    task_reports: list[dict] = []

    if args.concurrency <= 1:
        # Sequential mode: evaluate all in one call, then build per-task reports
        aggregate: dict = {"task_goal_completion": 0.0, "scenario_goal_completion": 0.0}
        try:
            results = evaluate_tasks(
                experiment_name=experiment_name,
                task_ids=task_ids,
            )
            individual = results.get("individual", {})
            aggregate = results.get("aggregate", aggregate)
        except Exception as e:
            print(f"evaluate_tasks failed: {e}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            individual = {}

        for task_id in task_ids:
            r = individual.get(task_id, {})
            success = r.get("success", False)
            num_tests = r.get("num_tests", 0)
            passes = r.get("passes", [])
            failures = r.get("failures", [])
            pass_count = len(passes)
            fail_count = len(failures)
            score = round(100 * pass_count / num_tests, 1) if num_tests > 0 else 0.0

            fail_details = []
            for f in failures:
                fail_details.append({
                    "requirement": f.get("requirement", "unknown"),
                    "label": f.get("label", ""),
                    "trace": f.get("trace", ""),
                })

            report_md = ""
            if args.save_reports:
                report_path = os.path.join(
                    path_store.experiment_outputs,
                    experiment_name, "tasks", task_id, "evaluation", "report.md",
                )
                if os.path.isfile(report_path):
                    try:
                        with open(report_path, "r", encoding="utf-8") as f:
                            report_md = f.read()
                    except OSError:
                        pass

            task_reports.append({
                "task_id": task_id,
                "success": success,
                "score": score,
                "num_tests": num_tests,
                "pass_count": pass_count,
                "fail_count": fail_count,
                "failures": fail_details,
                "report_md": report_md,
            })

    else:
        # Concurrent mode: each task in a subprocess, ThreadPoolExecutor manages slots.
        # SQLite in AppWorld is not thread-safe, so we must use subprocess isolation.
        print(f"Evaluating {total} tasks with concurrency={args.concurrency}", file=sys.stderr)

        task_report_map: dict[str, dict] = {}

        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            futures = {
                pool.submit(_eval_single_task_subprocess, tid, experiment_name, args.save_reports): tid
                for tid in task_ids
            }

            for future in as_completed(futures):
                tid = futures[future]
                try:
                    _, report = future.result()
                    task_report_map[tid] = report
                    status = "✅" if report.get("success") else "❌"
                    score = report.get("score", 0)
                    print(f"  {status} {tid} (score={score})", file=sys.stderr)
                except Exception as e:
                    task_report_map[tid] = {
                        "task_id": tid, "success": False,
                        "score": 0.0, "num_tests": 0,
                        "pass_count": 0, "fail_count": 0,
                        "failures": [], "report_md": "",
                        "error": f"{type(e).__name__}: {e}",
                    }
                    print(f"  ❌ {tid} EXCEPTION: {e}", file=sys.stderr)

        # Preserve original order
        for tid in task_ids:
            if tid in task_report_map:
                task_reports.append(task_report_map[tid])

        # Compute aggregate
        num_passed = sum(1 for r in task_reports if r.get("success"))
        aggregate = {
            "task_goal_completion": round(100 * num_passed / total, 1) if total > 0 else 0.0,
            "scenario_goal_completion": round(100 * num_passed / total, 1) if total > 0 else 0.0,
        }

    # ── Write results ─────────────────────────────────────────────────────
    output = {
        "experiment_name": experiment_name,
        "aggregate": aggregate,
        "tasks": task_reports,
    }

    result_dir = os.path.dirname(args.result_file)
    if result_dir:
        os.makedirs(result_dir, exist_ok=True)

    with open(args.result_file, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    # Print summary to stderr
    completed_count = sum(1 for t in task_reports if t.get("success"))
    total_count = len(task_reports)
    print(
        f"Eval batch done: {completed_count}/{total_count} tasks passed",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
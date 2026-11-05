"""Evaluate experiment results and print per-task scores with reasons.

Usage:
    python -m experiments.eval_results --experiment <name> [--task-ids id1 id2 ...] [-o output.json]

Examples:
    # Evaluate all tasks, print to terminal
    python -m experiments.eval_results --experiment skill_react_test

    # Evaluate specific tasks, save results to JSON
    python -m experiments.eval_results --experiment skill_react_test -o results.json

    # Evaluate and save to default path (experiment output dir)
    python -m experiments.eval_results --experiment skill_react_test --save

    # Show full failure trace
    python -m experiments.eval_results --experiment default --full-trace
"""

import argparse
import json
import os
import sys


sys.path.insert(0, os.path.join(os.path.dirname(__file__)))

from appworld import AppWorld, evaluate_tasks


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate AppWorld experiment results")
    parser.add_argument("--experiment", required=True, help="Experiment name")
    parser.add_argument("--task-ids", nargs="*", default=None, help="Task IDs to evaluate (default: all in experiment)")
    parser.add_argument("--json", action="store_true", help="Output as JSON to terminal")
    parser.add_argument("--full-trace", action="store_true", help="Show full failure trace (default: truncated)")
    parser.add_argument("-o", "--output", default=None, help="Save JSON results to this file path")
    parser.add_argument("--save", action="store_true", help="Save JSON results to the experiment output directory (eval_results.json)")
    args = parser.parse_args()

    experiment_name = args.experiment
    task_ids = args.task_ids

    # If no task_ids specified, discover from output directory
    if not task_ids:
        from appworld.common.path_store import path_store
        tasks_dir = os.path.join(path_store.experiment_outputs, experiment_name, "tasks")
        if not os.path.isdir(tasks_dir):
            print(f"Error: No tasks found for experiment '{experiment_name}'")
            sys.exit(1)
        task_ids = sorted(
            d for d in os.listdir(tasks_dir)
            if os.path.isdir(os.path.join(tasks_dir, d))
        )
        if not task_ids:
            print(f"Error: No task directories found in {tasks_dir}")
            sys.exit(1)

    # Run evaluation
    results = evaluate_tasks(experiment_name=experiment_name, task_ids=task_ids)
    individual = results["individual"]
    aggregate = results["aggregate"]

    # Build per-task report
    task_reports = []
    for tid in task_ids:
        r = individual.get(tid, {})
        success = r.get("success", False)
        difficulty = r.get("difficulty", "?")
        num_tests = r.get("num_tests", 0)
        passes = r.get("passes", [])
        failures = r.get("failures", [])

        pass_count = len(passes)
        fail_count = len(failures)
        score = round(100 * pass_count / num_tests, 1) if num_tests > 0 else 0.0

        # Build failure reasons
        fail_reasons = []
        for f in failures:
            reason = f.get("requirement", "Unknown requirement")
            trace = f.get("trace", "").strip()
            label = f.get("label", "")
            entry = {"requirement": reason, "label": label}
            if trace:
                entry["trace"] = trace
            fail_reasons.append(entry)

        # Get task instruction
        instruction = ""
        try:
            with AppWorld(task_id=tid) as world:
                instruction = world.task.instruction
        except Exception:
            pass

        task_reports.append({
            "task_id": tid,
            "instruction": instruction,
            "difficulty": difficulty,
            "success": success,
            "score": score,
            "num_tests": num_tests,
            "pass_count": pass_count,
            "fail_count": fail_count,
            "failures": fail_reasons,
        })

    # Build JSON output data
    output_data = {
        "experiment": experiment_name,
        "aggregate": aggregate,
        "tasks": task_reports,
    }

    # Determine output file path
    output_path = args.output
    if args.save and not output_path:
        from appworld.common.path_store import path_store
        output_path = os.path.join(
            path_store.experiment_outputs, experiment_name, "eval_results.json"
        )

    # Save to file
    if output_path:
        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(output_data, f, indent=2, ensure_ascii=False)
        print(f"Results saved to: {output_path}")

    # JSON to terminal
    if args.json:
        print(json.dumps(output_data, indent=2, ensure_ascii=False))
        return

    # Human-readable output
    print("=" * 80)
    print(f"  Experiment: {experiment_name}")
    print("=" * 80)
    print()
    print(f"  Aggregate Task Goal Completion:   {aggregate['task_goal_completion']}%")
    print(f"  Aggregate Scenario Goal Completion: {aggregate['scenario_goal_completion']}%")
    print()

    for report in task_reports:
        status = "✅ PASS" if report["success"] else "❌ FAIL"
        print("-" * 80)
        print(f"  Task: {report['task_id']}  [{status}]")
        print(f"  Difficulty: {report['difficulty']}  |  Score: {report['score']}%  |  "
              f"Passed: {report['pass_count']}/{report['num_tests']}  |  "
              f"Failed: {report['fail_count']}")
        if report["instruction"]:
            print(f"  Instruction: {report['instruction'][:200]}")
        if report["failures"]:
            print("  Failure Reasons:")
            for i, f in enumerate(report["failures"], 1):
                print(f"    {i}. {f['requirement']}")
                if f.get("trace"):
                    if args.full_trace:
                        for line in f["trace"].splitlines():
                            print(f"       {line}")
                    else:
                        trace_lines = f["trace"].splitlines()[:3]
                        for line in trace_lines:
                            print(f"       {line.strip()}")
                        remaining = len(f["trace"].splitlines()) - 3
                        if remaining > 0:
                            print(f"       ... ({remaining} more lines, use --full-trace to see all)")
        print()

    # Summary table
    print("=" * 80)
    print("  Summary")
    print("=" * 80)
    total = len(task_reports)
    passed = sum(1 for r in task_reports if r["success"])
    avg_score = sum(r["score"] for r in task_reports) / total if total > 0 else 0
    print(f"  Tasks: {passed}/{total} passed  |  Avg Score: {avg_score:.1f}%")
    print()

    # Per-difficulty breakdown
    by_diff: dict[str, list] = {}
    for r in task_reports:
        d = str(r["difficulty"])
        by_diff.setdefault(d, []).append(r)
    for diff in sorted(by_diff.keys()):
        reports = by_diff[diff]
        passed_d = sum(1 for r in reports if r["success"])
        avg_d = sum(r["score"] for r in reports) / len(reports)
        print(f"  Difficulty {diff}: {passed_d}/{len(reports)} passed  |  Avg Score: {avg_d:.1f}%")
    print()


if __name__ == "__main__":
    main()
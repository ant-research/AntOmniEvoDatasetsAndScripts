"""List tasks for an AppWorld dataset and write results as JSON.

Usage:
    APPWORLD_ROOT=/path/to/appworld python -m experiments.list_tasks \
        --dataset dev \
        --output /tmp/tasks.json

The output JSON is a list of task objects:
    [
        {
            "task_id": "024c982_1",
            "scenario_id": "024c982",
            "instruction": "Request $13 publicly on Venmo from...",
            "golden_answer": "..."
        }
    ]
"""

import argparse
import json
import os
import sys

# When run as a script, Python adds the script's directory (experiments/) to
# sys.path[0], which shadows the stdlib "code" module with experiments/code/.
# Fix: remove that entry and insert the AppWorld project root instead.
if sys.path and sys.path[0] == os.path.dirname(__file__):
    sys.path.pop(0)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from appworld.task import Task, load_task_ids


def main() -> None:
    parser = argparse.ArgumentParser(
        description="List tasks for an AppWorld dataset and write results as JSON"
    )
    parser.add_argument(
        "--dataset",
        required=True,
        choices=["train", "dev", "test_normal", "test_challenge"],
        help="Dataset name",
    )
    parser.add_argument(
        "--output",
        required=True,
        help="Path to write the output JSON file",
    )
    args = parser.parse_args()

    task_ids = load_task_ids(args.dataset)
    print(f"Loaded {len(task_ids)} task IDs from dataset '{args.dataset}'", file=sys.stderr)

    instances = []
    for task_id in task_ids:
        try:
            task = Task.load(task_id)
        except Exception as e:
            print(f"Failed to load task {task_id}: {e}", file=sys.stderr)
            continue

        scenario_id = task_id.rsplit("_", 1)[0] if "_" in task_id else task_id
        answer = ""
        if task.ground_truth is not None and task.ground_truth.answer is not None:
            answer = str(task.ground_truth.answer)

        instances.append({
            "task_id": task_id,
            "scenario_id": scenario_id,
            "instruction": task.instruction,
            "golden_answer": answer,
        })

    instances.sort(key=lambda x: x["task_id"])

    # Write output
    output_dir = os.path.dirname(args.output)
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(instances, f, indent=2, ensure_ascii=False)

    print(f"Wrote {len(instances)} tasks to {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
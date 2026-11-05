"""Run all AppWorld datasets (train, dev, test_normal, test_challenge) with SimplifiedSkillReActCodeAgent using glm-5.

Uses a concurrency-slot model: maintains N concurrent subprocess workers.
Whenever a worker finishes its task, the next pending task is dispatched
to a free slot—no static sharding, fast workers pick up more work.

Usage:
    # Run all 4 datasets
    python -m experiments.run_all_glm5

    # Run specific datasets
    python -m experiments.run_all_glm5 --datasets train dev
    python -m experiments.run_all_glm5 --datasets test_normal test_challenge

    # Custom settings
    python -m experiments.run_all_glm5 --concurrency 30 --max-steps 50

    # After run, evaluate:
    python -m experiments.eval_results --experiment glm5_empty_skill_train --save
    python -m experiments.eval_results --experiment glm5_empty_skill_dev --save
    python -m experiments.eval_results --experiment glm5_empty_skill_test_normal --save
    python -m experiments.eval_results --experiment glm5_empty_skill_test_challenge --save
"""

import argparse
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))

from appworld import load_task_ids


DATASET_EXPERIMENT_NAME = {
    "train": "glm5_empty_skill_train",
    "dev": "glm5_empty_skill_dev",
    "test_normal": "glm5_empty_skill_test_normal",
    "test_challenge": "glm5_empty_skill_test_challenge",
}

ALL_DATASETS = ["train", "dev", "test_normal", "test_challenge"]


def get_finished_task_ids(experiment_name: str) -> set[str]:
    """Return the set of task IDs that already have a finished marker."""
    from appworld.common.path_store import path_store
    tasks_dir = os.path.join(path_store.experiment_outputs, experiment_name, "tasks")
    if not os.path.isdir(tasks_dir):
        return set()
    finished = set()
    for tid in os.listdir(tasks_dir):
        marker = os.path.join(tasks_dir, tid, "misc", "finished")
        if os.path.exists(marker):
            finished.add(tid)
    return finished


def run_single_task(task_id: str, args: list[str], env: dict) -> tuple[str, int]:
    """Run a single task as a subprocess. Returns (task_id, return_code)."""
    cmd = [sys.executable, "-m", "experiments.run_all_glm5"] + args + ["--task-id", task_id]
    result = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=3600)
    return task_id, result.returncode


def run_dataset(
    dataset_name: str,
    experiment_name: str,
    task_ids: list[str],
    concurrency: int,
    cli_args: list[str],
) -> None:
    """Run all tasks for a dataset using concurrency-slot subprocess workers."""
    env = os.environ.copy()

    print(f"\n{'─' * 70}")
    print(f"  Dataset: {dataset_name}  |  Experiment: {experiment_name}")
    print(f"  Tasks: {len(task_ids)}  |  Concurrency: {concurrency}")
    print(f"{'─' * 70}\n")

    completed = 0
    failed = 0
    total = len(task_ids)

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {
            pool.submit(run_single_task, tid, cli_args, env): tid
            for tid in task_ids
        }

        for future in as_completed(futures):
            task_id = futures[future]
            try:
                _, returncode = future.result()
                if returncode != 0:
                    failed += 1
                    print(f"  ❌ [{completed + failed}/{total}] {task_id} (exit {returncode})")
                else:
                    completed += 1
                    print(f"  ✅ [{completed + failed}/{total}] {task_id} done")
            except Exception as e:
                failed += 1
                print(f"  ❌ [{completed + failed}/{total}] {task_id} EXCEPTION: {e}")

    print(f"\n  Dataset {dataset_name}: {completed} done, {failed} failed out of {total}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run AppWorld datasets with SimplifiedSkillReActCodeAgent (glm-5, empty skill)"
    )
    parser.add_argument(
        "--datasets",
        nargs="*",
        default=ALL_DATASETS,
        choices=ALL_DATASETS,
        help=f"Datasets to run (default: all: {', '.join(ALL_DATASETS)})",
    )
    parser.add_argument(
        "--skill-dir",
        default=None,
        help="Path to skill directory. Default: empty skill dir (only AGENT_CONSTRAINT).",
    )
    parser.add_argument(
        "--max-steps",
        type=int,
        default=50,
        help="Max steps per task (default: 50)",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=30,
        help="Number of concurrent task slots (default: 30)",
    )
    parser.add_argument(
        "--model",
        default="anthropic/glm-5",
        help="Model name (default: anthropic/glm-5)",
    )
    parser.add_argument(
        "--task-id",
        default=None,
        help=argparse.SUPPRESS,  # Internal: run a single task
    )
    args = parser.parse_args()

    # --- Internal single-task worker mode ---
    if args.task_id is not None:
        from appworld_agents.code.simplified.skill_react_code_agent import (
            SimplifiedSkillReActCodeAgent,
        )

        config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "env", "config.yaml")
        api_config: dict[str, str] = {}
        with open(config_path, "r") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                key, _, value = line.partition(":")
                api_config[key.strip()] = value.strip()

        os.environ.setdefault("MODEL_SERVER_URL", api_config["BASE_URL"])

        skill_dir = args.skill_dir
        if skill_dir is None:
            skill_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompts", "empty_skill")

        # Determine experiment name from task_id's dataset
        experiment_name = None
        for ds in ALL_DATASETS:
            task_ids = load_task_ids(ds)
            if args.task_id in task_ids:
                experiment_name = DATASET_EXPERIMENT_NAME[ds]
                break
        if experiment_name is None:
            print(f"ERROR: task_id {args.task_id} not found in any dataset")
            sys.exit(1)

        agent = SimplifiedSkillReActCodeAgent(
            skill_dir=skill_dir,
            ignore_multiple_calls=True,
            max_prompt_length=50000,
            max_output_length=20000,
            max_steps=args.max_steps,
            skip_if_finished=True,
            model_config={
                "name": args.model,
                "temperature": 0,
                "max_tokens": 4000,
                "retry_after_n_seconds": 10,
                "max_retries": 5,
                "api_key": api_config["API_KEY"],
                "base_url": api_config["BASE_URL"],
                "cost_per_token": {
                    "input_cache_miss": 0.0,
                    "input_cache_hit": 0.0,
                    "input_cache_write": 0.0,
                    "output": 0.0,
                },
            },
        )

        agent.solve_tasks(
            task_ids=[args.task_id],
            experiment_name=experiment_name,
            num_processes=1,
        )
        return

    # --- Main orchestrator mode ---
    skill_dir = args.skill_dir
    if skill_dir is None:
        skill_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompts", "empty_skill")

    datasets_to_run = args.datasets

    # Build CLI args to forward to worker subprocesses
    cli_args = [
        "--max-steps", str(args.max_steps),
        "--model", args.model,
        "--skill-dir", skill_dir,
    ]

    # Print summary
    total_tasks = 0
    for dataset_name in datasets_to_run:
        task_ids = load_task_ids(dataset_name)
        total_tasks += len(task_ids)

    print(f"\n{'=' * 70}")
    print("  AppWorld Full Run - glm-5 (empty skill)")
    print(f"{'=' * 70}")
    print(f"  Model:          {args.model}")
    print(f"  Skill dir:      {skill_dir}")
    print(f"  Max steps:      {args.max_steps}")
    print(f"  Concurrency:    {args.concurrency}")
    print(f"  Datasets:       {', '.join(datasets_to_run)}")
    print(f"  Total tasks:    {total_tasks}")
    print(f"{'=' * 70}")

    for dataset_name in datasets_to_run:
        experiment_name = DATASET_EXPERIMENT_NAME[dataset_name]
        task_ids = load_task_ids(dataset_name)

        # Filter out already-finished tasks
        finished = get_finished_task_ids(experiment_name)
        unfinished = [tid for tid in task_ids if tid not in finished]
        skipped = len(task_ids) - len(unfinished)
        if skipped > 0:
            print(f"  Skipping {skipped} already-finished tasks, {len(unfinished)} remaining")

        if not unfinished:
            print(f"  No tasks to run for {dataset_name}, skipping.")
            continue

        run_dataset(
            dataset_name=dataset_name,
            experiment_name=experiment_name,
            task_ids=unfinished,
            concurrency=args.concurrency,
            cli_args=cli_args,
        )
        print(f"\n  ✅ Dataset {dataset_name} completed!")
        print(f"     Evaluate: python -m experiments.eval_results --experiment {experiment_name} --save\n")

    print(f"\n{'=' * 70}")
    print("  All datasets completed!")
    print(f"{'=' * 70}")
    for dataset_name in datasets_to_run:
        experiment_name = DATASET_EXPERIMENT_NAME[dataset_name]
        print(f"  {dataset_name:20s}: python -m experiments.eval_results --experiment {experiment_name} --save")
    print()


if __name__ == "__main__":
    main()
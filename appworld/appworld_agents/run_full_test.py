"""Run AppWorld test datasets (test_normal + test_challenge) with SimplifiedSkillReActCodeAgent.

Usage:
    # Run both test_normal and test_challenge
    python -m experiments.run_full_test

    # Run only test_normal
    python -m experiments.run_full_test --dataset test_normal

    # Run only test_challenge
    python -m experiments.run_full_test --dataset test_challenge

    # Custom settings
    python -m experiments.run_full_test --max-steps 30 --num-processes 4

    # After run, evaluate:
    python -m experiments.eval_results --experiment glm5_test_normal --save
    python -m experiments.eval_results --experiment glm5_test_challenge --save
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))

from appworld import load_task_ids
from appworld_agents.code.simplified.skill_react_code_agent import (
    SimplifiedSkillReActCodeAgent,
)


def load_api_config() -> dict:
    config_path = os.path.join(os.path.dirname(__file__), "..", "env", "config.yaml")
    config: dict[str, str] = {}
    with open(config_path, "r") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, _, value = line.partition(":")
            config[key.strip()] = value.strip()
    return config


DATASET_EXPERIMENT_NAME = {
    "test_normal": "glm5_no_skill_test_normal",
    "test_challenge": "glm5_no_skill_test_challenge",
}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run AppWorld test datasets with SimplifiedSkillReActCodeAgent (no skill)"
    )
    parser.add_argument(
        "--dataset",
        choices=["test_normal", "test_challenge", "both"],
        default="both",
        help="Which dataset to run (default: both)",
    )
    parser.add_argument(
        "--skill-dir",
        default=None,
        help="Path to skill directory. If not set, uses empty skill (only AGENT_CONSTRAINT).",
    )
    parser.add_argument(
        "--max-steps",
        type=int,
        default=40,
        help="Max steps per task (default: 40)",
    )
    parser.add_argument(
        "--num-processes",
        type=int,
        default=1,
        help="Number of parallel processes (default: 1)",
    )
    parser.add_argument(
        "--model",
        default="anthropic/glm-5",
        help="Model name (default: anthropic/glm-5)",
    )
    parser.add_argument(
        "--skip-if-finished",
        action="store_true",
        default=True,
        help="Skip already finished tasks (default: True)",
    )
    args = parser.parse_args()

    api_config = load_api_config()
    os.environ.setdefault("MODEL_SERVER_URL", api_config["BASE_URL"])

    datasets = []
    if args.dataset == "both":
        datasets = ["test_normal", "test_challenge"]
    else:
        datasets = [args.dataset]

    for dataset_name in datasets:
        experiment_name = DATASET_EXPERIMENT_NAME[dataset_name]
        task_ids = load_task_ids(dataset_name)
        print(f"\n{'=' * 60}")
        print(f"  Dataset: {dataset_name}")
        print(f"  Experiment: {experiment_name}")
        print(f"  Tasks: {len(task_ids)}")
        print(f"  Model: {args.model}")
        print(f"  Skill dir: {args.skill_dir or '(empty - AGENT_CONSTRAINT only)'}")
        print(f"  Max steps: {args.max_steps}")
        print(f"  Num processes: {args.num_processes}")
        print(f"{'=' * 60}\n")

        # Create a fresh agent for each dataset
        agent = SimplifiedSkillReActCodeAgent(
            skill_dir=args.skill_dir,
            ignore_multiple_calls=True,
            max_prompt_length=50000,
            max_output_length=20000,
            max_steps=args.max_steps,
            skip_if_finished=args.skip_if_finished,
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
            task_ids=task_ids,
            experiment_name=experiment_name,
            num_processes=args.num_processes,
        )

        print(f"\n✅ Dataset {dataset_name} completed!")
        print(f"   Experiment name: {experiment_name}")
        print(f"   Evaluate with: python -m experiments.eval_results --experiment {experiment_name} --save")

    print(f"\n{'=' * 60}")
    print("  All datasets completed!")
    print(f"{'=' * 60}")
    for dataset_name in datasets:
        experiment_name = DATASET_EXPERIMENT_NAME[dataset_name]
        print(f"  {dataset_name}: python -m experiments.eval_results --experiment {experiment_name} --save")


if __name__ == "__main__":
    main()
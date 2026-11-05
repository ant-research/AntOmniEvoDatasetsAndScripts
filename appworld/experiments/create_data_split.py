"""Split AppWorld datasets into training and validation sets.

Training: all tasks from train + dev datasets.
Validation: 200 tasks sampled from test_normal and test_challenge, with
    - 50% from test_normal, 50% from test_challenge (100 each)
    - ~10% pass + ~90% fail per subset (based on glm5_empty_skill eval results)

Usage:
    python -m experiments.create_data_split

    # With custom parameters:
    python -m experiments.create_data_split --val-size 200 --pass-ratio 0.1 --seed 42
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from appworld import load_task_ids


EXPERIMENT_NAMES = {
    "test_normal": "glm5_empty_skill_test_normal",
    "test_challenge": "glm5_empty_skill_test_challenge",
}


def load_eval_results(experiment_name: str) -> dict:
    """Load evaluation results for an experiment."""
    from appworld.common.path_store import path_store

    result_path = os.path.join(
        path_store.experiment_outputs, experiment_name, "eval_results.json"
    )
    if not os.path.exists(result_path):
        print(f"ERROR: {result_path} not found. Run evaluation first:")
        print(f"  python -m experiments.eval_results --experiment {experiment_name} --save")
        sys.exit(1)
    with open(result_path, "r") as f:
        return json.load(f)


def main():
    parser = argparse.ArgumentParser(
        description="Create train/validation data split for AppWorld experiments"
    )
    parser.add_argument(
        "--val-size",
        type=int,
        default=200,
        help="Total validation set size (default: 200)",
    )
    parser.add_argument(
        "--pass-ratio",
        type=float,
        default=0.1,
        help="Ratio of pass (correct) tasks in validation set (default: 0.1)",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Random seed for reproducibility (default: 42)",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Output directory (default: experiments/data_splits)",
    )
    args = parser.parse_args()

    import random

    random.seed(args.seed)

    output_dir = args.output_dir or os.path.join(os.path.dirname(__file__), "data_splits")
    os.makedirs(output_dir, exist_ok=True)

    # ── Training set: train + dev ─────────────────────────────────────────
    train_ids = load_task_ids("train")
    dev_ids = load_task_ids("dev")
    training_ids = sorted(train_ids + dev_ids)

    print(f"Training set: {len(train_ids)} (train) + {len(dev_ids)} (dev) = {len(training_ids)} tasks")

    # ── Validation set: sampled from test_normal + test_challenge ─────────
    val_size = args.val_size
    pass_ratio = args.pass_ratio
    fail_ratio = 1.0 - pass_ratio
    half = val_size // 2  # 100 per subset

    val_normal_pass_target = int(half * pass_ratio)   # 10
    val_normal_fail_target = half - val_normal_pass_target  # 90
    val_challenge_pass_target = int(half * pass_ratio)  # 10
    val_challenge_fail_target = half - val_challenge_pass_target  # 90

    # Load eval results to determine pass/fail
    results = {}
    for dataset_name, exp_name in EXPERIMENT_NAMES.items():
        results[dataset_name] = load_eval_results(exp_name)

    tn_tasks = results["test_normal"]["tasks"]
    tc_tasks = results["test_challenge"]["tasks"]

    tn_pass = [t for t in tn_tasks if t["success"]]
    tn_fail = [t for t in tn_tasks if not t["success"]]
    tc_pass = [t for t in tc_tasks if t["success"]]
    tc_fail = [t for t in tc_tasks if not t["success"]]

    print(f"\ntest_normal:   {len(tn_pass)} pass, {len(tn_fail)} fail (total {len(tn_tasks)})")
    print(f"test_challenge: {len(tc_pass)} pass, {len(tc_fail)} fail (total {len(tc_tasks)})")

    # Adjust targets if not enough samples available
    def adjust_targets(pass_target, fail_target, pass_available, fail_available, subset_name):
        """Adjust pass/fail targets when not enough samples are available."""
        original_pass, original_fail = pass_target, fail_target
        if fail_available < fail_target:
            shortfall = fail_target - fail_available
            fail_target = fail_available
            pass_target = min(pass_target + shortfall, pass_available)
            print(f"  ⚠️ {subset_name}: only {fail_available} fail available, "
                  f"adjusted {original_pass}+{original_fail} → {pass_target}+{fail_target}")
        if pass_target > pass_available:
            shortfall = pass_target - pass_available
            pass_target = pass_available
            fail_target = min(fail_target + shortfall, fail_available)
            print(f"  ⚠️ {subset_name}: only {pass_available} pass available, "
                  f"adjusted {original_pass}+{original_fail} → {pass_target}+{fail_target}")
        return pass_target, fail_target

    val_normal_pass_n, val_normal_fail_n = adjust_targets(
        val_normal_pass_target, val_normal_fail_target,
        len(tn_pass), len(tn_fail), "test_normal"
    )
    val_challenge_pass_n, val_challenge_fail_n = adjust_targets(
        val_challenge_pass_target, val_challenge_fail_target,
        len(tc_pass), len(tc_fail), "test_challenge"
    )

    # Random sample
    random.shuffle(tn_pass)
    random.shuffle(tn_fail)
    random.shuffle(tc_pass)
    random.shuffle(tc_fail)

    tn_val_ids = sorted(
        [t["task_id"] for t in tn_pass[:val_normal_pass_n]]
        + [t["task_id"] for t in tn_fail[:val_normal_fail_n]]
    )
    tc_val_ids = sorted(
        [t["task_id"] for t in tc_pass[:val_challenge_pass_n]]
        + [t["task_id"] for t in tc_fail[:val_challenge_fail_n]]
    )
    all_val_ids = sorted(tn_val_ids + tc_val_ids)

    # ── Build output ──────────────────────────────────────────────────────
    output = {
        "config": {
            "val_size": val_size,
            "pass_ratio": pass_ratio,
            "seed": args.seed,
            "model": "anthropic/glm-5",
            "agent": "SimplifiedSkillReActCodeAgent",
            "skill": "empty (AGENT_CONSTRAINT only)",
            "max_steps": 50,
        },
        "train": {
            "description": "Training data: train + dev datasets",
            "task_ids": training_ids,
            "count": len(training_ids),
            "source": {"train": len(train_ids), "dev": len(dev_ids)},
        },
        "validation": {
            "description": (
                f"Validation data: ~{pass_ratio*100:.0f}% pass + "
                f"~{fail_ratio*100:.0f}% fail, 50% normal + 50% challenge, "
                f"{val_size} total"
            ),
            "test_normal": {
                "task_ids": tn_val_ids,
                "count": len(tn_val_ids),
                "pass_count": val_normal_pass_n,
                "fail_count": val_normal_fail_n,
            },
            "test_challenge": {
                "task_ids": tc_val_ids,
                "count": len(tc_val_ids),
                "pass_count": val_challenge_pass_n,
                "fail_count": val_challenge_fail_n,
            },
            "total_count": len(all_val_ids),
            "total_pass": val_normal_pass_n + val_challenge_pass_n,
            "total_fail": val_normal_fail_n + val_challenge_fail_n,
        },
    }

    # ── Save files ────────────────────────────────────────────────────────
    json_path = os.path.join(output_dir, "train_val_split.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    with open(os.path.join(output_dir, "train_task_ids.txt"), "w") as f:
        f.write("\n".join(training_ids) + "\n")

    with open(os.path.join(output_dir, "val_test_normal_task_ids.txt"), "w") as f:
        f.write("\n".join(tn_val_ids) + "\n")

    with open(os.path.join(output_dir, "val_test_challenge_task_ids.txt"), "w") as f:
        f.write("\n".join(tc_val_ids) + "\n")

    with open(os.path.join(output_dir, "val_all_task_ids.txt"), "w") as f:
        f.write("\n".join(all_val_ids) + "\n")

    # ── Print summary ─────────────────────────────────────────────────────
    print(f"\n{'=' * 60}")
    print(f"  Data Split Summary")
    print(f"{'=' * 60}")
    print(f"\n  Training: {len(training_ids)} tasks")
    print(f"    train: {len(train_ids)}, dev: {len(dev_ids)}")
    print(f"\n  Validation: {len(all_val_ids)} tasks")
    print(f"    test_normal:    {len(tn_val_ids)} tasks "
          f"({val_normal_pass_n} pass + {val_normal_fail_n} fail)")
    print(f"    test_challenge:  {len(tc_val_ids)} tasks "
          f"({val_challenge_pass_n} pass + {val_challenge_fail_n} fail)")
    total_pass = val_normal_pass_n + val_challenge_pass_n
    total_fail = val_normal_fail_n + val_challenge_fail_n
    actual_pass_ratio = total_pass / len(all_val_ids) * 100 if all_val_ids else 0
    print(f"    Total pass: {total_pass} ({actual_pass_ratio:.1f}%)")
    print(f"    Total fail: {total_fail} ({100 - actual_pass_ratio:.1f}%)")
    print(f"\n  Files saved to {output_dir}/")
    print(f"    train_val_split.json         (full metadata)")
    print(f"    train_task_ids.txt           ({len(training_ids)} IDs)")
    print(f"    val_test_normal_task_ids.txt  ({len(tn_val_ids)} IDs)")
    print(f"    val_test_challenge_task_ids.txt ({len(tc_val_ids)} IDs)")
    print(f"    val_all_task_ids.txt          ({len(all_val_ids)} IDs)")
    print()


if __name__ == "__main__":
    main()
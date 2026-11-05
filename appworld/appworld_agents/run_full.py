#!/usr/bin/env python
"""Run + evaluate AppWorld test_normal / test_challenge for one skill and
compute TGC / SGC. Implements tasks/task.md.

Flow (datasets are run SEQUENTIALLY — never two at once, per the task):
  for each of test_normal, test_challenge:
    1. experiments/run_batch.py   --concurrency 30 --model glm-5 --skill-dir <only-skillmd>
    2. experiments/eval_batch.py  --concurrency 10 --save-reports
    3. compute TGC / SGC from the per-task eval results (grouped by generator_id)

Output layout (the timestamped dir is a self-contained APPWORLD_ROOT with `data/`
symlinked to the real repo data, so appworld finds data/tasks AND writes the
experiment under it — matching the structure in task.md):

  experiments/outputs/skill_{version}_{model}_{timestamp}/
  ├── results.md
  ├── results.json
  ├── run.log
  ├── data -> <repo>/data                      (symlink, read-only usage)
  └── experiments/outputs/
      ├── antgroup_react_agent_and_auto_optimized_skill_test_normal/
      │   ├── run_result.json
      │   ├── eval_result.json
      │   └── tasks/...
      └── antgroup_react_agent_and_auto_optimized_skill_test_challenge/
          ├── run_result.json
          ├── eval_result.json
          └── tasks/...

NOTE on model/base_url: env/config.yaml points BASE_URL at the /api/anthropic
route. The repo's reference (run_all_glm5.py) uses model string "anthropic/glm-5"
with that route. The task says use model "glm-5" (bare) — we default to that, but
if the gateway rejects the bare name at /api/anthropic, pass --model
anthropic/glm-5 (or --base-url https://antchat.alipay.com/v1, which the gepa
smoke proved works with bare "glm-5").
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import subprocess
import sys
from collections import defaultdict


# Resolve repo root from this script's location (experiments/run_full_eval.py).
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENV_PY = os.path.join(REPO, ".venv", "bin", "python")

CONFIG_PATH = os.path.join(REPO, "env", "config.yaml")
DATASETS_DIR = os.path.join(REPO, "data", "datasets")
REAL_DATA_DIR = os.path.join(REPO, "data")
OUTPUT_BASE = os.path.join(REPO, "experiments", "outputs")

AGENT = "appworld_agents/code/simplified/skill_react_code_agent.py"  # used by run_batch.py

# (dataset .txt name, experiment name, label) — run in this order, one at a time.
TEST_DATASETS = [
    ("test_normal", "antgroup_react_agent_and_auto_optimized_skill_test_normal", "test_normal"),
    ("test_challenge", "antgroup_react_agent_and_auto_optimized_skill_test_challenge", "test_challenge"),
]


def load_config(path: str) -> dict[str, str]:
    """Parse the simple `KEY: value` env/config.yaml (same parser as run_batch.py)."""
    cfg: dict[str, str] = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, _, value = line.partition(":")
            cfg[key.strip()] = value.strip()
    return cfg


def read_task_ids(path: str) -> list[str]:
    with open(path, encoding="utf-8") as f:
        return [ln.strip() for ln in f if ln.strip()]


def generator_id(task_id: str) -> str:
    """Scenario = task_id prefix before the last underscore (e.g. 3d9a636_1 -> 3d9a636)."""
    return task_id.rsplit("_", 1)[0]


def compute_tgc_sgc(eval_result_path: str, task_ids: list[str]) -> dict:
    """TGC = per-task success rate; SGC = per-scenario all-must-pass rate.

    We compute from eval_result.json's per-task `success` ourselves because
    eval_batch.py's concurrency>1 path sets SGC == TGC (not the real SGC).
    """
    success_by_task: dict[str, bool] = {}
    if os.path.isfile(eval_result_path):
        with open(eval_result_path, encoding="utf-8") as f:
            data = json.load(f)
        for t in data.get("tasks", []):
            success_by_task[t.get("task_id")] = bool(t.get("success", False))

    n_tasks = len(task_ids)
    n_success = sum(1 for tid in task_ids if success_by_task.get(tid, False))
    tgc = round(100.0 * n_success / n_tasks, 1) if n_tasks else 0.0

    by_scenario: dict[str, list[bool]] = defaultdict(list)
    for tid in task_ids:
        by_scenario[generator_id(tid)].append(success_by_task.get(tid, False))
    n_scenarios = len(by_scenario)
    n_scenarios_pass = sum(1 for vals in by_scenario.values() if vals and all(vals))
    sgc = round(100.0 * n_scenarios_pass / n_scenarios, 1) if n_scenarios else 0.0

    return {
        "n_tasks": n_tasks,
        "n_success": n_success,
        "n_scenarios": n_scenarios,
        "n_scenarios_pass": n_scenarios_pass,
        "TGC": tgc,
        "SGC": sgc,
    }


def run_step(cmd: list[str], cwd: str, env: dict, log) -> int:
    log.write(">> " + " ".join(cmd) + "\n")
    log.flush()
    proc = subprocess.run(cmd, cwd=cwd, env=env)
    return proc.returncode


def write_results_md(path: str, meta: dict, per_dataset: dict) -> None:
    lines = [
        "# AppWorld Full Evaluation — Results",
        "",
        f"- Timestamp: {meta['timestamp']}",
        f"- Skill dir: `{meta['skill_dir']}` (version: `{meta['skill_version']}`)",
        f"- Model: `{meta['model']}`",
        f"- Base URL: `{meta['base_url']}`",
        f"- Max steps: {meta['max_steps']}",
        f"- Temperature: {meta['temperature']}  |  Max tokens: {meta['max_tokens']}",
        f"- Run concurrency: {meta['run_concurrency']}  |  Eval concurrency: {meta['eval_concurrency']}",
        f"- Agent: `{meta['agent']}`",
        f"- Output root: `{meta['output_root']}`",
        "",
        "## TGC / SGC",
        "",
        "| Dataset | Tasks | Success | Scenarios | Scenarios pass | TGC (%) | SGC (%) |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for label, info in per_dataset.items():
        m = info["metrics"]
        lines.append(
            f"| {label} | {m['n_tasks']} | {m['n_success']} | {m['n_scenarios']} | "
            f"{m['n_scenarios_pass']} | **{m['TGC']}** | **{m['SGC']}** |"
        )
    lines += [
        "",
        "## Notes",
        "- TGC = 100 * (# tasks fully passing all tests) / (# tasks).",
        "- SGC = 100 * (# scenarios where ALL variants pass) / (# scenarios); "
        "scenario = task_id prefix before the last `_` (generator_id).",
        "- SGC ≤ TGC. A bigger gap means the agent is unstable within a scenario.",
        "",
        "## Per-dataset output",
        "",
    ]
    for label, info in per_dataset.items():
        lines.append(f"### {label}")
        lines.append(f"- experiment_name: `{info['experiment_name']}`")
        lines.append(f"- run_result: `{info['run_result']}`")
        lines.append(f"- eval_result: `{info['eval_result']}`")
        if info.get("run_rc") is not None:
            lines.append(f"- run_batch exit code: {info['run_rc']}")
        if info.get("eval_rc") is not None:
            lines.append(f"- eval_batch exit code: {info['eval_rc']}")
        lines.append("")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def main() -> None:
    p = argparse.ArgumentParser(description="Run + eval AppWorld test_normal/challenge, compute TGC/SGC.")
    p.add_argument("--model", default="glm-5", help="Model name (default: glm-5, per task.md)")
    p.add_argument("--skill-dir", default=os.path.join(REPO, "experiments/prompts/skill_react_code_agent/only-skillmd"))
    p.add_argument("--api-key", default=None, help="Override config API_KEY")
    p.add_argument("--base-url", default=None, help="Override config BASE_URL")
    p.add_argument("--max-steps", type=int, default=50)
    p.add_argument("--temperature", type=float, default=0.7,
                   help="Sampling temperature passed to run_batch (default: 0.7)")
    p.add_argument("--max-tokens", type=int, default=10000,
                   help="Max completion tokens passed to run_batch (default: 10000)")
    p.add_argument("--run-concurrency", type=int, default=30)
    p.add_argument("--eval-concurrency", type=int, default=10)
    p.add_argument("--datasets", nargs="*", default=[d[0] for d in TEST_DATASETS],
                   choices=[d[0] for d in TEST_DATASETS])
    p.add_argument("--limit", type=int, default=None,
                   help="Only run first N task IDs per dataset (for a quick preflight).")
    p.add_argument("--no-skip-finished", action="store_true",
                   help="Re-run tasks even if a 'finished' marker exists (run_batch --skip-if-finished is on by default).")
    args = p.parse_args()

    cfg = load_config(CONFIG_PATH)
    api_key = args.api_key or cfg.get("API_KEY", "")
    base_url = args.base_url or cfg.get("BASE_URL", "")
    if not api_key or not base_url:
        sys.exit("ERROR: API_KEY/BASE_URL missing (check env/config.yaml or pass --api-key/--base-url).")

    skill_version = os.path.basename(args.skill_dir.rstrip("/")) or "skill"
    model_tag = args.model.replace("/", "-")
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    outer = os.path.join(OUTPUT_BASE, f"skill_{skill_version}_{model_tag}_{timestamp}")
    os.makedirs(outer, exist_ok=True)

    # Self-contained APPWORLD_ROOT: symlink data/ so appworld finds data/tasks,
    # while experiment outputs land under this timestamped dir.
    data_link = os.path.join(outer, "data")
    if not os.path.lexists(data_link):
        os.symlink(REAL_DATA_DIR, data_link)

    env = os.environ.copy()
    env["APPWORLD_ROOT"] = outer

    log_path = os.path.join(outer, "run.log")
    log = open(log_path, "w", encoding="utf-8")

    meta = {
        "timestamp": timestamp,
        "skill_dir": args.skill_dir,
        "skill_version": skill_version,
        "model": args.model,
        "base_url": base_url,
        "max_steps": args.max_steps,
        "temperature": args.temperature,
        "max_tokens": args.max_tokens,
        "run_concurrency": args.run_concurrency,
        "eval_concurrency": args.eval_concurrency,
        "agent": AGENT,
        "output_root": outer,
    }
    print(f"Output root: {outer}")
    print(f"Model={args.model}  base_url={base_url}  skill={args.skill_dir}")
    print(f"Datasets (sequential): {args.datasets}  limit={args.limit}")

    per_dataset: dict[str, dict] = {}
    for ds_name, exp_name, label in TEST_DATASETS:
        if ds_name not in args.datasets:
            continue
        print(f"\n=== [{label}] run_batch -> eval_batch ===")
        ids_file = os.path.join(DATASETS_DIR, f"{ds_name}.txt")
        task_ids = read_task_ids(ids_file)
        if args.limit:
            task_ids = task_ids[: args.limit]
        out_dir = os.path.join(outer, "experiments", "outputs", exp_name)
        os.makedirs(out_dir, exist_ok=True)
        run_result = os.path.join(out_dir, "run_result.json")
        eval_result = os.path.join(out_dir, "eval_result.json")

        run_cmd = [
            VENV_PY, "-u", "-m", "experiments.run_batch",
            "--task-ids", *task_ids,
            "--experiment-name", exp_name,
            "--skill-dir", args.skill_dir,
            "--model", args.model,
            "--api-key", api_key,
            "--base-url", base_url,
            "--max-steps", str(args.max_steps),
            "--temperature", str(args.temperature),
            "--max-tokens", str(args.max_tokens),
            "--concurrency", str(args.run_concurrency),
            "--result-file", run_result,
        ]
        if not args.no_skip_finished:
            run_cmd.append("--skip-if-finished")
        run_rc = run_step(run_cmd, cwd=REPO, env=env, log=log)
        if run_rc != 0:
            print(f"  WARNING: run_batch exited {run_rc} for {label}; continuing to eval anyway.")

        eval_cmd = [
            VENV_PY, "-u", "-m", "experiments.eval_batch",
            "--task-ids", *task_ids,
            "--experiment-name", exp_name,
            "--result-file", eval_result,
            "--save-reports",
            "--concurrency", str(args.eval_concurrency),
        ]
        eval_rc = run_step(eval_cmd, cwd=REPO, env=env, log=log)

        metrics = compute_tgc_sgc(eval_result, task_ids)
        print(f"  [{label}] TGC={metrics['TGC']}%  SGC={metrics['SGC']}%  "
              f"(tasks {metrics['n_success']}/{metrics['n_tasks']}, "
              f"scenarios {metrics['n_scenarios_pass']}/{metrics['n_scenarios']})")
        per_dataset[label] = {
            "experiment_name": exp_name,
            "run_result": run_result,
            "eval_result": eval_result,
            "run_rc": run_rc,
            "eval_rc": eval_rc,
            "metrics": metrics,
        }
        log.flush()

    write_results_md(os.path.join(outer, "results.md"), meta, per_dataset)
    with open(os.path.join(outer, "results.json"), "w", encoding="utf-8") as f:
        json.dump({"meta": meta, "per_dataset": per_dataset}, f, indent=2, ensure_ascii=False)
    log.close()

    print(f"\nDone. Wrote results.md / results.json / run.log under:\n  {outer}")
    print("Summary:")
    for label, info in per_dataset.items():
        m = info["metrics"]
        print(f"  {label:16s} TGC={m['TGC']:>5}%  SGC={m['SGC']:>5}%  "
              f"(tasks {m['n_success']}/{m['n_tasks']}, scenarios {m['n_scenarios_pass']}/{m['n_scenarios']})")


if __name__ == "__main__":
    main()
"""Run a batch of AppWorld tasks via SimplifiedSkillReActCodeAgent and write results as JSON.

Usage:
    APPWORLD_ROOT=/path/to/appworld python -m experiments.run_batch \
        --task-ids 024c982_1 042a9fc_1 \
        --experiment-name auto_opt_xxx \
        --skill-dir /path/to/skill \
        --model anthropic/glm-5 \
        --result-file /tmp/run_result.json \
        --concurrency 30

The result JSON is written to --result-file with the following schema:
    {
        "experiment_name": "auto_opt_xxx",
        "tasks": {
            "<task_id>": {
                "status": "completed" | "failed",
                "trajectory_relpath": "experiments/outputs/<exp>/tasks/<id>/misc/trajectory.json",
                "usage_relpath": "experiments/outputs/<exp>/tasks/<id>/misc/usage.json",
                "error": "..."  // only if status == "failed"
            }
        }
    }
"""

import argparse
import gc
import json
import os
import shutil
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

from appworld.common.path_store import path_store


def load_api_config() -> dict:
    """Load API config from env/config.yaml (same as run_all_glm5.py)."""
    config_path = os.path.join(os.path.dirname(__file__), "..", "env", "config.yaml")
    config: dict[str, str] = {}
    if not os.path.isfile(config_path):
        return config
    with open(config_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, _, value = line.partition(":")
            config[key.strip()] = value.strip()
    return config


def _read_file_tail(path: str, max_chars: int = 2000) -> str:
    """Read the tail of a text file, up to *max_chars* characters."""
    if not os.path.isfile(path):
        return ""
    try:
        with open(path, "r", errors="replace") as f:
            f.seek(0, 2)
            size = f.tell()
            start = max(0, size - max_chars)
            f.seek(start)
            text = f.read()
            return text[-max_chars:] if len(text) > max_chars else text
    except OSError:
        return ""


def _task_relpaths(experiment_name: str, task_id: str) -> tuple[str, str]:
    return (
        os.path.join("experiments", "outputs", experiment_name, "tasks", task_id, "misc", "trajectory.json"),
        os.path.join("experiments", "outputs", experiment_name, "tasks", task_id, "misc", "usage.json"),
    )


def _finished_marker(experiment_name: str, task_id: str) -> str:
    return os.path.join(
        path_store.experiment_outputs, experiment_name, "tasks", task_id, "misc", "finished",
    )


def _write_json_atomic(path: str, obj: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, path)


def _run_worker(task_id: str, args, model_config: dict) -> None:
    """Run a single task in-process and write result to args.part_file."""
    from appworld_agents.code.simplified.skill_react_code_agent import (
        SimplifiedSkillReActCodeAgent,
    )

    result: dict
    try:
        agent = SimplifiedSkillReActCodeAgent(
            skill_dir=args.skill_dir,
            ignore_multiple_calls=True,
            max_prompt_length=args.max_prompt_length,
            max_output_length=args.max_output_length,
            max_steps=args.max_steps,
            skip_if_finished=False,
            model_config=model_config,
        )
        agent.solve_tasks(
            task_ids=[task_id],
            experiment_name=args.experiment_name,
            num_processes=1,
        )
        traj_relpath, usage_relpath = _task_relpaths(args.experiment_name, task_id)
        finished = os.path.isfile(_finished_marker(args.experiment_name, task_id))
        result = {
            "status": "completed" if finished else "failed",
            "trajectory_relpath": traj_relpath,
            "usage_relpath": usage_relpath,
        }
    except Exception as e:
        result = {
            "status": "failed",
            "error": f"{type(e).__name__}: {e}",
            "traceback": traceback.format_exc(),
        }
    _write_json_atomic(args.part_file, result)


def _run_single_task_subprocess(
    task_id: str,
    worker_args: list[str],
    env: dict,
    part_path: str,
    stderr_path: str,
) -> tuple[str, int]:
    """Run a single task as a worker subprocess. Returns (task_id, return_code).

    The worker writes its result JSON to *part_path*. stdout is discarded;
    stderr is redirected to *stderr_path* so it does not accumulate in the
    parent's memory.
    """
    bin_dir = os.path.dirname(sys.executable)
    cli = os.path.join(bin_dir, "appworld-run-batch")
    cmd = [
        cli, *worker_args,
        "--worker",
        "--part-file", part_path,
        "--task-ids", task_id,
    ]
    with open(stderr_path, "w") as stderr_f:
        result = subprocess.run(
            cmd,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=stderr_f,
            text=True,
            timeout=3600,
        )
    return task_id, result.returncode


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run a batch of AppWorld tasks and write results as JSON"
    )
    parser.add_argument(
        "--task-ids",
        nargs="+",
        default=[],
        help="Task IDs to run",
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
        "--skill-dir",
        required=True,
        help="Path to the skill directory",
    )
    parser.add_argument(
        "--model",
        default="anthropic/glm-5",
        help="Model name (default: anthropic/glm-5)",
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help="API key for the model provider",
    )
    parser.add_argument(
        "--base-url",
        default=None,
        help="Base URL for the model provider",
    )
    parser.add_argument(
        "--max-steps",
        type=int,
        default=50,
        help="Max steps per task (default: 50)",
    )
    parser.add_argument(
        "--max-prompt-length",
        type=int,
        default=60000,
        help="Max prompt length (default: 60000)",
    )
    parser.add_argument(
        "--max-output-length",
        type=int,
        default=20000,
        help="Max output length (default: 20000)",
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=0.7,
        help="Sampling temperature (default: 0.7)",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=10000,
        help="Max completion tokens per LLM call (default: 10000)",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=1,
        help="Number of concurrent task slots (default: 1, sequential)",
    )
    parser.add_argument(
        "--result-file",
        default=None,
        help="Path to write the result JSON file (omit in worker mode)",
    )
    parser.add_argument(
        "--worker",
        action="store_true",
        default=False,
        help=argparse.SUPPRESS,  # Internal flag: this process is a worker spawned by the coordinator
    )
    parser.add_argument(
        "--part-file",
        default=None,
        help=argparse.SUPPRESS,  # Internal: worker writes its single-task result here
    )
    parser.add_argument(
        "--skip-if-finished",
        action="store_true",
        default=False,
        help="Skip already finished tasks",
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
        print("No task IDs provided (use --task-ids or --task-ids-file)", file=sys.stderr)
        sys.exit(1)

    # Build model config
    api_config = load_api_config()
    os.environ.setdefault("MODEL_SERVER_URL", api_config.get("BASE_URL", ""))

    model_config: dict = {
        "name": args.model,
        "client_name": "openai",
        "temperature": args.temperature,
        "max_tokens": args.max_tokens,
        "retry_after_n_seconds": 10,
        "max_retries": 10,
        "cost_per_token": {
            "input_cache_miss": 0.0,
            "input_cache_hit": 0.0,
            "input_cache_write": 0.0,
            "output": 0.0,
        },
    }
    if args.api_key:
        model_config["api_key"] = args.api_key
    elif api_config.get("API_KEY"):
        model_config["api_key"] = api_config["API_KEY"]
    if args.base_url:
        model_config["base_url"] = args.base_url
    elif api_config.get("BASE_URL"):
        model_config["base_url"] = api_config["BASE_URL"]

    # ── Worker mode short-circuit ──────────────────────────────────────
    # A worker runs exactly one task and writes its result JSON to
    # --part-file. It must NOT touch parts_dir, result-file, or perform any
    # assembly/cleanup — those belong to the coordinator.
    if args.worker:
        if not args.part_file:
            print("--worker requires --part-file", file=sys.stderr)
            sys.exit(2)
        if len(task_ids) != 1:
            print("--worker expects exactly one --task-ids value", file=sys.stderr)
            sys.exit(2)
        _run_worker(task_ids[0], args, model_config)
        return

    # Filter out already-finished tasks if requested
    if args.skip_if_finished:
        unfinished = []
        for tid in task_ids:
            finished_path = os.path.join(
                path_store.experiment_outputs,
                args.experiment_name,
                "tasks", tid, "misc", "finished",
            )
            if not os.path.isfile(finished_path):
                unfinished.append(tid)
        skipped = len(task_ids) - len(unfinished)
        if skipped > 0:
            print(f"Skipping {skipped} already-finished tasks", file=sys.stderr)
        task_ids = unfinished

    if not task_ids:
        print("All tasks already finished", file=sys.stderr)
        # Write empty result
        result = {"experiment_name": args.experiment_name, "tasks": {}}
        result_dir = os.path.dirname(args.result_file)
        if result_dir:
            os.makedirs(result_dir, exist_ok=True)
        with open(args.result_file, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, ensure_ascii=False)
        return

    # ── Coordinator: incremental result storage on disk ──────────────────
    # Each task result is written to a dedicated part file. The worker only
    # writes to its own part file; the coordinator owns parts_dir lifecycle.
    if not args.result_file:
        print("--result-file is required in coordinator mode", file=sys.stderr)
        sys.exit(2)
    parts_dir = args.result_file + ".parts"
    os.makedirs(parts_dir, exist_ok=True)

    def _part_path(tid: str) -> str:
        return os.path.join(parts_dir, tid + ".json")

    def _stderr_path(tid: str) -> str:
        return os.path.join(parts_dir, tid + ".stderr")

    # ── Run tasks ────────────────────────────────────────────────────────
    completed_count = 0
    failed_count = 0
    total = len(task_ids)

    if args.concurrency <= 1:
        # Sequential mode: re-instantiate the agent per task so per-task
        # state (trajectory buffers, tokenizer/prompt caches) does not
        # accumulate across tasks.
        from appworld_agents.code.simplified.skill_react_code_agent import (
            SimplifiedSkillReActCodeAgent,
        )

        for i, task_id in enumerate(task_ids, 1):
            status = "failed"
            agent = None
            try:
                agent = SimplifiedSkillReActCodeAgent(
                    skill_dir=args.skill_dir,
                    ignore_multiple_calls=True,
                    max_prompt_length=args.max_prompt_length,
                    max_output_length=args.max_output_length,
                    max_steps=args.max_steps,
                    skip_if_finished=False,
                    model_config=model_config,
                )
                agent.solve_tasks(
                    task_ids=[task_id],
                    experiment_name=args.experiment_name,
                    num_processes=1,
                )
                traj_relpath, usage_relpath = _task_relpaths(args.experiment_name, task_id)
                finished = os.path.isfile(_finished_marker(args.experiment_name, task_id))
                status = "completed" if finished else "failed"
                _write_json_atomic(_part_path(task_id), {
                    "status": status,
                    "trajectory_relpath": traj_relpath,
                    "usage_relpath": usage_relpath,
                })
                if status == "completed":
                    completed_count += 1
                else:
                    failed_count += 1
            except Exception as e:
                _write_json_atomic(_part_path(task_id), {
                    "status": "failed",
                    "error": f"{type(e).__name__}: {e}",
                    "traceback": traceback.format_exc(),
                })
                failed_count += 1
            finally:
                # Drop references so GC can reclaim per-task state.
                agent = None
                gc.collect()
            print(f"  [{i}/{total}] {task_id}: {status}", file=sys.stderr)

    else:
        # Concurrent mode: spawn worker subprocesses. Each worker runs one
        # task and writes to its own part file — it never touches parts_dir
        # or --result-file directly.
        print(f"Running {total} tasks with concurrency={args.concurrency}", file=sys.stderr)

        worker_args = [
            "--experiment-name", args.experiment_name,
            "--skill-dir", args.skill_dir,
            "--model", args.model,
            "--max-steps", str(args.max_steps),
            "--max-prompt-length", str(args.max_prompt_length),
            "--max-output-length", str(args.max_output_length),
            "--temperature", str(args.temperature),
            "--max-tokens", str(args.max_tokens),
        ]
        if args.api_key:
            worker_args.extend(["--api-key", args.api_key])
        if args.base_url:
            worker_args.extend(["--base-url", args.base_url])

        env = os.environ.copy()

        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            # Submit lazily: keep at most ~2x concurrency pending Futures
            # instead of queueing all tasks up-front.
            task_iter = iter(task_ids)
            in_flight: dict = {}
            max_pending = args.concurrency * 2

            def _submit_next() -> bool:
                try:
                    tid = next(task_iter)
                except StopIteration:
                    return False
                fut = pool.submit(
                    _run_single_task_subprocess,
                    tid, worker_args, env,
                    _part_path(tid), _stderr_path(tid),
                )
                in_flight[fut] = tid
                return True

            for _ in range(min(max_pending, total)):
                if not _submit_next():
                    break

            while in_flight:
                for future in as_completed(list(in_flight)):
                    tid = in_flight.pop(future)
                    try:
                        _, returncode = future.result()
                    except Exception as e:
                        _write_json_atomic(_part_path(tid), {
                            "status": "failed",
                            "error": f"{type(e).__name__}: {e}",
                        })
                        failed_count += 1
                        print(f"  [FAIL {completed_count + failed_count}/{total}] {tid} EXCEPTION: {e}", file=sys.stderr)
                    else:
                        # Worker is expected to have written its part file.
                        # If it didn't (crash / non-zero exit), synthesize one.
                        part = _part_path(tid)
                        if returncode != 0 or not os.path.isfile(part):
                            stderr_snippet = _read_file_tail(_stderr_path(tid), max_chars=2000)
                            _write_json_atomic(part, {
                                "status": "failed",
                                "error": f"subprocess exit code {returncode}",
                                "stderr": stderr_snippet,
                            })
                            failed_count += 1
                            print(f"  [FAIL {completed_count + failed_count}/{total}] {tid} (exit {returncode})", file=sys.stderr)
                        else:
                            # Cross-check the finished marker in case the
                            # worker's own status is stale.
                            finished = os.path.isfile(_finished_marker(args.experiment_name, tid))
                            if finished:
                                completed_count += 1
                                print(f"  [OK   {completed_count + failed_count}/{total}] {tid}", file=sys.stderr)
                            else:
                                failed_count += 1
                                print(f"  [FAIL {completed_count + failed_count}/{total}] {tid} (no finished marker)", file=sys.stderr)
                    # Release the stderr file immediately.
                    sf = _stderr_path(tid)
                    if os.path.isfile(sf):
                        try:
                            os.remove(sf)
                        except OSError:
                            pass
                    # Keep the pipeline filled.
                    _submit_next()
                    break  # re-enter as_completed on the smaller set

    # ── Assemble results from part files (streaming write) ──────────────
    # We stream one part at a time into the final JSON so we never hold all
    # task results in memory simultaneously.
    result_dir = os.path.dirname(args.result_file)
    if result_dir:
        os.makedirs(result_dir, exist_ok=True)

    fnames = sorted(f for f in os.listdir(parts_dir) if f.endswith(".json"))
    tmp_out = args.result_file + ".tmp"
    written = 0
    with open(tmp_out, "w", encoding="utf-8") as out:
        out.write("{\n")
        out.write(f'  "experiment_name": {json.dumps(args.experiment_name)},\n')
        out.write('  "tasks": {\n')
        for idx, fname in enumerate(fnames):
            tid = fname[:-len(".json")]
            part = os.path.join(parts_dir, fname)
            try:
                with open(part, "r", encoding="utf-8") as f:
                    obj = json.load(f)
            except (OSError, json.JSONDecodeError):
                continue
            sep = "," if idx < len(fnames) - 1 else ""
            out.write(f"    {json.dumps(tid)}: {json.dumps(obj, ensure_ascii=False)}{sep}\n")
            written += 1
            del obj
        out.write("  }\n")
        out.write("}\n")
    os.replace(tmp_out, args.result_file)

    # Clean up part files (coordinator-owned; safe now).
    shutil.rmtree(parts_dir, ignore_errors=True)

    print(
        f"Run batch done: {completed_count} completed, {failed_count} failed out of {written} tasks",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
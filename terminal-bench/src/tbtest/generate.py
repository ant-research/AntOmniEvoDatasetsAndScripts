"""Terminal-Bench 2 generation runner.

Thin CLI around ``harbor run`` that:

  1. Defaults to the Terminus-KIRA agent — points Harbor at
     ``test/job-config-kira.yaml`` (skill mount, no PI install), imports
     ``spec.kira.agent.kira_agent:AgentHarness``, and uses ``spec/kira`` as
     the per-rollout skill source. Pass KIRA config/import/spec-dir explicitly
     to override; the legacy Pi agent is reached via the Pi triplet
     (``-c test/job-config.yaml -a tbtest.custom_agent:PiAgent --spec-dir
     spec/pi``).
  2. Forces a deterministic job name so we know exactly where results land.
  3. After Harbor exits, walks every trial's ``agent/trajectory.json`` (KIRA)
     or ``agent/pi.txt`` (Pi, legacy) and writes an auto-opt ``trajectory.json``
     in the format defined by :mod:`tbtest.trajectory`.

Installed as the ``generate`` console script (see ``pyproject.toml``):

    uv run generate                            # full dataset
    uv run generate -i "adaptive-*"            # one task family
    uv run generate -l 5 -n 8                  # first 5 tasks, 8 parallel
    uv run generate -j run-1102 -o /tmp/jobs   # custom job name + output dir
"""

from __future__ import annotations

import argparse
import atexit
import json
import logging
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from typing import Any
from pathlib import Path

from tbtest.trajectory import Span, Trajectory
from tbtest.trajectory_parser import parse_pi_json_output

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_PKG_ROOT = Path(__file__).resolve().parent          # src/tbtest/
_SRC_DIR = _PKG_ROOT.parent                          # src/
_REPO_ROOT = _SRC_DIR.parent                         # <repo>/
DEFAULT_DATASET = _REPO_ROOT / "dataset2.1"
# KIRA (Terminus2 + native tool-calling) is the default agent — the three
# defaults below must move together: the KIRA config (no PI install), the
# `spec/kira` rollouts dir (KIRA's skill/, no extension/), and the KIRA agent
# import path. To run the legacy Pi agent instead, pass all three explicitly:
#   -c test/job-config.yaml -a tbtest.custom_agent:PiAgent --spec-dir spec/pi
DEFAULT_CONFIG = _REPO_ROOT / "test" / "job-config-kira.yaml"
DEFAULT_SPEC_DIR = _REPO_ROOT / "spec" / "kira"
DEFAULT_AGENT_IMPORT = "spec.kira.agent.kira_agent:AgentHarness"

# Pi writes its NDJSON here inside the container; Harbor mounts it out to
# ``<trial_dir>/agent/pi.txt``.
_PI_OUTPUT_NAME = "pi.txt"
 
# Container mount target → subdir under spec-dir. Keep in sync with
# ``tbtest.custom_agent`` (which reads from these paths at install time).
_SPEC_MOUNTS: dict[str, str] = {
    "/skill": "skill",
    "/extension": "extension",
}


def _resolve_harbor() -> str:
    """Resolve the ``harbor`` executable.

    Harbor is installed alongside this package's console script (and
    alongside ``sys.executable`` when running from a venv). Relying on
    ``$PATH`` breaks when the CLI is launched without the venv activated
    — e.g. ``/path/to/.venv/bin/generate`` invoked directly — because
    ``subprocess.run(["harbor", ...])`` then can't find the binary.

    We look in the same ``bin`` dir as ``sys.executable``. Note we use
    the path *verbatim* (no ``.resolve()``): in a venv created with the
    default symlinks, ``sys.executable`` is ``.venv/bin/python`` → a
    symlink to the system interpreter, so resolving would carry us out
    of the venv's ``bin`` dir and we'd miss the ``harbor`` shim.
    """
    candidate = Path(sys.executable).parent / "harbor"
    if candidate.exists():
        return str(candidate)
    found = shutil.which("harbor")
    return found or "harbor"


# ---------------------------------------------------------------------------
# CLI plumbing
# ---------------------------------------------------------------------------

def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="generate",
        description=(
            "Run Harbor with the Terminus-KIRA agent over the Terminal-Bench 2 "
            "dataset and emit trajectories in auto_opt format."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("-p", "-d", dest="dataset", default=None,
                   help=f"Dataset root (default: {DEFAULT_DATASET}).")
    p.add_argument("-c", dest="config", default=None,
                   help=f"Harbor JobConfig YAML (default: {DEFAULT_CONFIG}).")
    # Harbor's --agent / -a is the unified flag: it accepts either a builtin
    # agent name (pi, codex, …), an ACP registry shorthand (acp:…), or a
    # custom-agent import path (module.path:ClassName). --agent-import-path
    # is a deprecated alias for the same value, so we forward ours to --agent.
    p.add_argument("-a", "--agent", dest="agent_import", default=DEFAULT_AGENT_IMPORT,
                   help=f"Agent name or import path (default: {DEFAULT_AGENT_IMPORT}).")
    p.add_argument("-i", dest="include", action="append", default=[],
                   help="Include task name pattern (repeatable, glob).")
    p.add_argument("-x", dest="exclude", action="append", default=[],
                   help="Exclude task name pattern (repeatable, glob).")
    p.add_argument("-n", dest="n_concurrent", type=int, default=None,
                   help="Concurrent trials (harbor -n).")
    p.add_argument("-l", dest="n_tasks", type=int, default=None,
                   help="Max tasks to run (harbor -l).")
    p.add_argument("-k", dest="n_attempts", type=int, default=None,
                   help="Attempts per trial (harbor -k).")
    p.add_argument("-j", "--job-name", dest="job_name", default=None,
                   help="Job name (default: timestamp).")
    p.add_argument("-o", dest="jobs_dir", default=None,
                   help="Jobs directory (harbor -o). Default from job-config.")
    p.add_argument("-m", dest="model", default=None,
                   help="Model in <provider>/<name> form (overrides job-config).")
    p.add_argument("--debug", action="store_true", help="Enable harbor debug logging.")
    p.add_argument("--force-build", action="store_true",
                   help="Rebuild every task's Docker image.")
    p.add_argument("--spec-dir", dest="spec_dir", default=None,
                   help=(
                       "Directory containing per-rollout skill/ and "
                       f"extension/ subdirs (default: {DEFAULT_SPEC_DIR}). "
                       "The job-config's mounts are rewritten to point at "
                       "this dir before harbor runs, so per-rollout config "
                       "swap is a one-flag operation for training loops."
                   ))
    p.add_argument("--skip-trajectories", action="store_true",
                   help="Skip the post-run trajectory dump step.")
    p.add_argument("extra", nargs=argparse.REMAINDER,
                   help="Extra flags forwarded verbatim to `harbor run` (put after --).")
    return p


def _harbor_argv(args: argparse.Namespace, dataset: Path, config: Path) -> list[str]:
    cmd: list[str] = [
        _resolve_harbor(), "run", "-y",
        "-c", str(config),
        "--agent", args.agent_import,
        "-p", str(dataset),
        "--job-name", args.job_name,
    ]
    for pattern in args.include:
        cmd.extend(["-i", pattern])
    for pattern in args.exclude:
        cmd.extend(["-x", pattern])
    if args.n_tasks is not None:
        cmd.extend(["-l", str(args.n_tasks)])
    if args.n_concurrent is not None:
        cmd.extend(["-n", str(args.n_concurrent)])
    if args.n_attempts is not None:
        cmd.extend(["-k", str(args.n_attempts)])
    if args.jobs_dir:
        cmd.extend(["-o", args.jobs_dir])
    if args.model:
        cmd.extend(["-m", args.model])
    if args.debug:
        cmd.append("--debug")
    if args.force_build:
        cmd.append("--force-build")

    extra = args.extra
    if extra and extra[0] == "--":
        extra = extra[1:]
    cmd.extend(extra)
    return cmd


# ---------------------------------------------------------------------------
# Spec-dir → temp config
# ---------------------------------------------------------------------------

def _rewrite_config_for_spec(config: Path, spec_dir: Path) -> Path:
    """Load ``config``, rewrite skill/extension mount sources to
    subdirs under ``spec_dir``, write to a temp yaml, return its path.

    Subdirs of ``spec_dir`` that don't exist on disk are dropped from the
    mounts list (Docker fails hard on missing bind sources).
    """
    import yaml  # transitively via harbor

    data = yaml.safe_load(config.read_text(encoding="utf-8")) or {}

    # Harbor validates job_name as a string; null causes ValidationError.
    # The CLI --job-name flag overrides this at runtime, but the YAML must
    # parse cleanly first.  Replace null with a placeholder.
    if data.get("job_name") is None:
        logger.info("job_name is null in config; setting placeholder (CLI -j will override)")
        data["job_name"] = "placeholder"

    env = data.setdefault("environment", {})
    mounts = env.get("mounts") or []
    # Drop any pre-existing skill/extension entries — we take over.
    mounts = [m for m in mounts if m.get("target") not in _SPEC_MOUNTS]

    spec_dir = spec_dir.resolve()
    for target, sub in _SPEC_MOUNTS.items():
        src = spec_dir / sub
        if not src.is_dir():
            logger.info(f"spec-dir: {src} missing, skipping mount → {target}")
            continue
        mounts.append({
            "type": "bind",
            "source": str(src),
            "target": target,
            "read_only": True,
        })

    env["mounts"] = mounts
    data["environment"] = env

    fh = tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", delete=False, prefix="tb2-job-config-",
    )
    yaml.safe_dump(data, fh, sort_keys=False)
    fh.close()
    atexit.register(lambda p=fh.name: os.path.exists(p) and os.unlink(p))
    return Path(fh.name)


# ---------------------------------------------------------------------------
# Trajectory emission
# ---------------------------------------------------------------------------

def _resolve_jobs_dir(cli_jobs_dir: str | None, config: Path) -> Path:
    """Match harbor's own resolution: -o wins; else job-config's `jobs_dir`
    (relative to the config file); else `./jobs` (relative to the config file).
    """
    if cli_jobs_dir:
        p = Path(cli_jobs_dir)
        return p if p.is_absolute() else (config.parent / p).resolve()

    # Best-effort YAML peek — harbor already parsed this yaml for us,
    # but we run BEFORE its process exits so we do a light re-read.
    try:
        import yaml  # pulled in by harbor
        with config.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        jobs_dir = data.get("jobs_dir") or "jobs"
    except Exception:
        jobs_dir = "jobs"
    p = Path(jobs_dir)
    return p if p.is_absolute() else (config.parent / p).resolve()


def _emit_trajectories(job_dir: Path) -> int:
    """Parse every trial's pi.txt (Pi agent) OR harbor trajectory.json (KIRA)
    into a tbtest/auto-opt Span trajectory.json. Returns count written."""
    if not job_dir.is_dir():
        logger.warning(f"Job dir not found; skipping trajectory dump: {job_dir}")
        return 0

    written = 0
    for trial_dir in sorted(job_dir.iterdir()):
        if not trial_dir.is_dir():
            continue
        agent_dir = trial_dir / "agent"
        pi_output = agent_dir / _PI_OUTPUT_NAME
        harbor_traj = agent_dir / "trajectory.json"

        if pi_output.exists():
            # Pi path: parse pi.txt NDJSON → Span.
            try:
                raw = pi_output.read_text(encoding="utf-8")
            except OSError as e:
                logger.warning(f"[{trial_dir.name}] cannot read {pi_output}: {e}")
                continue
            try:
                traj: Trajectory = parse_pi_json_output(raw)
            except Exception as e:
                logger.warning(f"[{trial_dir.name}] parse failed: {e}")
                continue
            traj.trace_id = trial_dir.name
            (agent_dir / "trajectory.json").write_text(
                traj.to_json(indent=2), encoding="utf-8"
            )
            written += 1
        elif harbor_traj.exists():
            # KIRA path: harbor already wrote trajectory.json in its native
            # Step format — convert to tbtest/auto-opt Span format in place,
            # preserving the original as trajectory.harbor.json.
            try:
                kira_traj = _kira_steps_to_trajectory(harbor_traj, trial_dir.name)
            except Exception as e:
                logger.warning(f"[{trial_dir.name}] KIRA trajectory convert failed: {e}")
                continue
            (agent_dir / "trajectory.harbor.json").write_text(
                harbor_traj.read_text(encoding="utf-8"), encoding="utf-8"
            )
            (agent_dir / "trajectory.json").write_text(
                kira_traj.to_json(indent=2), encoding="utf-8"
            )
            written += 1

    return written


def _kira_steps_to_trajectory(harbor_traj_path: Path, trace_id: str) -> Trajectory:
    """Convert a harbor-native trajectory.json (Step format: {steps:[...]})
    into a tbtest/auto-opt Trajectory (Span tree).

    The converter is the SINGLE place where harbor's step stream is mapped to
    the auto-opt Span tree that downstream (auto_opt `_parse_trajectory`,
    analysis proposer, training) consumes. The goal: every module that ran
    during the agent loop becomes a Span whose ``input``/``output`` let a
    reader reconstruct "what was the agent doing and what did it see".

    ── Mapping (the canonical module → span table) ──────────────────────────
    harbor Step.source            → Span
    ─────────────────────────────────────────────────────────────────────────
    "user"                        → top-level input span  (the task prompt)
                                    Also: each reminder pushed to the model (e.g.
                                    the time-budget 30/20/10/5-min notices, the
                                    no-tool-calls / malformed-tool-call notices)
                                    is emitted by kira_agent.py's reminder system
                                    as its own source="user" Step whose message is
                                    the reminder text and whose extra.reminder
                                    carries {code, message}; mapped here to a
                                    top-level user span with metadata.reminder.
    "agent"                       → model span
                                    output = {text, reasoning, tool_calls}
                                    metadata = {step_id, model_name,
                                                reasoning_effort, llm_call_count,
                                                metrics{prompt/completion/cache
                                                        tokens, cost, logprobs}}
      Step.tool_calls[*]         → child tool_call span of the model span
                                    input  = arguments (e.g. {keystrokes, duration})
                                    output = observation.content (terminal output)
                                    metadata = {tool_call_id, source_call_id,
                                                extra}
    "system"                      → top-level system span
      (context summarization)        output = observation.content AND, if the
                                     observation carries a subagent_trajectory_ref,
                                     metadata.subagent_trajectory_ref (a pointer
                                     to the subagent's own trajectory file).
                                     metadata.context_management = step.extra
                                     ("compaction"/boundary/...).

    ── Fields currently captured from each harbor Step ─────────────────────
    step_id, timestamp, source, model_name, message, reasoning_content,
    reasoning_effort, llm_call_count, tool_calls (id/function_name/arguments),
    observation.results[*] (content, source_call_id, subagent_trajectory_ref),
    metrics (prompt/completion/cached tokens, cost_usd, prompt_token_ids,
    completion_token_ids, logprobs), extra.

    ╔══════════════════════════════════════════════════════════════════════╗
    ║  EXTENDING THE TRAJECTORY (read this before adding a new module)     ║
    ╠══════════════════════════════════════════════════════════════════════╣
    ║ The agent's full run should be reconstructable from trajectory.json. ║
    ║ When you add a new runtime module to the agent, make its activity    ║
    ║ observable as a Span. Two cases:                                     ║
    ║                                                                      ║
    ║  A. The module runs INSIDE a harbor episode (turn).                  ║
    ║     → emit a harbor Step in kira_agent.py (see the EXTENDS-TRAJECTORY ║
    ║       markers there) OR attach a ToolCall/ObservationResult to the    ║
    ║       current agent Step. The converter already maps tool_calls and   ║
    ║       system steps to Spans, so if your module is a new TOOL it shows ║
    ║       up automatically — just register it in TOOLS + handle it in    ║
    ║       _parse_tool_calls + record a Step with tool_calls=[...].        ║
    ║                                                                      ║
    ║  B. The module runs OUTSIDE a normal turn (its own LLM call or       ║
    ║     sub-process): image_read's internal vision call, summarization's ║
    ║     sub-agent, a future verifier-pre-check, etc.                     ║
    ║     → record it as a system/agent Step in kira_agent.py with         ║
    ║       source="system" (sub_agent) or a dedicated source, set         ║
    ╸       observation.results with content + metadata{module, tokens,    ║
    ║       cost}, and (if it nests) a subagent_trajectory_ref pointing    ║
    ║       at a child trajectory file. Then ADD a branch here so the      ║
    ║       converter emits a child Span tree rather than dropping it.     ║
    ║                                                                      ║
    ║  C. A purely host-side module (skill preload, prompt assembly,        ║
    ║     marker-polling bookkeeping).                                      ║
    ║     → no per-call Span needed; record aggregate signals (e.g.         ║
    ║       _total_time_saved) as metadata on the run's root/user span.     ║
    ║                                                                      ║
    ║  Rule of thumb: if a future reader needs to answer "did module X run ║
    ║  here and what did it produce?", it must be a Span. If it can be     ║
    ║  inferred from a parent span's I/O, metadata suffices.               ║
    ╚══════════════════════════════════════════════════════════════════════╝
    """
    import uuid
    from datetime import datetime

    def _parse_ts(v):
        if not v or not isinstance(v, str):
            return None
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None

    def _tc_view(tc, obs_text):
        """Build a compact view of a tool_call WITH its execution output merged in,
        so each tool_calls entry is self-contained: name + arguments (input) +
        output (the terminal result). This makes a model span's tool_calls read
        as 'the agent ran command X and saw Y' without needing a separate child
        tool span."""
        tc = tc if isinstance(tc, dict) else {}
        view: dict[str, Any] = {
            "name": tc.get("function_name") or tc.get("name"),
            "input": tc.get("arguments"),
        }
        if obs_text:
            view["output"] = obs_text
        return view

    data = json.loads(harbor_traj_path.read_text(encoding="utf-8"))
    steps = data.get("steps") or data.get("trajectory") or []
    spans: list[Span] = []
    # parent chain: last agent model span id (so consecutive agent spans link)
    last_agent_id: str | None = None

    for step in steps:
        source = step.get("source") or "system"
        message = step.get("message")
        tool_calls = step.get("tool_calls") or []
        observation = step.get("observation") or {}
        obs_results = observation.get("results") or []
        obs_text = "\n\n".join(
            (r.get("content") or "") for r in obs_results if isinstance(r, dict)
        )
        # A subagent (e.g. summarization) embeds its trajectory as a ref —
        # capture it so the converter can later expand a child span tree.
        subagent_ref = None
        source_call_ids: list[str | None] = []
        for r in obs_results:
            if isinstance(r, dict):
                ref = r.get("subagent_trajectory_ref")
                if ref and subagent_ref is None:
                    subagent_ref = ref
                if r.get("source_call_id"):
                    source_call_ids.append(r.get("source_call_id"))
        metrics = step.get("metrics") or {}
        ts = _parse_ts(step.get("timestamp"))
        span_kw_common = {"start_time": ts} if ts else {}
        step_extra = step.get("extra")

        if source == "agent":
            model_span = Span(
                id=str(uuid.uuid4()),
                parent_span_id=last_agent_id,
                name="model",
                span_type="model",
                input=None,  # the user prompt that prompted this turn
                output={
                    "text": message,
                    "reasoning": step.get("reasoning_content"),
                    "tool_calls": [_tc_view(tc, obs_text) for tc in tool_calls],
                },
                **span_kw_common,
                end_time=ts,
                metadata={
                    "step_id": step.get("step_id"),
                    "model_name": step.get("model_name"),
                    "reasoning_effort": step.get("reasoning_effort"),
                    "llm_call_count": step.get("llm_call_count"),
                    "metrics": metrics,
                    "extra": step_extra,
                },
            )
            spans.append(model_span)
            last_agent_id = model_span.id
        else:
            # user / system step → top-level span. For system steps that carry a
            # subagent_trajectory_ref (context summarization), preserve both the
            # handoff text (as output) and the ref + context-management tag (as
            # metadata), so the subagent's nested trajectory stays reachable.
            # A step may carry either (or neither) of two extra tags:
            #   - extra.reminder           → a reminder pushed to the model
            #                                  (source="user", emitted by the
            #                                  reminder system in kira_agent.py)
            #   - extra.context_management → context summarization handoff
            # Map each to its own metadata key so the two stay distinguishable.
            meta: dict[str, Any] = {
                "step_id": step.get("step_id"),
            }
            if subagent_ref is not None:
                meta["subagent_trajectory_ref"] = subagent_ref
            if step_extra:
                if isinstance(step_extra, dict):
                    if "reminder" in step_extra:
                        meta["reminder"] = step_extra["reminder"]
                    if "context_management" in step_extra:
                        meta["context_management"] = step_extra["context_management"]
                else:
                    meta["context_management"] = step_extra
            spans.append(Span(
                id=str(uuid.uuid4()),
                parent_span_id=None,
                name=source,
                span_type="span",
                input=message,
                output=obs_text or None,
                **span_kw_common,
                end_time=ts,
                metadata=meta,
            ))

    sess = data.get("session_id")
    # harbor stores no explicit session_id in the trajectory file; synthesize one.
    if not sess:
        sess = trace_id
    return Trajectory(root_span_list=spans, session_id=sess, trace_id=trace_id)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
    )
    args = _build_argparser().parse_args()

    dataset = Path(args.dataset) if args.dataset else DEFAULT_DATASET
    base_config = Path(args.config) if args.config else DEFAULT_CONFIG

    if not dataset.is_dir():
        print(f"dataset not found: {dataset}", file=sys.stderr)
        return 1
    if not base_config.is_file():
        print(f"job-config not found: {base_config}", file=sys.stderr)
        return 1

    # Resolve spec-dir → rewrite mounts if the dir exists.
    spec_dir = Path(args.spec_dir) if args.spec_dir else DEFAULT_SPEC_DIR
    if spec_dir.is_dir():
        config = _rewrite_config_for_spec(base_config, spec_dir)
        logger.info(f"spec-dir: {spec_dir} → temp config {config}")
    else:
        logger.info(f"spec-dir: {spec_dir} missing, using base config verbatim")
        config = base_config

    if not args.job_name:
        args.job_name = datetime.now().strftime("run__%Y-%m-%d__%H-%M-%S")
    logger.info(f"job name: {args.job_name}")

    cmd = _harbor_argv(args, dataset, config)
    # Relative paths inside the yaml (jobs_dir, mount sources unless
    # spec-dir rewrote them) resolve against the *original* config's
    # directory — the tempfile's location is irrelevant.
    workdir = base_config.parent
    print("$ cd", shlex.quote(str(workdir)), "&&", " ".join(shlex.quote(c) for c in cmd), flush=True)

    # Ensure the repo root is importable by the harbor subprocess: agents whose
    # code lives at the repo root (e.g. spec.kira.agent.kira_agent) otherwise
    # fail to import because harbor runs with cwd = the config's dir (test/).
    repo_root = str(_REPO_ROOT)
    pp = os.environ.get("PYTHONPATH", "")
    parts = [p for p in pp.split(os.pathsep) if p]
    for root_path in (repo_root,):
        if root_path and root_path not in parts:
            parts.insert(0, root_path)
    # Also put the --spec-dir's PARENT on the path so an agent import path like
    # ``spec.agent.kira_agent`` can resolve to a candidate spec dir laid out as
    # <candidate>/spec/agent/kira_agent.py (auto-opt per-candidate agent code).
    # For the default ``spec/kira`` usage this parent is the repo root (already
    # added above), so this is a no-op there.
    spec_parent = str(spec_dir.resolve().parent)
    # Put the spec dir ITSELF on the path too, so an import path like
    # ``agent.kira_agent`` resolves to <spec_dir>/agent/kira_agent.py. auto-opt
    # candidate specs are laid out as <cand>/spec/{agent,skill}/ — the agent
    # code lives directly under spec/agent/, with no ``spec.kira`` package
    # wrapper around it. For the default ``spec/kira`` usage this is harmless
    # (spec/kira/agent/ is importable as ``agent.`` but the default import path
    # stays ``spec.kira.agent.kira_agent``, resolved via repo_root above).
    spec_dir_abs = str(spec_dir.resolve())
    for p in (spec_dir_abs, spec_parent):
        if p and p not in parts:
            parts.insert(0, p)
    os.environ["PYTHONPATH"] = os.pathsep.join(parts)

    rc = subprocess.run(cmd, cwd=workdir).returncode

    # Always attempt the trajectory dump — even on non-zero exit some
    # trials may have finished successfully.
    if not args.skip_trajectories:
        job_dir = _resolve_jobs_dir(args.jobs_dir, base_config) / args.job_name
        logger.info(f"dumping trajectories from {job_dir}")
        written = _emit_trajectories(job_dir)
        logger.info(f"wrote {written} trajectory.json file(s)")

    return rc


if __name__ == "__main__":
    raise SystemExit(main())

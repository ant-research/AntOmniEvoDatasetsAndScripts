"""Terminus-KIRA agent — native tool-calling variant of Terminus2.

────────────────────────────────────────────────────────────────────────────────
TRAJECTORY & EXTENSIBILITY  — read before adding a module / tool / middleware
────────────────────────────────────────────────────────────────────────────────
The whole run must be reconstructable from ``<trial>/agent/trajectory.json``.
The agent emits its activity as harbor ``Step`` objects (``self._trajectory_steps``)
which ``tbtest.generate._kira_steps_to_trajectory`` later converts to the
auto-opt Span tree. So: **every runtime module that does meaningful work should
leave a Step**, and the converter knows how to map it. The mapping is documented
in ``generate.py::_kira_steps_to_trajectory``'s docstring.

Per-module, how the trajectory is produced today:

  * **A model turn** (``_run_agent_loop`` each episode) → one ``source="agent"``
    Step via ``_append_step``. Its ``tool_calls`` + ``observation`` become child
    tool_call spans with full I/O. This is the main loop — you do nothing here.
    [EXTENDS-TRAJECTORY: adding a new TOOL] register it in ``TOOLS``, handle it
    in ``_parse_tool_calls``, execute it in the ``image_read`` / commands branch
    of ``_run_agent_loop``, and record a ``ToolCall`` on the step. The converter
    will emit a child tool_call span automatically — no converter change needed.

  * **Context summarization** (``_summarize_or_fallback`` / proactive) → the
    parent Terminus2 emits a ``source="system"`` Step with an
    ``observation.subagent_trajectory_ref`` pointing at the subagent's own
    trajectory file + an ``extra.context_management`` marker. The converter maps
    it to a top-level system span preserving both. No work needed here, but if
    you add a NEW kind of sub-agent, append a ``Step(source="system", ...)``
    with a ``subagent_trajectory_ref`` so its nested trajectory is reachable.

  * **image_read's internal vision LLM call** (``_execute_image_read``) — this
    makes its own ``litellm.acompletion`` call OUTSIDE a normal turn. Today the
    result is folded into the turn's tool_call span output (good enough for I/O),
    but its separate token usage is only added to ``chat._cumulative_*`` and is
    NOT a standalone span. [EXTENDS-TRAJECTORY: if you need the vision call as
    its own observable module] append a ``Step(source="system", model_name=...,
    observation=Observation(results=[ObservationResult(content=<analysis>)]),
    metrics=Metrics(...))`` here, then add a branch in the converter to emit a
    child model span under the agent's tool_call span.

  * **marker-polling bookkeeping** (``_execute_commands`` → ``_total_time_saved``)
    — a host-side optimization signal, not a module with its own input/output.
    It is recorded in ``self._total_time_saved``; if you want it in the
    trajectory, surface it as metadata on the run's root/user span (case C in
    the converter docstring), not as a step.

  * **skill preload / prompt assembly** (parent ``run()`` → ``_build_skills_section``)
    — folded into the initial ``source="user"`` Step (the formatted prompt).
    No separate span needed.

Rule of thumb (also in the converter): if a future reader must answer "did
module X run here and what did it produce?", it must be a Step → Span. If it
can be inferred from a parent turn's I/O, metadata suffices.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import litellm
from harbor.agents.terminus_2 import Terminus2
from harbor.agents.terminus_2.terminus_2 import Command
from harbor.agents.terminus_2.tmux_session import TmuxSession
from harbor.environments.base import BaseEnvironment
from harbor.llms.base import (
    ContextLengthExceededError,
    LLMResponse,
    OutputLengthExceededError,
)
from harbor.llms.chat import Chat
from harbor.models.agent.context import AgentContext
from harbor.models.metric import UsageInfo
from harbor.models.trajectories import (
    Metrics,
    Observation,
    ObservationResult,
    Step,
    ToolCall,
)
from litellm.exceptions import (
    AuthenticationError as LiteLLMAuthenticationError,
    BadRequestError,
    ContextWindowExceededError as LiteLLMContextWindowExceededError,
)
from tenacity import (
    retry,
    retry_if_exception_type,
    retry_if_not_exception_type,
    stop_after_attempt,
    wait_exponential,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants & data classes
# ---------------------------------------------------------------------------

BLOCK_TIMEOUT_SEC = 600
_MARKER_PREFIX = "__CMDEND__"


class BlockError(Exception):
    """Infrastructure API blocked too long."""


@dataclass
class ToolCallResponse:
    content: str | None
    tool_calls: list[dict[str, Any]]
    reasoning_content: str | None = None
    usage: UsageInfo | None = None


@dataclass
class ImageReadRequest:
    file_path: str
    image_read_instruction: str


# Reminder system — "notice → tell the LLM". Each reminder becomes its own
# source="user" trajectory Step (see _emit_reminder), interleaved with model
# spans. Add one by building a Reminder and calling self._emit_reminder(reminder).

@dataclass
class Reminder:
    """A notice pushed to the LLM: a source="user" Step plus feedback to it."""
    code: str               # e.g. "no_tool_calls", "time_30m"
    message: str            # description (trajectory Step)
    span_message: str       # text in the trajectory span
    feedback_text: str = "" # corrective prompt pushed to the model
    delivery: str = "NEXT_TURN"  # "NEXT_TURN" (post-LLM) | "THIS_TURN" (pre-LLM)


# Wall-clock budget: harbor wraps agent.run in asyncio.wait_for. 3600s is also
# stated in the prompt template — keep in sync. Each threshold fires at most once.
AGENT_TIMEOUT_SEC = 3600  # == 60 min, mirrors the prompt's stated budget
TIME_BUDGET_THRESHOLDS_SEC: tuple[int, ...] = (30 * 60, 20 * 60, 10 * 60, 5 * 60)
_REMINDER_DELIVERY_THIS_TURN = "THIS_TURN"
_REMINDER_DELIVERY_NEXT_TURN = "NEXT_TURN"


def _format_duration(seconds: float) -> str:
    """Render a remaining-budget duration as a human string for the prompt."""
    seconds = max(0, int(round(seconds)))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h and m:
        return f"{h}h {m}m"
    if h:
        return f"{h}h"
    if m and s:
        return f"{m}m {s}s"
    if m:
        return f"{m}m"
    return f"{s}s"


# Output-driven checkers: inspect the LLM response after a turn → Reminder (or
# None). Feedback is delivered NEXT_TURN.
ReminderChecker = Any  # (content, tool_calls, reasoning_content, turn_index) -> Reminder | None


def _check_no_tool_calls(
    content: str | None,
    tool_calls: list[dict],
    reasoning_content: str | None,
    turn_index: int,
) -> Reminder | None:
    """Remind the assistant when it produces no structured tool calls.

    A common regression: after several turns, the assistant stops using the
    structured ``tool_calls`` field and instead writes command JSON directly in
    the ``content`` text. Without this reminder, the agent silently does nothing
    (no commands executed) and burns turns until timeout.

    The feedback tells the assistant to use ``execute_commands`` via the tool
    interface, not text output.
    """
    if tool_calls:
        return None
    # Check if the model wrote tool-call-like JSON in its text output
    text = content or ""
    has_text_tool_call = "keystrokes" in text or "execute_commands" in text
    if has_text_tool_call:
        return Reminder(
            code="tool_call_in_text_not_structured",
            message=(
                f"Turn {turn_index}: assistant wrote tool-call JSON in text content "
                f"instead of using the structured tool_calls field. "
                f"Commands will NOT be executed."
            ),
            span_message=(
                "REMINDER: the previous response put commands in the message text "
                "instead of a structured tool call, so they were not executed. Asking "
                "the model to re-issue them via execute_commands."
            ),
            feedback_text=(
                "WARNING: Your previous response put commands in the message text "
                "instead of a structured tool call. Those commands were NOT executed "
                "and the turn did nothing. Text content is never executed — only "
                "tool calls run. Re-issue the commands now as a proper execute_commands "
                "tool call (analysis, plan, commands array)."
            ),
        )
    return Reminder(
        code="no_tool_calls",
        message=(
            f"Turn {turn_index}: assistant produced no tool calls and no text-embedded "
            f"commands. The agent did nothing this turn."
        ),
        span_message=(
            "REMINDER: the previous response contained no tool calls, so the agent "
            "did nothing. Asking the model to call a tool now."
        ),
        feedback_text=(
            "WARNING: Your previous response contained no tool calls, so nothing "
            "happened this turn. Make progress now by calling a tool: execute_commands "
            "to run terminal commands, task_complete when the task is done, or "
            "image_read to analyze an image. Do not reply with only text."
        ),
    )


def _check_empty_keystrokes(
    content: str | None,
    tool_calls: list[dict],
    reasoning_content: str | None,
    turn_index: int,
) -> Reminder | None:
    """Remind the assistant when execute_commands has only empty/whitespace keystrokes.

    The agent sometimes sends execute_commands with empty or whitespace-only
    keystrokes while waiting for output. This is functionally a no-op turn —
    nothing happens in the terminal. The skill rule advises using generous
    initial durations and active polling commands instead.
    """
    if not tool_calls:
        return None
    for tc in tool_calls:
        function_name = tc.get("function", {}).get("name", "")
        if function_name == "execute_commands":
            arguments_str = tc.get("function", {}).get("arguments", "{}")
            try:
                arguments = json.loads(arguments_str) if isinstance(arguments_str, str) else arguments_str
            except json.JSONDecodeError:
                continue
            cmds = arguments.get("commands", [])
            if isinstance(cmds, list) and len(cmds) > 0:
                all_empty = all(
                    isinstance(cmd, dict) and not cmd.get("keystrokes", "").strip()
                    for cmd in cmds
                )
                if all_empty:
                    return Reminder(
                        code="empty_keystrokes",
                        message=(
                            f"Turn {turn_index}: agent sent execute_commands with "
                            f"only empty/whitespace keystrokes. No terminal output "
                            f"was produced."
                        ),
                        span_message=(
                            "REMINDER: the previous execute_commands had no actual "
                            "keystrokes — nothing happened in the terminal. "
                            "Skipped turn without progress."
                        ),
                        feedback_text=(
                            "WARNING: Your previous execute_commands call had only "
                            "empty/whitespace keystrokes, so nothing happened in the "
                            "terminal. This wastes turns without making progress. "
                            "Instead: (1) set a generous duration on the original "
                            "long-running command so it completes within one turn, or "
                            "(2) include an active polling command like `ps aux | grep "
                            "<proc>` or `echo __CHECK__` to capture terminal state. "
                            "Never submit a command batch with only empty keystrokes."
                        ),
                    )
    return None


def _check_qemu_missing_vga(
    content: str | None,
    tool_calls: list[dict],
    reasoning_content: str | None,
    turn_index: int,
) -> Reminder | None:
    """Detect QEMU launch commands missing explicit VGA configuration.

    Verifiers inspect /proc/<pid>/cmdline for VGA-related flags (cirrus, std,
    vga). Without an explicit -vga flag, QEMU's default VGA adapter is
    invisible in the command line, causing verification failures even though
    the VM works. The SKILL.md 'Explicit service configuration' rule already
    names QEMU VGA/display, but prompt-level guidance is insufficient — the
    model consistently omits the flag across two consecutive runs.
    """
    import re
    # Collect all text sources that may contain the QEMU command.
    observation = (content or "") + "\n" + (reasoning_content or "")
    # Also check tool call arguments for the QEMU command itself.
    for tc in (tool_calls or []):
        args_str = tc.get("function", {}).get("arguments", "")
        if isinstance(args_str, dict):
            cmds = args_str.get("commands", [])
            for cmd in cmds:
                observation += "\n" + cmd.get("keystrokes", "")
        elif isinstance(args_str, str):
            observation += "\n" + args_str
    if not re.search(r"qemu-system-", observation):
        return None
    # Check if -vga is already present (covers -vga cirrus, -vga std,
    # -vga qxl, -vga none, etc.)
    if re.search(r"-vga\s", observation):
        return None  # VGA already specified
    # Only fire when we see evidence of a VM launch (disk or cdrom arg)
    if not re.search(r"(-hda\s|-drive\s|-cdrom\s)", observation):
        return None  # Not a VM launch, just a ps listing
    return Reminder(
        code="qemu_missing_vga",
        message=(
            f"Turn {turn_index}: QEMU launched without explicit -vga flag. "
            "Verifiers inspect /proc/<pid>/cmdline for VGA configuration and "
            "will fail if -vga is absent, even though QEMU defaults to a VGA "
            "adapter. Add -vga cirrus (for Windows compatibility) or -vga std "
            "to the QEMU command line."
        ),
        span_message=(
            "REMINDER: QEMU command missing explicit -vga flag. Verifiers check "
            "/proc/<pid>/cmdline for VGA type (cirrus, std, vga). Adding "
            "-vga cirrus or -vga std to the command line."
        ),
        feedback_text=(
            "WARNING: Your QEMU command is missing the -vga flag. Without an "
            "explicit -vga flag, the VGA type does not appear in "
            "/proc/<pid>/cmdline, and verifiers that inspect the command line "
            "will fail with 'No VGA config found'. Add -vga cirrus (recommended "
            "for Windows guests) or -vga std to your qemu-system command. "
            "This is required even though QEMU defaults to a VGA adapter — the "
            "verifier checks the command line, not the runtime behavior."
        ),
    )


def _check_qemu_nonstandard_socket_paths(
    content: str | None,
    tool_calls: list[dict],
    reasoning_content: str | None,
    turn_index: int,
) -> Reminder | None:
    """Detect QEMU launch commands using non-standard socket paths.

    Verifiers hardcode standard socket paths: /tmp/qemu-monitor.sock,
    /tmp/qemu-qmp.sock, /tmp/qemu-serial.sock. Agents sometimes organize
    sockets into subdirectories like /tmp/qemu/monitor.sock, which breaks
    verifier socket discovery. The SKILL.md 'Programmatic control interfaces
    via Unix sockets' rule already specifies the standard paths, but
    prompt-level guidance is insufficient — same pattern as the VGA issue.
    """
    import re
    # Collect all text sources that may contain the QEMU command.
    observation = (content or "") + "\n" + (reasoning_content or "")
    for tc in (tool_calls or []):
        args_str = tc.get("function", {}).get("arguments", "")
        if isinstance(args_str, dict):
            cmds = args_str.get("commands", [])
            for cmd in cmds:
                observation += "\n" + cmd.get("keystrokes", "")
        elif isinstance(args_str, str):
            observation += "\n" + args_str
    if not re.search(r"qemu-system-", observation):
        return None
    # Only fire when we see evidence of a VM launch (disk or cdrom arg)
    if not re.search(r"(-hda\s|-drive\s|-cdrom\s)", observation):
        return None
    # Check for non-standard socket paths in -monitor, -qmp, -serial flags
    nonstandard = []
    # pattern: flag followed by unix:PATH that is NOT /tmp/qemu-{name}.sock
    for flag, std_path in [
        ("-monitor", "/tmp/qemu-monitor.sock"),
        ("-qmp", "/tmp/qemu-qmp.sock"),
        ("-serial", "/tmp/qemu-serial.sock"),
    ]:
        # Find the socket path for this flag
        match = re.search(
            rf"{re.escape(flag)}\s+unix:([^,\s]+)", observation
        )
        if match and match.group(1) != std_path:
            nonstandard.append((flag, match.group(1), std_path))
    if not nonstandard:
        return None
    corrections = "; ".join(
        f"{flag} unix:{std} (not {actual})"
        for flag, actual, std in nonstandard
    )
    return Reminder(
        code="qemu_nonstandard_socket_paths",
        message=(
            f"Turn {turn_index}: QEMU launched with non-standard socket paths. "
            "Verifiers hardcode standard paths and will fail to connect. "
            f"Correct: {corrections}"
        ),
        span_message=(
            "REMINDER: QEMU socket paths must match verifier expectations. "
            f"Correcting: {corrections}"
        ),
        feedback_text=(
            "WARNING: Your QEMU command uses non-standard socket paths. "
            "Verifiers expect these exact paths: "
            "-monitor unix:/tmp/qemu-monitor.sock, "
            "-qmp unix:/tmp/qemu-qmp.sock, "
            "-serial unix:/tmp/qemu-serial.sock. "
            "Do NOT place sockets in subdirectories like /tmp/qemu/ — "
            "the verifier hardcodes these flat paths and cannot discover "
            "alternatives. Also do not use alternative names like "
            "qmp.sock or monitor.sock — the hyphen-separated names are "
            "required."
        ),
    )


# Registry of output-driven checkers. Time-driven reminders are produced in the
# loop (_time_reminder_due), not here.
def _check_python_c_multiline(
    content: str | None,
    tool_calls: list[dict],
    reasoning_content: str | None,
    turn_index: int,
) -> Reminder | None:
    """Detect python3 -c with multi-line inline scripts in command keystrokes.

    Multi-line python3 -c scripts are unreliable because shell quoting
    corrupts hex strings, f-strings, dict access, and nested quotes —
    observed producing garbled XOR decoding output when spaces in a hex
    string were shifted. The SKILL.md Error Patterns section already warns
    against this, but prompt-level guidance is insufficient. The fix: write
    the script to a file first (cat << 'PYEOF' > /tmp/script.py), then
    execute it (python3 /tmp/script.py).
    """
    import re
    if not tool_calls:
        return None
    for tc in tool_calls:
        function_name = tc.get("function", {}).get("name", "")
        if function_name not in ("execute_commands", "commands"):
            continue
        arguments_str = tc.get("function", {}).get("arguments", "{}")
        try:
            arguments = json.loads(arguments_str) if isinstance(arguments_str, str) else arguments_str
        except json.JSONDecodeError:
            continue
        cmds = arguments.get("commands", [])
        if isinstance(cmds, str):
            try:
                cmds = json.loads(cmds)
            except json.JSONDecodeError:
                continue
        for cmd in cmds:
            if not isinstance(cmd, dict):
                continue
            keystrokes = cmd.get("keystrokes", "")
            # Match python3 -c (or python -c) where the -c argument spans
            # multiple lines, indicating an inline multi-line script.
            match = re.search(r"python3?\s+-c\s+", keystrokes)
            if not match:
                continue
            # Check if there's a newline in the -c argument body that is
            # NOT just the terminal Enter key at the end. A single-line
            # command like `python3 -c "print(1)"\n` has one trailing
            # newline; a true multi-line script has newlines in the code
            # body before the final newline.
            after_c = keystrokes[match.end():]
            # Strip the trailing newline(s) that are just the Enter key,
            # then check if any newlines remain (indicating multi-line code).
            if "\n" in after_c.rstrip("\n"):
                return Reminder(
                    code="python_c_multiline",
                    message=(
                        f"Turn {turn_index}: python3 -c used with a multi-line "
                        "inline script. Shell quoting frequently corrupts "
                        "hex strings, f-strings, and nested quotes in this "
                        "pattern, producing garbled output that wastes turns "
                        "debugging."
                    ),
                    span_message=(
                        "REMINDER: python3 -c with multi-line content is unreliable. "
                        "Asking the model to write the script to a file first."
                    ),
                    feedback_text=(
                        "WARNING: Your python3 -c command contains multi-line "
                        "content. Shell quoting corrupts hex strings, "
                        "f-strings, dict access, nested quotes, AND regex "
                        "backslashes (\\b becomes \\\\b, breaking word-boundary "
                        "matching) in this pattern. Instead: (1) write the "
                        "script to a file first: cat << 'PYEOF' > /tmp/script.py "
                        "then the script body then PYEOF, (2) run it: "
                        "python3 /tmp/script.py. For scripts with regex patterns, "
                        "prefer base64 encoding: echo BASE64 | base64 -d > "
                        "/tmp/script.py && python3 /tmp/script.py. This avoids "
                        "all shell quoting issues. For short one-liners without "
                        "special characters, python3 -c is fine — this warning "
                        "only fires for multi-line inline scripts."
                    ),
                )
    return None


def _foreground_download_rewrite(commands: list[Command]) -> str | None:
    """Automatically rewrite foreground curl/wget downloads to backgrounded form.

    Foreground downloads (curl -O, wget <url>) block the terminal for the
    entire download duration, creating an unrecoverable death spiral where
    subsequent commands queue behind the download. The _check_foreground_download
    Reminder was insufficient — the model ignored it and ran wget in
    foreground anyway (observed on build-pov-ray span 20). Code-level
    enforcement replaces text reminders with automatic command rewriting,
    the same pattern used by the quote-balance auto-fix and heredoc
    auto-rewrite.

    Returns feedback text if any commands were rewritten, None otherwise.
    """
    import re as _re
    feedback_parts = []
    for _di, _dcmd in enumerate(commands):
        _dks = _dcmd.keystrokes
        dl_type = ""
        # Extract primary commands from ALL segments of the &&/; chain,
        # not just the first segment. Agents frequently write `cd /app &&
        # curl -O URL` and the download in the second segment must be
        # detected. Only check segments after a `cd` prefix — other
        # prefixes (pip, apt, rm) before && are unlikely to precede a
        # download and checking all segments risks false positives from
        # arguments containing 'curl'/'wget' strings.
        _segments = _re.split(r'&&|;', _dks)
        _primary_cmds = [seg.strip().split()[0] if seg.strip() else '' for seg in _segments]
        # Also allow detection when curl/wget is the primary command of
        # ANY segment whose first command is cd (preserving the original
        # guard against `which wget curl` / `pkill curl` false positives).
        _all_cmds = set(_primary_cmds)
        _has_cd_prefix = _primary_cmds and _primary_cmds[0] == 'cd'
        _download_in_chain = _has_cd_prefix and ('curl' in _all_cmds or 'wget' in _all_cmds)
        _primary_cmd = _primary_cmds[0] if _primary_cmds else ''
        # curl download: primary command is curl (or curl appears after cd
        # prefix) AND output goes to a real file (not /dev/null) AND not a
        # HEAD/connectivity check.
        if _primary_cmd == 'curl' or (_has_cd_prefix and 'curl' in _all_cmds):
            _is_output_download = False
            # -O (remote filename) always writes to a file
            if _re.search(r'-\w*O', _dks):
                _is_output_download = True
            # -o <file> or --output <file> where file is not /dev/null
            _o_match = _re.search(r'(?:-\w*o\s+|--output\s+)(\S+)', _dks)
            if _o_match and _o_match.group(1) != '/dev/null':
                _is_output_download = True
            if _is_output_download:
                if not _re.search(r"&\s*$", _dks) and not _re.search(r"\bcurl\s+.*-(sI|I\b|--head|head)", _dks):
                    dl_type = "curl"
        # wget download: primary command is wget (or wget appears after cd
        # prefix) AND not --spider AND not help/version
        if not dl_type and (_primary_cmd == 'wget' or (_has_cd_prefix and 'wget' in _all_cmds)):
            if not _re.search(r"&\s*$", _dks) and not _re.search(r"\bwget\s+.*--spider", _dks) and not _re.search(r"\bwget\s+(-h$|--help$|--version$)", _dks):
                dl_type = "wget"
        if dl_type:
            # Rewrite: wrap in nohup ... & and add a completion marker.
            # Avoid `& &&` which is invalid bash — `&` is a command
            # terminator, so `&&` cannot follow it. Instead, wrap in a
            # subshell `(...)` so the `&&` chains the echo with the
            # nohup'd command's background start, not with a preceding
            # command's exit code.
            _stripped = _dks.rstrip('\n')
            _rewritten = (
                f"(nohup {_stripped} > /dev/null 2>&1 &) "
                f"&& echo '{dl_type}_download_started' "
                f"|| echo '{dl_type}_download_failed'; "
                f"echo '{dl_type}_bg_started'\n"
            )
            commands[_di] = Command(
                keystrokes=_rewritten,
                duration_sec=min(_dcmd.duration_sec, 2.0),  # nohup+& returns instantly
            )
            feedback_parts.append(
                f"NOTE: A {dl_type} file download was automatically "
                f"backgrounded with nohup to prevent terminal blocking. "
                f"The command now runs in the background. Check completion "
                f"with `ps aux | grep {dl_type}` or verify the output file "
                f"exists with `ls -la <output_file>`. Polling for the "
                f"download to finish is more efficient than running it in "
                f"the foreground — foreground downloads block the terminal "
                f"and create an unrecoverable death spiral."
            )
            logger.info(
                "[download-rewrite] Auto-backgrounded %s download "
                "in command %d: %r -> %r",
                dl_type, _di, _dks[:60], _rewritten[:60],
            )
    if feedback_parts:
        return "\n".join(feedback_parts)
    return None


def _check_task_complete_with_failing_tests(
    content: str | None,
    tool_calls: list[dict],
    reasoning_content: str | None,
    turn_index: int,
) -> Reminder | None:
    """Detect task_complete calls when the agent knows about a failing test.

    Agents routinely dismiss test failures as 'unrelated to the main task'
    and call task_complete. This has caused 0-score failures on at least
    build-cython-ext across 4 chain candidates, where test_reconstructed_space_curve
    failed with KeyError: 'pos' but the agent rationalized it as a planarity
    library issue outside their scope. The verifier runs the full suite
    and a single failure scores 0.

    This checker fires when: (1) the model calls task_complete, AND
    (2) the model's reasoning or content mentions a test failure indicator
    (like '1 failed', 'FAILED', 'AssertionError') alongside dismissive
    language ('unrelated', 'not my', 'pre-existing', 'outside scope').
    """
    import re
    if not tool_calls:
        return None
    # Only fires on task_complete calls
    has_task_complete = any(
        tc.get("function", {}).get("name", "") == "task_complete"
        for tc in tool_calls
    )
    if not has_task_complete:
        return None
    # Combine all text sources to check for failure + dismissal pattern
    combined_text = " ".join(
        t for t in [content, reasoning_content] if t
    ).lower()
    if not combined_text:
        return None
    # Check for test failure indicators
    failure_indicators = [
        r'\d+\s+failed',
        r'failed.*test',
        r'test.*failed',
        r'assertionerror',
        r'return code [1-9]',
    ]
    has_failure = any(re.search(p, combined_text) for p in failure_indicators)
    if not has_failure:
        return None
    # Check for dismissive language
    dismissive_patterns = [
        r'unrelated',
        r'not my (responsibility|fault|issue|problem)',
        r'pre-?existing',
        r'outside (my|the) scope',
        r'not related to',
        r'someone else',
        r'library (issue|api|bug)',
        r'dependency (issue|problem|bug)',
    ]
    has_dismissal = any(re.search(p, combined_text) for p in dismissive_patterns)
    if not has_dismissal:
        return None
    return Reminder(
        code="task_complete_with_failing_tests",
        message=(
            f"Turn {turn_index}: task_complete called despite known test failure "
            "that was dismissed as unrelated. The verifier will score 0 "
            "on any task where a non-excluded test fails."
        ),
        span_message=(
            "REMINDER: task_complete called with a known failing test that was "
            "dismissed as unrelated. Every non-excluded test failure is YOUR "
            "responsibility. Investigate and fix the failure."
        ),
        feedback_text=(
            "STOP: You are calling task_complete despite a known test failure "
            "that you dismissed as unrelated or pre-existing. This has caused "
            "zero-score failures on build-cython-ext (4 consecutive attempts). "
            "When the task states tests should pass with specific exclusions, "
            "EVERY non-excluded test failure is in scope — including failures "
            "caused by dependency API changes, transitive library issues, or "
            "errors that appear unrelated to your primary fix. The verifier "
            "runs the full suite and a single failure scores 0.\n\n"
            "REQUIRED ACTIONS before calling task_complete:\n"
            "1. Read the failing test's source code to understand what it exercises\n"
            "2. Read the traceback to identify the exact failing line\n"
            "3. Investigate and fix the root cause — patch the project code, "
            "add a compatibility shim, or fix the dependency integration\n"
            "4. Re-run the test suite and confirm the previously failing test now passes\n"
            "5. Only THEN call task_complete\n\n"
            "Do NOT rationalize failures as 'someone else's bug.' If a test "
            "fails, it is YOUR problem to fix.\n"
            "Do NOT call task_complete again until the failing test passes."
        ),
    )


def _check_background_python_scripts(
    content: str | None,
    tool_calls: list[dict],
    reasoning_content: str | None,
    turn_index: int,
) -> Reminder | None:
    """Detect Python scripts backgrounded with nohup/& that should run in the foreground.

    Agents routinely use nohup python3 script.py > /tmp/out.txt 2>&1 & or
    python3 script.py & for scripts that complete in seconds (pip installs,
    pytest runs, small verification scripts). This wastes 3-5 turns per script
    on polls (sleep + cat + check) and triggers stall alerts when the agent
    forgets to poll. The correct pattern for scripts under ~60 seconds is:
    timeout 120 python3 -u script.py > /tmp/out.txt 2>&1 with duration=120.
    """
    import re
    if not tool_calls:
        return None
    for tc in tool_calls:
        function_name = tc.get("function", {}).get("name", "")
        if function_name not in ("execute_commands", "commands"):
            continue
        arguments_str = tc.get("function", {}).get("arguments", "{}")
        try:
            arguments = json.loads(arguments_str) if isinstance(arguments_str, str) else arguments_str
        except json.JSONDecodeError:
            continue
        cmds = arguments.get("commands", [])
        if isinstance(cmds, str):
            try:
                cmds = json.loads(cmds)
            except json.JSONDecodeError:
                continue
        for cmd in cmds:
            if not isinstance(cmd, dict):
                continue
            keystrokes = cmd.get("keystrokes", "")
            if not re.search(r'python3?\b', keystrokes):
                continue
            has_background = bool(
                re.search(r'nohup\s+.*python', keystrokes)
                or re.search(r'python.*&\s*$', keystrokes.strip())
                or re.search(r'python.*>.*2>&1\s*&', keystrokes)
            )
            is_build = bool(
                re.search(r'(pip\s+install|setup\.py\s+(build|install)|cmake|make|cargo)\b', keystrokes)
            )
            if has_background and not is_build:
                return Reminder(
                    code="background_python_script",
                    message=(
                        f"Turn {turn_index}: Python script backgrounded with nohup/&. "
                        "Non-build Python scripts should run in the foreground "
                        "with timeout and -u flag for unbuffered output."
                    ),
                    span_message=(
                        "REMINDER: Python script backgrounded unnecessarily. "
                        "Asking the model to use foreground execution."
                    ),
                    feedback_text=(
                        "WARNING: You are backgrounding a Python script with nohup/&. "
                        "For scripts that complete in seconds (test runs, "
                        "verification scripts, fix scripts), this wastes "
                        "3-5 turns on polling and often triggers stall alerts. "
                        "Instead, run in the foreground with unbuffered output: "
                        "timeout 120 python3 -u /tmp/script.py > /tmp/out.txt 2>&1 "
                        "with duration set to 120. For pytest runs: "
                        "timeout 120 python3 -u -m pytest tests/ > /tmp/out.txt 2>&1 "
                        "with duration=120. Only background genuinely long-running "
                        "processes (pip install from source, make, model training) "
                        "where foreground execution would block the terminal "
                        "for minutes. Backgrounding short scripts has caused "
                        "50+ turn waste on build-cython-ext due to polling "
                        "spirals and stall alerts."
                    ),
                )
    return None


def _check_grep_single_filetype_for_deprecated_apis(
    content: str | None,
    tool_calls: list[dict],
    reasoning_content: str | None,
    turn_index: int,
) -> Reminder | None:
    """Detect grep commands limited to .py files when searching for deprecated APIs.

    When fixing NumPy 2.x compatibility or other API deprecations, agents
    routinely use `grep --include='*.py'` which misses Cython (.pyx, .pxd),
    SWIG (.i), and other compiled source files. This produces silent failures
    at runtime (e.g., AttributeError: module 'numpy' has no attribute 'int')
    because the deprecated type alias in the .pyx file was never fixed.

    SKILL.md Procedure 8b provides the deprecated-api-search.sh script which
    searches ALL source file types, but agents ignore prompt-level guidance.
    This code-level checker fires a reminder when it detects the anti-pattern.
    """
    import re
    if not tool_calls:
        return None
    # Patterns that suggest a deprecated API search
    _deprecated_patterns = (
        r'np\.int\b', r'n\.int\b', r'numpy\.int\b',
        r'np\.float\b', r'n\.float\b', r'numpy\.float\b',
        r'np\.complex\b', r'n\.complex\b', r'numpy\.complex\b',
        r'np\.bool\b', r'n\.bool\b', r'np\.str\b',
        r'np\.long\b', r'np\.object\b',
        r'deprecated', r'from fractions import gcd',
    )
    _single_type_include = re.compile(r"--include=['\"]?\*\.py['\"]?")
    for tc in tool_calls:
        function_name = tc.get("function", {}).get("name", "")
        if function_name not in ("execute_commands", "commands"):
            continue
        arguments_str = tc.get("function", {}).get("arguments", "{}")
        try:
            arguments = json.loads(arguments_str) if isinstance(arguments_str, str) else arguments_str
        except json.JSONDecodeError:
            continue
        cmds = arguments.get("commands", [])
        if isinstance(cmds, str):
            try:
                cmds = json.loads(cmds)
            except json.JSONDecodeError:
                continue
        for cmd in cmds:
            if not isinstance(cmd, dict):
                continue
            keystrokes = cmd.get("keystrokes", "")
            # Check: grep with --include='*.py' AND a deprecated-API pattern
            if not _single_type_include.search(keystrokes):
                continue
            has_deprecated_pattern = any(
                re.search(p, keystrokes) for p in _deprecated_patterns
            )
            if not has_deprecated_pattern:
                # Also check if the grep target includes numpy/np in the pattern
                has_numpy_ref = bool(
                    re.search(r'n[pq]?\.|numpy\.', keystrokes)
                )
                if not has_numpy_ref:
                    continue
            return Reminder(
                code="grep_single_filetype_deprecated_api",
                message=(
                    f"Turn {turn_index}: grep with --include='*.py' used to search "
                    "for deprecated APIs. This misses Cython (.pyx, .pxd), "
                    "SWIG (.i), and other compiled source files. The "
                    "deprecated-api-search.sh script searches ALL source types."
                ),
                span_message=(
                    "REMINDER: grep limited to .py files misses Cython sources. "
                    "Asking the model to use deprecated-api-search.sh instead."
                ),
                feedback_text=(
                    "WARNING: Your grep command uses --include='*.py', which "
                    "misses Cython (.pyx, .pxd), SWIG (.i), and other compiled "
                    "source files. When searching for deprecated NumPy APIs "
                    "(np.int, np.float, np.complex, etc.), .pyx files are a "
                    "common source of these patterns and MUST be checked. "
                    "Run the search script instead: "
                    "`bash /skill/scripts/deprecated-api-search.sh <directory>`. "
                    "It searches ALL source file types and filters out "
                    "already-fixed replacement types (np.int64, np.float64, "
                    "etc.). Do NOT fall back to manual grep --include='*.py' \u2014 "
                    "it has caused AttributeError failures on build-cython-ext "
                    "where np.int in .pyx files was missed."
                ),
            )
    return None


REMINDER_CHECKERS: list[ReminderChecker] = [
    _check_no_tool_calls,
    _check_empty_keystrokes,
    _check_python_c_multiline,
    _check_task_complete_with_failing_tests,
    _check_background_python_scripts,
    _check_qemu_missing_vga,
    _check_qemu_nonstandard_socket_paths,
    _check_grep_single_filetype_for_deprecated_apis,
    # _check_foreground_download: replaced by _foreground_download_rewrite
    # (code-level enforcement instead of text reminder; the Reminder was
    # consistently ignored — the model ran wget in foreground despite the
    # reminder, blocking the terminal on build-pov-ray).
    # _check_repeated_empty_commands,
    # _check_ignoring_terminal_output,
]


def _check_compiler_warnings_in_output(
    observation_text: str,
) -> Reminder | None:
    """Detect compiler warnings in terminal output after command execution.

    GCC/Clang warnings include [-W<flag>] at the end of the warning line.
    When the agent compiles code and warnings appear, emit a reminder to
    follow the -Wall -Werror procedure from the skill. This moves
    enforcement from prompt-level (which the agent ignored repeatedly on
    build-pmars) to a code-driven reminder.
    """
    import re
    # Count distinct warning flags to gauge warning density. For legacy
    # codebases (pre-ANSI C, old third-party source), dozens of warnings
    # are normal and adding -Werror would block the build entirely. Only
    # fire the reminder when it is genuinely actionable (low warning count
    # suggesting the agent's own code, not mass legacy warnings).
    warning_flags = re.findall(r'\[-W([a-z-]+)', observation_text)
    distinct_flags = set(warning_flags)
    # Heuristic: if there are many distinct warning types OR many total
    # warnings (>=20 occurrences), this is likely a legacy codebase - the
    # reminder would be counterproductive. Skip it. Also skip if the
    # output appears truncated (piped through head) — a truncated view
    # undercounts warnings, and legacy codebases that pipe through head
    # are exactly the case where the reminder is harmful.
    output_truncated = ('| head' in observation_text or observation_text.rstrip().endswith('...'))
    if len(warning_flags) >= 20 or len(distinct_flags) >= 5 or (output_truncated and len(warning_flags) >= 3):
        return None
    # Heuristic: skip when the output contains linker errors (undefined
    # reference, multiple definition, etc.). Linker errors are more
    # critical than compiler warnings — the agent must fix the linker
    # error before the warnings become relevant. Emitting a
    # -Wall -Werror reminder during a linker failure is counterproductive
    # (observed on build-pov-ray: reminder fired while the agent was
    # diagnosing an undefined reference, adding distraction without
    # helping the actual problem).
    _linker_errors = re.findall(
        r'(undefined reference|multiple definition|cannot find -l|ld returned)',
        observation_text,
    )
    if _linker_errors:
        return None
    # Heuristic: skip warnings from auto-generated Cython C code. Warnings
    # referencing symbols prefixed with __pyx_ are in Cython-generated .c
    # files that the agent cannot fix — adding -Werror would break the
    # build, and the agent has no way to suppress these warnings at the
    # source.
    __pyx_refs = re.findall(r'__pyx_\w+', observation_text)
    if __pyx_refs and len(distinct_flags) <= 2:
        return None
    if warning_flags:
        return Reminder(
            code="compiler_warnings",
            message=(
                "Terminal output contains compiler warnings (detected [-W flags). "
                "Reminding the agent to follow the -Wall -Werror procedure from the skill."
            ),
            span_message=(
                "REMINDER: the previous compilation produced warnings. "
                "Asking the model to add -Wall -Werror and suppress unfixable warnings."
            ),
            feedback_text=(
                "WARNING: The compilation output contains compiler warnings "
                "(detected [-W flags in output). Per the build procedure: "
                "(1) add -Wall -Werror to CFLAGS/CXXFLAGS, "
                "(2) if -Werror causes build failures, fix the warnings or "
                "(3) for genuinely unfixable warnings (e.g., variable name "
                "conflict with a built-in, third-party code), suppress with "
                "-Wno-<warning-type> explicitly. Do not leave unsuppressed warnings — "
                "they may mask real bugs."
            ),
            delivery=_REMINDER_DELIVERY_NEXT_TURN,
        )
    return None


def _run_reminder_checkers(
    content: str | None,
    tool_calls: list[dict],
    reasoning_content: str | None,
    turn_index: int,
) -> list[Reminder]:
    """Run all registered output-driven reminder checkers, return triggered ones."""
    reminders: list[Reminder] = []
    for checker in REMINDER_CHECKERS:
        result = checker(content, tool_calls, reasoning_content, turn_index)
        if result is not None:
            reminders.append(result)
            logger.warning(f"[reminder:{result.code}] {result.message}")
    return reminders


# ---------------------------------------------------------------------------
# Tool descriptions (the description text IS the prompt engineering)
# ---------------------------------------------------------------------------

_EXECUTE_COMMANDS_DESC = (
    "Call this to execute commands in the terminal with your analysis and plan."
)
_ANALYSIS_DESC = (
    "Analyze the current state based on the terminal output provided. "
    "What do you see? What has been accomplished? What still needs to be done?"
)
_PLAN_DESC = (
    "Describe your plan for the next steps. What commands will you run and why? "
    "Be specific about what you expect each command to accomplish."
)
_COMMANDS_DESC = "The commands array must contain at least one command with non-empty keystrokes. To wait for a long-running process, include an active polling command (e.g., `echo __CHECK__`, `ps aux | grep <proc>`) instead of empty keystrokes — empty keystrokes produce no terminal output and waste the turn."
_KEYSTROKES_DESC = (
    "String containing the exact keystrokes to send to the terminal. "
    "The text will be used completely verbatim as keystrokes. "
    "Write commands exactly as you want them sent to the terminal. "
    "Most bash commands should end with a newline (\\n) to cause them to execute. "
    "For special key sequences, use tmux-style escape sequences: C-c for Ctrl+C, C-d for Ctrl+D. "
    "Each command's keystrokes are sent exactly as written to the terminal. "
    "Do not include extra whitespace before or after the keystrokes unless it is part of the intended command."
)
_DURATION_DESC = (
    "Number of seconds to wait for the command to complete (default: 1.0) "
    "before the next command will be executed. "
    "On immediate read-only tasks (e.g., cd, ls, cat file) set a duration of 0.1 seconds. "
    "On commands that write files via redirect (e.g., echo > file, cat << 'EOF' > file, printf > file, tee) set a duration of at least 1.0 seconds — the shell must parse the redirect and flush I/O, which is slower than a read-only equivalent. "
    "On other commands (e.g., gcc, find, rustc) set a duration of 1.0 seconds. "
    "On slow commands (e.g., make, python3 [long running script], wget [file]) set an appropriate duration. "
    "It is better to set a smaller duration than a longer duration. "
    "Never wait longer than 60 seconds; prefer to poll to see intermediate result status."
)
_TASK_COMPLETE_DESC = "Call this when the task is complete."
_IMAGE_READ_DESC = (
    "Read and analyze an image file. Use this ONLY for image files that you need to visually analyze. "
    "Do NOT use this for text files — use shell commands (cat, head, etc.) instead. "
    "The image will be sent to the model for visual analysis "
    "and you will receive a text description in the next turn. "
    "For image-based document classification or data extraction (invoices, forms, receipts, letters in JPG/PNG format), "
    "prefer this tool over OCR (tesseract) — the vision model understands document layout and context, "
    "while OCR may produce empty or garbled output on certain image types. "
    "For images containing code, pseudocode, or programming text where exact text extraction matters, "
    "try OCR first (e.g., tesseract <file> stdout 2>/dev/null) — the vision model often fabricates "
    "plausible but entirely incorrect code when reading code images, while OCR (even garbled) preserves "
    "enough signal to reconstruct the intended algorithm. Use this tool only after OCR fails or for "
    "visual context that OCR cannot provide. "
    "If OCR produces empty or unreliable output for an image, fall back to this tool."
)
_FILE_PATH_DESC = "Absolute path to the image file. Supported formats: PNG, JPG, JPEG, GIF, WEBP."
_IMAGE_READ_INSTRUCTION_DESC = (
    "A text instruction describing what you want to learn from the image. Be specific."
)

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "execute_commands",
            "description": _EXECUTE_COMMANDS_DESC,
            "parameters": {
                "type": "object",
                "properties": {
                    "analysis": {"type": "string", "description": _ANALYSIS_DESC},
                    "plan": {"type": "string", "description": _PLAN_DESC},
                    "commands": {
                        "type": "array",
                        "description": _COMMANDS_DESC,
                        "items": {
                            "type": "object",
                            "properties": {
                                "keystrokes": {"type": "string", "description": _KEYSTROKES_DESC},
                                "duration": {"type": "number", "description": _DURATION_DESC},
                            },
                            "required": ["keystrokes"],
                        },
                    },
                },
                "required": ["analysis", "plan", "commands"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "task_complete",
            "description": _TASK_COMPLETE_DESC,
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "image_read",
            "description": _IMAGE_READ_DESC,
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": _FILE_PATH_DESC},
                    "image_read_instruction": {"type": "string", "description": _IMAGE_READ_INSTRUCTION_DESC},
                },
                "required": ["file_path", "image_read_instruction"],
            },
        },
    },
]


# ---------------------------------------------------------------------------
# AgentHarness(Pi) — wait, Terminus2
# ---------------------------------------------------------------------------

class AgentHarness(Terminus2):
    """TerminusKira: native tool-calling variant of Terminus2.

    Overrides the LLM-interaction layer to use ``litellm.acompletion(tools=...)``
    instead of prompting the model for JSON/XML and parsing it. All Terminus2
    infrastructure (tmux session, chat, trajectory, summarization) is inherited.
    """

    def __init__(self, *args, **kwargs):
        # Force skills_dir attribute: Terminus2._build_skills_section reads
        # self.skills_dir to discover /skill/SKILL.md and build an
        # <available_skills> section in the system prompt. BaseAgent does not
        # set it as a direct attribute, so we pull it from kwargs (the
        # job-config agents[].kwargs.skills_dir) with a sane default.
        self.skills_dir = kwargs.get("skills_dir", "/skill")
        super().__init__(*args, **kwargs)
        self._marker_seq = 0
        self._total_time_saved = 0.0
        # Count of consecutive turns that produced zero executable commands.
        # Drives the repeated-empty-commands escalator in
        # _handle_llm_interaction, which brakes no-op loops before they burn
        # the whole agent timeout.
        self._consecutive_empty_turns = 0
        self._force_action_count = 0
        # Reminder system state. _time_start_monotonic is captured in run();
        # _time_reminder_fired tracks which time thresholds have already fired
        # so each fires at most once. _pending_next_turn_reminders holds
        # output-driven reminders whose feedback is delivered on the next turn
        # (crewed by _handle_llm_interaction, cleared by _append_step).
        self._time_start_monotonic: float | None = None
        self._time_reminder_fired: set[int] = set()
        self._pending_next_turn_reminders: list[Reminder] = []
        self._fc_consecutive_count: int = 0
        self._fc_suppressed_count: int = 0
        self._force_action_feedback: str | None = None
        self._consecutive_reasoning_only: int = 0
        self._suppress_reasoning_next_call: bool = False
        self._git_forensics_reminder_fired: bool = False

    @staticmethod
    def name() -> str:
        return "terminus-kira"

    def version(self) -> str | None:
        return "1.0.0"

    # -- prompt/parser override hooks ----------------------------------------

    def _get_parser(self):
        """No parser — native tool calling returns structured tool_calls."""
        return None

    def _get_prompt_template_path(self) -> Path:
        return Path(__file__).parent / "prompt-templates" / "terminus-kira.txt"

    async def _build_skills_section(self, environment: BaseEnvironment) -> str | None:
        """Assemble the initial-prompt context sections appended after the task.

        Name is fixed by the parent Terminus2 hook (Terminus2.run calls
        ``self._build_skills_section(environment)`` and appends the return value
        to the instruction), so it cannot be renamed even though it now returns
        multiple independent sections, not just the skill. Each section is built
        by its own helper and they are concatenated — genuinely parallel, not
        nested:

          - ``_build_skill_section``      → ## Skill + ## Skill Directory
            (SKILL.md body and the skill-dir file manifest; host-side read).
          - ``_build_app_dir_section``    → ## App directory (/app, depth 1)
            (the task's working-directory layout; one container exec up front).
          - ``_build_tests_dir_section``  → ## Test directory (/tests, depth 1)
            (the verifier's test expectations; one container exec up front).

        Returns ``None`` if neither produced anything (so the prompt stays clean).
        """
        skill_section = self._build_skill_section()
        app_section = await self._build_app_dir_section(environment)
        tests_section = await self._build_tests_dir_section(environment)
        sections = [s for s in (skill_section, app_section, tests_section) if s]
        return "\n\n".join(sections) if sections else None

    def _build_skill_section(self) -> str | None:
        """## Skill + ## Skill Directory: SKILL.md body and the skill-dir file
        manifest, read host-side from the spec/kira/skill tree.

        Supports both layouts (resolved relative to the ``/skill`` mount, so the
        emitted paths are correct for either):
          flat:   skill/SKILL.md            → container /skill/...
          nested: skill/<name>/SKILL.md     → container /skill/<name>/...
        """
        skill_root = Path(__file__).resolve().parent.parent / "skill"
        if not skill_root.is_dir():
            return None

        # Support both flat (skill/SKILL.md) and nested (skill/<name>/SKILL.md).
        skill_mds = sorted(
            { *skill_root.glob("SKILL.md"), *skill_root.glob("*/SKILL.md") }
        )

        parts: list[str] = []
        for skill_md in skill_mds:
            skill_dir = skill_md.parent  # host dir holding SKILL.md (+ refs/scripts)
            # Container path of this skill dir: the mount binds spec/skill → /skill,
            # so the in-container path is /skill + (skill_dir relative to skill_root).
            rel = skill_dir.relative_to(skill_root)
            container_dir = "/skill" if str(rel) == "." else f"/skill/{rel.as_posix()}"

            raw = skill_md.read_text(encoding="utf-8")
            if raw.startswith("---"):
                end = raw.find("---", 3)
                if end != -1:
                    raw = raw[end + 3:].strip()
            parts.append(f"## Skill\n\n{raw}")

            # Always tell the agent where the skill dir is and list every file in
            # it with its container path — load on demand, but the manifest is
            # upfront so the agent never has to guess or ls.
            manifest: list[str] = [
                f"- `skill_dir` = `{container_dir}` (bind-mounted, read-only)",
                f"- SKILL.md (shown in full above): `{container_dir}/SKILL.md`",
            ]
            refs = skill_dir / "references"
            if refs.is_dir():
                for fn in sorted(
                    f.name for f in refs.iterdir()
                    if f.is_file() and f.suffix == ".md" and not f.name.startswith(".")
                ):
                    manifest.append(
                        f"- reference `{fn}`: `cat {container_dir}/references/{fn}`"
                    )
            scripts = skill_dir / "scripts"
            if scripts.is_dir():
                for fn in sorted(
                    f.name for f in scripts.iterdir()
                    if f.is_file() and not f.name.startswith(".")
                ):
                    manifest.append(
                        f"- script `{fn}`: run `{container_dir}/scripts/{fn}` via execute_commands"
                    )
            parts.append(
                "## Skill Directory\n\n"
                f"The skill directory is at `{container_dir}` (read-only inside the "
                f"container). Its files:\n\n"
                + "\n".join(manifest)
                + "\n\nLoad only what you need: read references with `cat`, run "
                "scripts with execute_commands."
            )

        if not parts:
            return None
        return "\n\n" + "\n\n".join(parts) + "\n"

    async def _build_app_dir_section(self, environment: BaseEnvironment) -> str | None:
        """## App directory (/app, depth 1): the task's working-directory layout,
        fetched once at prompt-build time via a container exec.

        depth 1 = /app's immediate children only — enough to surface top-level
        sources/build-artifacts/fixtures without dumping the whole tree. Returns
        None if /app is absent or the exec fails, so a task without /app still runs.
        """
        try:
            result = await self._with_block_timeout(
                environment.exec(
                    command="test -d /app || exit 0; ls -ApF /app 2>/dev/null",
                    timeout_sec=30,
                )
            )
        except Exception:
            # BlockError/timeout or exec failure: skip the section, keep the run going.
            # Intentionally does NOT catch CancelledError (it's a BaseException),
            # so task cancellation still propagates.
            return None
        stdout = (getattr(result, "stdout", None) or "").strip() if result else ""
        if not stdout:
            return None
        listing = "\n".join(f"- /app/{e}" for e in stdout.splitlines())
        return (
            "\n\n## App directory (/app, depth 1)\n\n"
            f"{listing}\n\n"
            "Inspect deeper as needed with `ls`/`find`/`cat` via execute_commands. "
            "Before installing or fetching ANY dependency, search this tree (and the "
            "local filesystem) for an already-present source, build artifact, data "
            "file, or test fixture first — reach for the network (apt/pip/git/curl) "
            "only if that local search genuinely finds nothing.\n"
        )

    async def _build_tests_dir_section(self, environment: BaseEnvironment) -> str | None:
        """## Test directory (/tests, depth 1): the verifier's test expectations,
        injected into the initial context so the agent knows what the verifier
        checks BEFORE writing any implementation code.

        Reads the /tests/ directory listing and the content of small test files
        (under 200 lines). This ensures the agent sees exact CWE IDs, function
        signatures, and expected error types that the verifier tests for, without
        relying on the agent to proactively seek out /tests/ (which it routinely
        skips despite SKILL.md Procedure step 2 mandating it). Returns None if
        /tests/ is absent or the exec fails.
        """
        try:
            result = await self._with_block_timeout(
                environment.exec(
                    command="test -d /tests || exit 0; ls -ApF /tests 2>/dev/null",
                    timeout_sec=30,
                )
            )
        except Exception:
            return None
        stdout = (getattr(result, "stdout", None) or "").strip() if result else ""
        if not stdout:
            return None

        listing_lines = [f"- /tests/{e}" for e in stdout.splitlines()]

        # Read the content of small test files (under 200 lines) to surface
        # exact verifier expectations (CWE IDs, function signatures, error
        # types) directly in the initial context. Skip large files to avoid
        # blowing up the prompt.
        _MAX_TEST_FILE_LINES = 200
        file_contents_parts: list[str] = []
        for entry in stdout.splitlines():
            entry = entry.rstrip("*/@")  # strip ls -F suffixes
            if not entry.endswith(".py") and not entry.endswith(".sh"):
                continue
            try:
                cat_result = await self._with_block_timeout(
                    environment.exec(
                        command=(
                            f"test -f /tests/{entry} || exit 0; "
                            f"wc -l /tests/{entry}"
                        ),
                        timeout_sec=10,
                    )
                )
            except Exception:
                continue
            wc_output = (getattr(cat_result, "stdout", None) or "").strip()
            try:
                line_count = int(wc_output.split()[0])
            except (ValueError, IndexError):
                continue
            if line_count > _MAX_TEST_FILE_LINES:
                listing_lines.append(
                    f"  (file /tests/{entry} has {line_count} lines; "
                    f"read with `cat /tests/{entry}`)"
                )
                continue
            try:
                cat_result = await self._with_block_timeout(
                    environment.exec(
                        command=f"cat /tests/{entry}",
                        timeout_sec=10,
                    )
                )
            except Exception:
                continue
            content = (getattr(cat_result, "stdout", None) or "").strip()
            if content:
                file_contents_parts.append(
                    f"### /tests/{entry}\n```\n{content}\n```"
                )

        parts = [
            "## Test directory (/tests, depth 1)\n\n"
            "The verifier's test expectations. These tests define EXACTLY what "
            "the verifier checks — read them FIRST before any implementation "
            "work to avoid wasting turns on blind search.\n\n"
            + "\n".join(listing_lines)
        ]
        if file_contents_parts:
            parts.append(
                "\n\n### Verifier test file contents\n\n"
                + "\n\n".join(file_contents_parts)
            )
        parts.append(
            "\n\nInspect deeper as needed with `ls`/`find`/`cat` via execute_commands. "
            "These tests define the passing criteria — always verify your solution "
            "against them before calling task_complete.\n"
        )
        return "\n".join(parts)

    def _get_error_response_type(self) -> str:
        return "response with valid tool calls"

    def _get_completion_confirmation_message(self, terminal_output: str) -> str:
        instruction = getattr(self, "_original_instruction", "N/A")
        return (
            f"Original task:\n{instruction}\n\n"
            f"Current terminal state:\n{terminal_output}\n\n"
            "Are you sure you want to mark the task as complete?\n\n"
            "[!] Checklist\n"
            "- Have you run the VERIFIER's test suite from /tests/? Execute it NOW if you haven't. "
            "If any test reports FAIL, AssertionError, or a non-zero exit code, the task is NOT complete — "
            "fix the solution first. Paste the test output here as evidence. [TODO/DONE]\n"
            "- Does your solution meet EVERY requirement in the original task above, including ones NOT "
            "illustrated by the examples? The task's examples are NOT exhaustive — the verifier tests "
            "cases the examples don't cover. "
            "To answer [DONE], you MUST enumerate each functional requirement the task names (e.g., join, leave, announce, debug, deploy) "
            "and confirm you have a passing test for EACH one. If a provided eval/helper script only covers some requirements, "
            "the uncovered ones are UNTESTED until you write and run a test for them — they are NOT [DONE]. "
            "Do NOT re-run an already-passing eval script; write NEW tests for the gaps. [TODO/DONE]\n"
            "- Does your solution account for potential changes in numeric values, array sizes, file contents, or configuration parameters? [TODO/DONE]\n"
            "- If the task has BOTH 'add/remove/change X' AND 'preserve/keep/do not alter Y' requirements, have you tested BOTH sides? Agents commonly test only the active requirement and miss that Y is preserved. Write a before/after diff test for Y just as you would for X. [TODO/DONE]\n"
            "- If the task specifies that data should come from or match a particular source (e.g., 'sequences should match the PDB API fasta', 'use the UniProt entry'), have you verified ALL THREE: (a) you are using THAT exact source and not an alternative database (fpbase, GenBank, etc.). (b) For EACH source file you used, your constructed sequence's FIRST amino acid (after only the task's explicit modifications like N-terminal M removal) matches the FIRST amino acid of the raw source file content (after the header line). If your sequence starts at a different position than the raw file, you have skipped content — restore it. (c) Compute the EXPECTED length directly from the raw source file (count the characters after the header line), then apply ONLY the task's explicit modifications to get expected_length. For PDB fastas: expected_length = raw_source_length - number_of_X_chars - number_of_M_removals + (3 × number_of_X_chars). Now count your constructed sequence's actual length. If actual ≠ expected, your X resolution or M removal is WRONG — do NOT use your own construction logic as the 'expected' value. A length mismatch means content was incorrectly added or removed. [TODO/DONE]\n"
            "- Have you verified your solution from the all perspectives of a test engineer, a QA engineer, and the user who requested this task?\n"
            "  - test engineer [TODO/DONE]\n"
            "  - QA engineer [TODO/DONE]\n"
            "  - user who requested this task [TODO/DONE]\n"
            "- Have you checked that every directory where you created deliverable files contains ONLY the files specified in the task — no compiled binaries, test outputs, or other artifacts from your verification steps? Run `ls -la` on each deliverable directory and remove any files not explicitly required. Artifacts from testing (compiled binaries, pip cache, temporary outputs) cause verifier failures when the test inspects directory contents. [TODO/DONE]\n\n"
            "After this point, solution grading will begin and no further edits will be possible. "
            "If ANY checklist item is still [TODO], you MUST run verification commands for it before calling task_complete again — "
            "the confirmation will persist until you call task_complete after addressing all TODO items. "
            "Mark items [DONE] for any requirement you already verified BEFORE this checklist appeared, as long as no code changes were made since that verification. "
            "Do NOT re-verify requirements that already passed — re-running passed tests wastes turns without producing new information. "
            "If ALL checklist items are [DONE], call task_complete again now. "
            "Any tool call other than task_complete or execute_commands will reset this confirmation and waste extra turns."
        )

    # -- run ---------------------------------------------------------------

    async def run(
        self, instruction: str, environment: BaseEnvironment, context: AgentContext
    ) -> None:
        self._original_instruction = instruction

        # The overall wall-clock budget is enforced externally by harbor; we
        # only need the run start (monotonic) here to drive the time-budget
        # reminders injected in _run_agent_loop.
        self._time_start_monotonic = time.monotonic()
        self._time_reminder_fired = set()
        self._pending_next_turn_reminders = []

        await super().run(instruction, environment, context)

    # -- reminder system ---------------------------------------------------

    def _emit_reminder(self, reminder: Reminder) -> None:
        """Record the reminder as its own source="user" Step and stage feedback.

        THIS_TURN feedback is prepended to the current turn's prompt by the loop
        (time reminders); NEXT_TURN feedback is concatenated into `feedback` by
        _handle_llm_interaction (output reminders).
        """
        self._trajectory_steps.append(
            Step(
                step_id=len(self._trajectory_steps) + 1,
                timestamp=datetime.now(timezone.utc).isoformat(),
                source="user",
                message=reminder.span_message or reminder.message,
                extra={"reminder": {"code": reminder.code, "message": reminder.message}},
            )
        )
        logger.info(f"[reminder:{reminder.code}] {reminder.message}")
        if (
            reminder.delivery == _REMINDER_DELIVERY_NEXT_TURN
            and reminder.feedback_text
        ):
            self._pending_next_turn_reminders.append(reminder)

    # -- time-budget reminders ---------------------------------------------

    def _time_remaining_sec(self) -> float:
        """Remaining wall-clock budget, or +inf if tracking is not active."""
        if self._time_start_monotonic is None:
            return float("inf")
        elapsed = time.monotonic() - self._time_start_monotonic
        return AGENT_TIMEOUT_SEC - elapsed  # mirrors the prompt's stated budget

    def _time_reminder_due(self) -> Reminder | None:
        """Return a THIS_TURN reminder when the budget first drops below a
        threshold, else None. Each threshold fires at most once; on a long turn
        that crosses two, emits only the larger.
        """
        remaining = self._time_remaining_sec()
        due = [
            t for t in TIME_BUDGET_THRESHOLDS_SEC
            if remaining < t and t not in self._time_reminder_fired
        ]
        if not due:
            return None
        threshold = max(due)
        self._time_reminder_fired.add(threshold)
        minutes = threshold // 60
        rem_str = _format_duration(remaining)
        feedback = (
            f"⏰ TIME REMINDER: approximately {minutes} minutes remain "
            f"(~{rem_str} of wall-clock budget left). The deadline is enforced "
            f"externally and cannot be extended. Converge now: finish and verify "
            f"the remaining task requirements, avoid starting large or risky "
            f"changes, and call task_complete as soon as the requirements are met."
        )
        return Reminder(
            code=f"time_{minutes}m",
            message=(
                f"Wall-clock budget crossed below {minutes} minutes "
                f"(~{rem_str} remaining). Time reminder pushed to the model."
            ),
            span_message=feedback,
            feedback_text=feedback,
            delivery=_REMINDER_DELIVERY_THIS_TURN,
        )

    # -- block timeout helper ----------------------------------------------

    async def _with_block_timeout(self, coro, timeout_sec: int = BLOCK_TIMEOUT_SEC):
        try:
            return await asyncio.wait_for(coro, timeout=timeout_sec)
        except asyncio.TimeoutError:
            raise BlockError(f"Infrastructure API blocked for {timeout_sec}s")

    # -- marker-polled command execution -----------------------------------

    async def _execute_commands(
        self,
        commands: list[Command],
        session: TmuxSession,
    ) -> tuple[bool, str]:
        """Execute commands with marker-based polling for early completion."""
        for command in commands:
            self._marker_seq += 1
            marker = f"{_MARKER_PREFIX}{self._marker_seq}__"
            start = time.monotonic()

            # Strip any trailing newlines from keystrokes, then append exactly one.
            # Without this, the CMDEND marker echo concatenates with the command
            # on the same line (e.g., `head fileecho '__CMDEND__1__'`),
            # corrupting every command whose keystrokes lack a trailing \n.
            keystrokes = command.keystrokes.rstrip("\n")
            await session.send_keys(f"{keystrokes}\n", block=False, min_timeout_sec=0.0)
            await session.send_keys(f"echo '{marker}'\n", block=False, min_timeout_sec=0.0)

            await asyncio.sleep(min(0.3, command.duration_sec))
            while time.monotonic() - start < command.duration_sec:
                pane_content = await session.capture_pane()
                if marker in pane_content:
                    break
                await asyncio.sleep(0.5)

            saved = command.duration_sec - (time.monotonic() - start)
            if saved > 0.1:
                self._total_time_saved += saved
                self.logger.debug(
                    f"[polling] saved {saved:.1f}s (duration={command.duration_sec:.1f}s) "
                    f"cmd={command.keystrokes!r}"
                )

        output = await session.get_incremental_output()
        markers = {f"{_MARKER_PREFIX}{seq}__" for seq in range(1, self._marker_seq + 1)}
        lines = [line for line in output.split("\n") if not any(m in line for m in markers)]
        output = "\n".join(lines)
        return False, self._limit_output_length(output)

    # -- litellm tool-call extraction --------------------------------------

    def _extract_tool_calls(self, response) -> list[dict[str, Any]]:
        tool_calls = []
        try:
            message = response.choices[0].message
            if hasattr(message, "tool_calls") and message.tool_calls:
                for tc in message.tool_calls:
                    tool_calls.append(
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments,
                            },
                        }
                    )
        except (AttributeError, IndexError):
            pass
        return tool_calls

    def _extract_usage_info(self, response) -> UsageInfo | None:
        try:
            usage = response.usage
            if usage:
                cost = 0.0
                try:
                    cost = litellm.completion_cost(completion_response=response) or 0.0
                except Exception:
                    pass
                return UsageInfo(
                    prompt_tokens=usage.prompt_tokens or 0,
                    completion_tokens=usage.completion_tokens or 0,
                    cache_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
                    cost_usd=cost,
                )
        except (AttributeError, TypeError):
            pass
        return None

    def _parse_tool_calls(
        self, tool_calls: list[dict[str, Any]]
    ) -> tuple[list[Command], bool, str, str, str, ImageReadRequest | None]:
        commands: list[Command] = []
        is_task_complete = False
        feedback = ""
        analysis = ""
        plan = ""
        image_read: ImageReadRequest | None = None

        if not tool_calls:
            feedback = (
                "WARNINGS: Your response contained no tool calls. "
                "Please use execute_commands to run commands."
            )
            return commands, is_task_complete, feedback, analysis, plan, image_read

        for tool_call in tool_calls:
            function_name = tool_call.get("function", {}).get("name", "")
            arguments_str = tool_call.get("function", {}).get("arguments", "{}")
            try:
                arguments = json.loads(arguments_str) if isinstance(arguments_str, str) else arguments_str
            except json.JSONDecodeError:
                self.logger.warning(f"Failed to parse tool arguments: {arguments_str}")
                continue

            # Tool-name alias: the model occasionally names the tool "commands"
            # (matching the parameter property name) instead of "execute_commands".
            # Normalize here so downstream code sees the correct name without
            # depending on the API gateway rejecting and retrying.
            if function_name in ("execute_commands", "commands"):
                if function_name == "commands":
                    logger.info(
                        "[tool-alias] Normalized tool name 'commands' -> 'execute_commands'"
                    )
                analysis = arguments.get("analysis", "")
                plan = arguments.get("plan", "")
                cmds = arguments.get("commands", [])
                if isinstance(cmds, str):
                    try:
                        cmds = json.loads(cmds)
                    except json.JSONDecodeError:
                        cmds = []
                for cmd in cmds:
                    keystrokes = cmd.get("keystrokes", "")
                    duration = cmd.get("duration", 1.0)
                    commands.append(Command(keystrokes=keystrokes, duration_sec=min(duration, 60)))
            elif function_name == "task_complete":
                is_task_complete = True
            elif function_name == "image_read":
                file_path = arguments.get("file_path", "")
                instr = arguments.get("image_read_instruction", "")
                if file_path and instr:
                    image_read = ImageReadRequest(file_path=file_path, image_read_instruction=instr)
                else:
                    feedback = "WARNINGS: image_read requires both file_path and image_read_instruction arguments."
            else:
                feedback = (
                    f"WARNINGS: Unknown function '{function_name}'. "
                    "Please use execute_commands, task_complete, or image_read."
                )
                self.logger.warning(f"Unknown function called: {function_name}")

        return commands, is_task_complete, feedback, analysis, plan, image_read

    # -- LLM calls ---------------------------------------------------------

    @retry(
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=4),
        retry=(
            retry_if_exception_type(Exception)
            & retry_if_not_exception_type(
                (
                    BadRequestError,
                    LiteLLMAuthenticationError,
                    ContextLengthExceededError,
                    OutputLengthExceededError,
                    asyncio.CancelledError,
                )
            )
        ),
        reraise=True,
    )
    async def _call_llm_for_image(
        self, messages: list[dict], model: str, temperature: float, max_tokens: int
    ) -> object:
        return await litellm.acompletion(
            model=model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=900,
            drop_params=True,
            api_base=os.getenv("ANTHROPIC_BASE_URL") or getattr(self._llm, "_api_base", None),
            api_key=os.getenv("ANTHROPIC_API_KEY"),
        )

    async def _execute_image_read(
        self, image_read: ImageReadRequest, chat: Chat, original_instruction: str = ""
    ) -> str:
        if self._session is None:
            raise RuntimeError("Session is not set")

        file_path = image_read.file_path
        result = await self._with_block_timeout(
            self._session.environment.exec(command=f"base64 {file_path}")
        )
        if result.return_code != 0:
            return f"ERROR: Failed to read file '{file_path}': {result.stderr or ''}"

        b64 = (result.stdout or "").replace("\n", "")
        ext = Path(file_path).suffix.lower()
        mime_map = {
            ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".gif": "image/gif", ".webp": "image/webp",
        }
        mime = mime_map.get(ext)
        if mime is None:
            return (
                f"ERROR: Unsupported image format '{ext}'. "
                f"Convert to PNG first, then use image_read on the PNG file."
            )

        multimodal_messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": image_read.image_read_instruction},
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                ],
            },
        ]
        messages = multimodal_messages

        try:
            # [EXTENDS-TRAJECTORY] This is a SEPARATE LLM call outside the main
            # turn loop (case B in generate.___kira_steps docstring). Its tokens
            # are folded into chat._cumulative_* and its text into the turn's
            # tool_call span output today; to make it its own span, append a
            # source="system" Step here with metrics + a converter branch.
            response = await self._call_llm_for_image(
                messages=messages,
                model=self._model_name,
                temperature=self._temperature,
                max_tokens=self._llm.get_model_output_limit(),
            )
        except Exception as e:
            return f"ERROR: {e}"

        response_text = response["choices"][0]["message"]["content"]
        usage = response.get("usage", {})
        if usage:
            chat._cumulative_input_tokens += usage.get("prompt_tokens", 0)
            chat._cumulative_output_tokens += usage.get("completion_tokens", 0)
            prompt_details = usage.get("prompt_tokens_details")
            cached = getattr(prompt_details, "cached_tokens", 0) if prompt_details else 0
            chat._cumulative_cache_tokens += cached or 0
        if response_text is None:
            return (
                f"ERROR: The vision model returned no content for '{file_path}'. "
                f"This may be a transient failure — retry image_read for this file, "
                f"or try with a more specific image_read_instruction."
            )
        return f"File Read Result for '{file_path}':\n{response_text}"

    @retry(
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=4),
        retry=(
            retry_if_exception_type(Exception)
            & retry_if_not_exception_type(
                (
                    BadRequestError,
                    LiteLLMAuthenticationError,
                    ContextLengthExceededError,
                    OutputLengthExceededError,
                    asyncio.CancelledError,
                )
            )
        ),
        reraise=True,
    )
    async def _call_llm_with_tools(self, messages: list[dict]) -> ToolCallResponse:
        completion_kwargs: dict[str, Any] = {
            "model": self._model_name,
            "messages": messages,
            "temperature": self._temperature,
            "tools": TOOLS,
            "timeout": 900,
            "drop_params": True,
        }
        # Resolve the antchat gateway URL: prefer the LLM backend's api_base
        # (set via job-config kwargs.api_base), fall back to the env var injected
        # into the container (agents[].env.ANTHROPIC_BASE_URL) — and finally
        # drop the literal "${ANTHROPIC_BASE_URL}" if harbor didn't substitute it.
        api_base = getattr(self._llm, "_api_base", None)
        if not api_base or api_base.startswith("$"):
            api_base = os.getenv("ANTHROPIC_BASE_URL") or None
        if api_base:
            completion_kwargs["api_base"] = api_base
        # Likewise the API key: litellm reads ANTHROPIC_API_KEY from env, but
        # make it explicit so the antchat gateway key is used regardless of
        # litellm's per-provider key resolution.
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if api_key:
            completion_kwargs["api_key"] = api_key

        _consecutive_ro = getattr(self, '_consecutive_reasoning_only', 0)
        if self._reasoning_effort and _consecutive_ro == 0:
            completion_kwargs["reasoning_effort"] = self._reasoning_effort
            completion_kwargs["temperature"] = 1
        if _consecutive_ro > 0:
            logger.info(
                "[reasoning-suppress] Suppressing reasoning_effort to break "
                "reasoning-only death spiral (consecutive_reasoning_only=%d)",
                _consecutive_ro,
            )

        try:
            response = await litellm.acompletion(**completion_kwargs)
        except LiteLLMContextWindowExceededError:
            raise ContextLengthExceededError()

        message = response.choices[0].message
        content = message.content or ""
        tool_calls = self._extract_tool_calls(response)
        usage_info = self._extract_usage_info(response)

        finish_reason = response.choices[0].finish_reason
        if finish_reason == "length":
            raise OutputLengthExceededError(
                "Response was truncated due to max tokens limit",
                truncated_response=content,
            )

        reasoning_content = getattr(message, "reasoning_content", None)
        return ToolCallResponse(
            content=content,
            tool_calls=tool_calls,
            reasoning_content=reasoning_content,
            usage=usage_info,
        )

    # -- LLM interaction (override Terminus2) ------------------------------

    async def _handle_llm_interaction(
        self,
        chat: Chat,
        prompt: str,
        original_instruction: str = "",
        session: TmuxSession | None = None,
    ) -> tuple[list[Command], bool, str, str, str, LLMResponse, ImageReadRequest | None]:
        """Override: native tool calling instead of JSON/XML parsing.

        NOTE: returns 7-tuple (adds image_read). The override ``_run_agent_loop``
        below unpacks all 7; Terminus2's own loop unpacks only 6, but we always
        run our own loop.
        """
        messages = chat.messages.copy()
        messages.append({"role": "user", "content": prompt})

        try:
            tool_response = await self._call_llm_with_tools(messages)
            self._record_chat_turn(chat, prompt, tool_response)
        except ContextLengthExceededError:
            if not self._enable_summarize:
                raise
            if session is None:
                raise RuntimeError("Cannot handle context length error without session")
            self._unwind_messages_to_free_tokens(chat, target_free_tokens=4000)
            summary_prompt = await self._summarize_or_fallback(chat, original_instruction, session)
            messages = chat.messages.copy()
            messages.append({"role": "user", "content": summary_prompt})
            tool_response = await self._call_llm_with_tools(messages)
            self._record_chat_turn(chat, summary_prompt, tool_response)
        except OutputLengthExceededError:
            error_msg = (
                "ERROR!! Your response was truncated. "
                "Please provide a shorter response with fewer commands."
            )
            chat._messages.extend([
                {"role": "user", "content": prompt},
                {"role": "assistant", "content": "[truncated]"},
                {"role": "user", "content": error_msg},
            ])
            chat.reset_response_chain()
            messages = chat.messages.copy()
            tool_response = await self._call_llm_with_tools(messages)
            self._record_chat_turn(chat, prompt, tool_response)
        except BadRequestError as exc:
            # Non-exist-tool degradation: glm-5 occasionally emits a tool_use
            # whose name is the *plan* field's text content (e.g.
            # "plan\nInstall deno...") instead of "execute_commands". The
            # antchat gateway rejects it as a 400 Bad Request, killing the
            # trial. It's transient, so detect it, emit a reminder, and retry
            # once with a corrective nudge.
            exc_text = str(exc)
            if "non-exist tool" not in exc_text.lower():
                raise  # other BadRequestErrors: keep the original behavior

            nonexist_reminder = Reminder(
                code="non_exist_tool_degradation",
                message=(
                    f"Turn {len(self._trajectory_steps)}: model emitted a malformed "
                    f"tool call rejected as 'non-exist tool' by the gateway "
                    f"(glm-5 tool-calling degradation). Retrying once with a "
                    f"corrective nudge to use structured execute_commands."
                ),
                span_message=(
                    "REMINDER: the previous tool call was malformed — it named a "
                    "non-existent tool and was rejected by the gateway. Asking the "
                    "model to re-issue a clean execute_commands call."
                ),
                feedback_text=(
                    "WARNING: Your previous tool call was malformed — it named a "
                    "non-existent tool and was rejected. Re-issue your next step as a "
                    "single structured execute_commands call: include an `analysis`, "
                    "`plan`, and `commands` array (each command is an object with "
                    "`keystrokes` and `duration`). Do not put the plan text in the "
                    "tool name; the only valid tools are execute_commands, "
                    "task_complete, and image_read."
                ),
                # Delivered inline as the retry nudge below, not via the
                # next-turn queue (this handler already replays it immediately).
                delivery=_REMINDER_DELIVERY_THIS_TURN,
            )
            self._emit_reminder(nonexist_reminder)
            # Replay the corrective feedback as the retry's user nudge.
            correction = (
                f"{nonexist_reminder.feedback_text}\n\nRe-attempt the previous step now."
            )
            chat._messages.append({"role": "user", "content": correction})
            chat.reset_response_chain()
            messages = chat.messages.copy()
            # If summarize-prep altered `prompt`, the original instruction is
            # still in chat history; the retry just asks for a clean tool call.
            tool_response = await self._call_llm_with_tools(messages)
            self._record_chat_turn(chat, correction, tool_response)

        # Reasoning-only AND format-collapse death spiral detection:
        # (a) reasoning_only: model produces reasoning_content but no
        #     tool_calls and no text — degenerate loop.
        # (b) format_collapse: model produces text with embedded
        #     command-like JSON ("commands", "keystrokes") but no
        #     tool_calls — the format-collapse anti-pattern documented
        #     in SKILL.md Error Patterns. The model keeps putting
        #     commands in text content instead of structured tool_calls.
        #     SKILL.md guidance is insufficient — observed 40 consecutive
        #     format-collapse stalls on largest-eigenval that the
        #     SKILL.md rule could not break. Code-level enforcement is
        #     needed.
        # Both patterns are tracked under the same counter; after 2+
        # consecutive occurrences (2 for format-collapse, 3 for
        # reasoning-only), inject _force_action_feedback with a direct
        # imperative requiring a tool call.
        _has_commands_in_text = False
        if (
            not tool_response.tool_calls
            and tool_response.content
            and isinstance(tool_response.content, str)
        ):
            _c = tool_response.content
            _has_commands_in_text = (
                ('"commands"' in _c or "'commands'" in _c)
                and ('"keystrokes"' in _c or "'keystrokes'" in _c)
            )
        _is_reasoning_only = (
            not tool_response.tool_calls
            and (not tool_response.content or not tool_response.content.strip())
            and bool(tool_response.reasoning_content)
        )
        _is_format_collapse = (
            not tool_response.tool_calls
            and _has_commands_in_text
        )
        if _is_reasoning_only or _is_format_collapse:
            self._consecutive_reasoning_only += 1
            if _is_format_collapse:
                logger.warning(
                    "[format-collapse] Turn %d: model put commands in text "
                    "instead of tool_calls (%d consecutive). Injecting "
                    "format-recovery feedback.",
                    len(self._trajectory_steps),
                    self._consecutive_reasoning_only,
                )
            else:
                logger.info(
                    "[reasoning-only] Turn %d: model produced only reasoning "
                    "(%d consecutive). Suppressing reasoning_effort until recovery.",
                    len(self._trajectory_steps),
                    self._consecutive_reasoning_only,
                )
            # Escalate after 2 consecutive stalls for format-collapse
            # (lower threshold — more corrosive than reasoning-only),
            # 3 for reasoning-only.
            _escalation_threshold = 2 if _is_format_collapse else 3
            if self._consecutive_reasoning_only >= _escalation_threshold:
                if _is_format_collapse:
                    escalation = (
                        "CRITICAL FORMAT ERROR: You wrote commands as text "
                        "content instead of using the execute_commands "
                        "tool. This is the format-collapse anti-pattern. "
                        "You MUST produce a structured tool call now. "
                        "Your NEXT response must contain ZERO text content "
                        "and ONLY an execute_commands tool call: "
                        "execute_commands(analysis='...', plan='...', "
                        "commands=[{keystrokes: 'command\\\\n', "
                        "duration: 1.0}]). Do NOT write commands in your "
                        "text content — use the tool interface only."
                    )
                else:
                    escalation = (
                        "CRITICAL: You have produced %d consecutive responses with "
                        "no executable tool calls — the task is stalled. You MUST "
                        "call execute_commands NOW with any command that makes "
                        "progress. Format: execute_commands(analysis='...', "
                        "plan='...', commands=[{keystrokes: 'command\\\\n', "
                        "duration: 1.0}]). Do NOT produce another reasoning-only "
                        "response." % self._consecutive_reasoning_only
                    )
                if self._force_action_feedback:
                    self._force_action_feedback += "\n" + escalation
                else:
                    self._force_action_feedback = escalation
        else:
            if self._consecutive_reasoning_only > 0:
                logger.info(
                    "[reasoning-only] Loop broken after %d reasoning-only turns.",
                    self._consecutive_reasoning_only,
                )
            self._consecutive_reasoning_only = 0

        commands, is_task_complete, feedback, analysis, plan, image_read = (
            self._parse_tool_calls(tool_response.tool_calls)
        )

        # Format-collapse recovery: when the model produces no tool_calls but
        # its text output contains command-like JSON patterns (e.g.,
        # '"commands": [{"keystrokes": "..."}]'), attempt to extract and
        # execute those commands as a fallback. This handles the degenerate case
        # where the model embeds tool-call arguments as plain text instead of
        # using the structured tool-calling API.
        #
        # FC_RECOVERY_LIMIT: when format-collapse recovery has been triggered
        # this many consecutive times, disable it. Continued FC recovery
        # partially reinforces the degenerate behavior (the model gets terminal
        # feedback from extracted/replaced commands), perpetuating the loop.
        # Disabling it produces empty turns, which is harsher but breaks the
        # self-reinforcing cycle more reliably than partial extraction that
        # feeds confusing output back to the model.
        _FC_RECOVERY_LIMIT = 5
        _FC_RESET_INTERVAL = 8
        # FC_RECOVERY_LIMIT: after this many consecutive FC extractions,
        # stop extracting commands for _FC_RESET_INTERVAL turns, then
        # reset the counter to give the model another chance. This avoids
        # the permanent death spiral where fc_count grows monotonically
        # and the model never recovers (observed: 85/97 FC turns suppressed
        # in train-fasttext with limit=3, zero productive output for the
        # entire run). The reset interval lets the model see 'SUPPRESSED'
        # feedback finite times before retrying extraction.

        if tool_response.tool_calls and commands:
            # Model used proper tool_calls interface — conditionally reset
            # FC counter. Only reset if the tool call produced commands that
            # are plausibly task-progressing (non-trivial keystrokes), not if
            # it was a perfunctory call (simple ls/pwd/echo) that merely
            # resets the counter without advancing the task. Perfunctory calls
            # are a common FC-loop bypass: the model alternates between FC
            # and trivial proper calls to stay under the limit.
            _trivial_patterns = ('ls', 'pwd', 'echo', 'cat /app', 'ls -la',
                                 'ps aux', 'wc -l')
            _is_trivial = any(
                c.keystrokes.strip().startswith(p)
                for c in commands
                for p in _trivial_patterns
            )
            if not _is_trivial:
                self._fc_consecutive_count = 0
                self._fc_suppressed_count = 0
            else:
                # Trivial call does not reset FC counter but doesn't
                # increment it either — it's neutral, not a relapse.
                pass
        if not commands and not is_task_complete and tool_response.content:
            content = tool_response.content
            fc_exceeded = getattr(
                self, '_fc_consecutive_count', 0
            ) >= _FC_RECOVERY_LIMIT
            if fc_exceeded:
                _fc_interval = getattr(
                    self, '_fc_suppressed_count', 0
                ) + 1
                self._fc_suppressed_count = _fc_interval
                logger.warning(
                    "[fc-limit] Format-collapse recovery disabled after %d "
                    "consecutive FC turns (%d suppressed). Skipping extraction.",
                    self._fc_consecutive_count,
                    _fc_interval,
                )
                if _fc_interval >= _FC_RESET_INTERVAL:
                    logger.info(
                        "[fc-limit] FC reset interval reached (%d). "
                        "Resetting FC counter to give extraction another chance.",
                        _FC_RESET_INTERVAL,
                    )
                    self._fc_consecutive_count = 0
                    self._fc_suppressed_count = 0
                # Provide explicit feedback that the FC commands were
                # suppressed, so the model understands WHY nothing executed
                # instead of seeing a misleading 'no commands' message.
                feedback = (
                    f"SUPPRESSED: Your response contained commands as text "
                    f"content for the {self._fc_consecutive_count}th "
                    f"consecutive turn. These commands were NOT executed — "
                    f"FC recovery is disabled to break the loop. Your "
                    f"NEXT response must be a structured execute_commands "
                    f"tool call with NO text content. Format: "
                    f"execute_commands(analysis='...', plan='...', "
                    f"commands=[{{keystrokes: 'command\\\\n', duration: 1.0}}])."
                )
            _has_keystrokes = (
                '"keystrokes"' in content
                or "'keystrokes'" in content
            )
            if _has_keystrokes and not fc_exceeded:
                import re as _re
                # Find where the commands array starts, then walk forward
                # counting brackets to find the matching ] (balanced-bracket
                # extraction). The previous regex approach used [^]]*? and
                # \[.*?\] which fail when keystroke strings contain ]
                # characters (common in Python code with list indexing like
                # arr[i:j]). Balanced-bracket counting correctly handles
                # nested brackets inside JSON string values.
                #
                # Handle both JSON-style double-quoted keys ("commands":)
                # and Python-style single-quoted keys ('commands':). The
                # model sometimes produces format-collapsed text with
                # Python-style quoting, which the previous double-quote-only
                # regex silently failed to match, preventing command
                # extraction and leaving the model in an unrecoverable FC
                # loop (observed: 20 FC turns with zero extractions in
                # make-mips-interpreter run for 8c9afe30d890).
                cmd_arr_start = _re.search(
                    r'["\x27]?commands["\x27]?\s*[:=]\s*\[', content
                )
                if cmd_arr_start:
                    arr_pos = cmd_arr_start.end() - 1  # position of '['
                    depth = 0
                    in_string = False
                    escape_next = False
                    arr_end = None
                    for pos in range(arr_pos, len(content)):
                        ch = content[pos]
                        if escape_next:
                            escape_next = False
                            continue
                        if ch == '\\' and in_string:
                            escape_next = True
                            continue
                        if ch == '"':
                            in_string = not in_string
                            continue
                        if in_string:
                            continue
                        if ch == '[':
                            depth += 1
                        elif ch == ']':
                            depth -= 1
                            if depth == 0:
                                arr_end = pos + 1
                                break
                    if arr_end is not None:
                        _arr_text = content[arr_pos:arr_end]
                        _extracted = None
                        try:
                            _extracted = json.loads(_arr_text)
                        except (json.JSONDecodeError, KeyError, TypeError):
                            # Fallback: the model may produce Python-style
                            # single-quoted dicts instead of JSON. Try
                            # three approaches in order:
                            # 1. Normalize single-quoted keys to double-quoted
                            #    and re-parse as JSON.
                            # 2. Try ast.literal_eval on the original text —
                            #    it handles Python-style single-quoted keys
                            #    AND values natively.
                            # 3. Try ast.literal_eval on the key-normalized
                            #    text (handles mixed quoting).
                            #
                            # ast.literal_eval is safe: it only evaluates
                            # literal structures (strings, numbers, tuples,
                            # lists, dicts, booleans, None) — no code exec.
                            try:
                                import re as _re_inner
                                _normalized = _re_inner.sub(
                                    r"'([a-zA-Z_][a-zA-Z_0-9]*)'\s*:",
                                    r'"\1":',
                                    _arr_text
                                )
                                _extracted = json.loads(_normalized)
                            except (json.JSONDecodeError, KeyError, TypeError):
                                pass
                            if _extracted is None:
                                try:
                                    import ast as _ast
                                    _extracted = _ast.literal_eval(_arr_text)
                                except (ValueError, SyntaxError):
                                    pass
                            if _extracted is None:
                                try:
                                    import ast as _ast
                                    _extracted = _ast.literal_eval(_normalized)
                                except (ValueError, SyntaxError, NameError):
                                    pass
                        if _extracted is not None:
                            for cmd in _extracted:
                                if isinstance(cmd, dict) and cmd.get("keystrokes", "").strip():
                                    commands.append(Command(
                                        keystrokes=cmd["keystrokes"],
                                        duration_sec=min(cmd.get("duration", 5.0), 60),
                                    ))
                            if commands:
                                self._fc_consecutive_count = getattr(
                                    self, '_fc_consecutive_count', 0
                                ) + 1
                                feedback = (
                                    "WARNING: Your response contained commands as "
                                    "plain text instead of a structured tool call. "
                                    "The commands were extracted and executed, but "
                                    "you MUST use the tool-calling interface "
                                    "(execute_commands function call) in future "
                                    "turns. Do not write commands as text content. "
                                    "Your NEXT response must be a single structured "
                                    "tool call like: execute_commands(analysis='...', "
                                    "plan='...', commands=[{keystrokes: 'command\\\\n', "
                                    "duration: 1.0}]). NO text in the message content — "
                                    "only the tool call."
                                )
                                if self._fc_consecutive_count >= 2:
                                    feedback += (
                                        f" This is your "
                                        f"{self._fc_consecutive_count}th "
                                        f"consecutive turn producing commands as "
                                        f"text instead of tool calls. The "
                                        f"tool-calling interface is the ONLY way "
                                        f"to execute commands. Your text output "
                                        f"must be empty when making tool calls."
                                    )
                                self.logger.info(
                                    "Recovered %d commands from format-collapse "
                                    "text output. (consecutive FC: %d)",
                                    len(commands),
                                    self._fc_consecutive_count,
                                )

        # Long-heredoc enforcement: heredocs (>1500 chars) are unreliable
        # in tmux — they can get corrupted by tmux rendering (leaving the
        # terminal stuck in heredoc-input mode) or cause terminal congestion
        # from rendering long content. SKILL.md warns against this but the
        # model routinely ignores the rule across runs. Code-level
        # enforcement: detect long heredocs and replace them with the
        # reliable base64-write approach.
        # Pre-execution quote-balance check: detect and auto-fix
        # unbalanced single/double quotes in keystrokes. An unclosed
        # quote causes bash to enter continuation mode (> prompt),
        # trapping all subsequent commands as part of the string and
        # creating a terminal death spiral that wastes 50+ turns.
        # Auto-fix strategy: if a keystroke has an odd number of
        # unescaped quotes of a given type, append the matching close
        # quote before the final newline. This preserves intent while
        # preventing continuation mode.
        for _qi, _qcmd in enumerate(commands):
            _qks = _qcmd.keystrokes
            # Skip heredocs — they have their own quoting rules
            if '<<' in _qks:
                continue
            # Strip bash comments before counting quotes. Comments
            # can contain legitimate apostrophes (doesn't, it's)
            # that make the total quote count appear even even when
            # the actual command portion has an unbalanced quote.
            # Observed false negative: comment "doesn't" balanced out
            # a stray trailing quote in a printf command, causing a
            # 60+ turn terminal death spiral.
            _qks_no_comments = re.sub(r'#[^\n]*', '', _qks)
            for _qchar in ("'", '"'):
                # Count unescaped occurrences of this quote char
                _count = 0
                _escaped = False
                for _ci, _c in enumerate(_qks_no_comments):
                    if _escaped:
                        _escaped = False
                        continue
                    if _c == '\\':
                        _escaped = True
                        continue
                    if _c == _qchar:
                        _count += 1
                if _count % 2 != 0:
                    # Odd number of unescaped quotes — unbalanced.
                    # Auto-fix: insert the closing quote before the
                    # final newline.
                    _fixed = _qks.rstrip('\n')
                    _fixed += _qchar + '\n'
                    logger.warning(
                        "[quote-fix] Auto-fixed unbalanced %s in "
                        "command %d: added closing quote. "
                        "Original ended with: %r",
                        _qchar, _qi, _qks[-40:],
                    )
                    _force_quote_feedback = (
                        "NOTE: A command with an unbalanced quote was "
                        "automatically fixed by appending the closing "
                        "quote. An unclosed quote causes bash to enter "
                        "continuation mode (> prompt), trapping all "
                        "subsequent commands as part of the string and "
                        "creating a terminal death spiral. Always "
                        "verify that echo/printf commands have matching "
                        "opening and closing quotes."
                    )
                    if self._force_action_feedback:
                        self._force_action_feedback += "\n" + _force_quote_feedback
                    else:
                        self._force_action_feedback = _force_quote_feedback
                    commands[_qi] = Command(
                        keystrokes=_fixed,
                        duration_sec=_qcmd.duration_sec,
                    )

        # Foreground-download auto-rewrite: curl -O and wget commands
        # run in the foreground block the terminal until the download
        # completes, creating an unrecoverable death spiral. The
        # _check_foreground_download Reminder was consistently ignored
        # by the model (build-pov-ray span 20: wget ran foreground
        # despite the reminder at span 19). Code-level enforcement
        # automatically rewrites the command to use nohup+&.
        _download_feedback = _foreground_download_rewrite(commands)
        if _download_feedback:
            if self._force_action_feedback:
                self._force_action_feedback += "\n" + _download_feedback
            else:
                self._force_action_feedback = _download_feedback

        _HEREDOC_MAX_LEN = 1500
        for _hi, _hcmd in enumerate(commands):
            _hks = _hcmd.keystrokes
            _hmatch = re.search(r'<<\s*[\'"\']?(\w+)[\'"\']?', _hks)
            if _hmatch and len(_hks) > _HEREDOC_MAX_LEN:
                _delim = _hmatch.group(1)
                _heredoc_start = _hks.index(_hmatch.group(0))
                _first_newline = _hks.find('\n', _heredoc_start)
                _content_start = _first_newline + 1 if _first_newline != -1 else len(_hks)
                _closing = f'\n{_delim}\n'
                _close_pos = _hks.find(_closing, _content_start)
                if _close_pos == -1:
                    _closing = f'\n{_delim}'
                    _close_pos = _hks.rfind(_closing, _content_start)
                _content = _hks[_content_start:_close_pos] if _close_pos != -1 else _hks[_content_start:]
                # Search for redirect target both before the << marker AND
                # between the marker and the first newline. The common format
                # `cat << 'PYEOF' > /app/file.py` places the redirect AFTER
                # the heredoc delimiter on the same line, so searching only
                # before << misses it. The fallback default is /app/script.py.
                _file_match = re.search(r'>\s*(/\S+)', _hks[:_heredoc_start])
                if not _file_match and _first_newline != -1:
                    _file_match = re.search(r'>\s*(/\S+)', _hks[_heredoc_start:_first_newline])
                _target_file = _file_match.group(1) if _file_match else '/app/script.py'
                import base64 as _b64
                _b64_content = _b64.b64encode(_content.encode('utf-8')).decode('ascii')
                _new_ks = f"echo '{_b64_content}' | base64 -d > {_target_file}\n"
                logger.info(
                    "[heredoc-rewrite] Replaced long heredoc (%d chars, "
                    "delim='%s') with base64-write to %s",
                    len(_hks), _delim, _target_file,
                )
                commands[_hi] = Command(
                    keystrokes=_new_ks,
                    duration_sec=max(_hcmd.duration_sec, 1.0),
                )
                self._force_action_feedback = (
                    "NOTE: Your long heredoc command was automatically "
                    "rewritten to use base64 encoding because heredocs "
                    "longer than 1500 characters are unreliable in tmux "
                    "(they get corrupted, leaving the terminal stuck in "
                    "heredoc-input mode and causing 100+ turn death "
                    "spirals). For future file writes, prefer "
                    "`python3 -c \"open('file','w').write(content)\"` "
                    "or `echo BASE64 | base64 -d > file` for reliability."
                )
                break

        # Endianness-change atomicity reminder: when the agent modifies a
        # runtime/VM file and the change involves an endianness convention
        # (readUInt32LE→BE, writeUInt32LE→BE, etc.), inject a reminder to
        # grep for ALL code using the same data with the old convention.
        # SKILL.md has this rule but the model routinely ignores it when in
        # execution/debugging mode — code-level enforcement is needed.
        # Only trigger on commands that WRITE or MODIFY (sed -i, python
        # scripts that open-for-write, content.replace), NOT on read-only
        # commands like grep/cat/head that merely search for the pattern.
        # Detect foreground execution of large-file-reading programs
        # (compiled binaries, python scripts loading checkpoints/models)
        # without output redirect. Running these directly floods the
        # terminal and causes death spirals costing 20+ turns each.
        _LARGE_FILE_PROGRAM_PATTERNS = [
            "/app/a.out", "./a.out", "./program", "./run",
        ]
        # pip install commands that compile C extensions from source can
        # take 30-120 seconds, blocking the terminal. Only trigger on
        # pip install commands that are likely to compile (git+https
        # URLs, or packages known to require compilation) — simple
        # `pip install requests` completes in seconds and is fine.
        _pip_compile_patterns = [
            "pip install git+",
            "pip install --no-binary",
        ]
        # Database CLI commands that may execute long-running queries or produce
        # voluminous output when run in the foreground without output redirect.
        # Running `sqlite3 db < query.sql` or `psql db -f query.sql` blocks the
        # terminal for the entire query duration (potentially 60+ seconds) and
        # floods the buffer if the result set is large. This caused 33+ wasted
        # turns in query-optimize from terminal unresponsiveness + polling.
        _DB_CLI_PATTERNS = [
            "sqlite3 ", "psql ", "mysql ", "mongosh ",
        ]
        # Build commands without output redirection flood the tmux buffer.
        # Build commands (make, cmake --build, cargo build, ninja) produce voluminous
        # output that floods the tmux buffer. SKILL.md step 8a1 instructs
        # redirecting to a file, but the agent routinely ignores it.
        # Code-level enforcement prevents terminal death spirals.
        _BUILD_CMD_PATTERNS = ["make", "cmake --build", "cargo build", "ninja"]
        # Collect indices of foreground-program commands to strip after
        # the detection loop. Stripping inside the loop would skip
        # subsequent commands in the batch.
        _fg_strip_indices = []
        # Track polling patterns: if the agent repeatedly polls the same
        # file or process with short-duration commands across turns,
        # inject feedback to use a single long sleep instead.
        # Key format: 'file:<path>' for tail/cat/ls polling,
        # 'proc:<name>' for ps-aux-grep polling.
        if not hasattr(self, '_polling_log_tracker'):
            self._polling_log_tracker = {}  # poll_key -> count of consecutive polling turns
        for cmd_idx, cmd in enumerate(commands):
            ks = cmd.keystrokes.strip()
            if ks:
                # Detect python3 commands even when prefixed by timeout/nohup/etc.
                _python3_match = re.search(
                    r'(?:timeout\s+\S+\s+|nohup\s+)?python3?\s+', ks
                )
                _python3_is_model = _python3_match and (
                    "model" in ks.lower() or "checkpoint" in ks.lower()
                    or ".ckpt" in ks
                )
                # Short python3 -c one-liners are safe to run in foreground.
                # Long inline scripts (>10 newlines) can flood the terminal.
                _python3_is_short_inline = bool(
                    _python3_match
                    and re.search(r"python3?\s+-c\s+['\"]", ks)
                    and ks.count('\n') <= 10
                )
                # python3 -c inline scripts with >10 newlines are
                # classified as scripts (enforcement applies), but we
                # also classify shorter inline scripts with complex
                # quoting (multiple nested quotes) as scripts because
                # bash quoting corruption in `python3 -c '...'` is the
                # #1 cause of terminal death spirals where the terminal
                # gets stuck in Python multi-line input mode. The
                # enforcement below rewrites them to file-based
                # execution instead of stripping them.
                _python3_inline = bool(
                    _python3_match
                    and re.search(r"python3?\s+-c\s+['\"]", ks)
                )
                # File-based Python scripts (python3 script.py, NO -c flag)
                # don't suffer from bash quoting corruption and should
                # NOT be classified as foreground programs by default.
                # Only strip them when the duration suggests a
                # long-running training/inference script (>=60s) or when
                # the command is an inline -c script (quoting risk).
                # The inline-rewrite path already handles problematic
                # `python3 -c` scripts above.
                _python3_is_script = (
                    _python3_match
                    and not _python3_is_short_inline
                )
                # ALL non-short-inline Python scripts are foreground programs:
                # file-based scripts (python3 script.py) with any duration can
                # block the terminal — a script that hangs in select(), waits for
                # PTY I/O, or enters an infinite loop makes the terminal
                # unresponsive for the full auto-timeout duration. The duration
                # threshold of 60 previously caused a gap: scripts with duration
                # < 60 ran unredirected in the foreground, and a debug PTY script
                # with duration=10 blocked the terminal for 300 seconds (the
                # auto-timeout), creating a 100+ turn death spiral. Inline -c
                # scripts were already caught; now file-based scripts are too.
                #
                # Pre-compute redirect/tee/backgrounded flags needed by the safe
                # foreground exemption below. These must be computed BEFORE
                # _python3_is_safe_fg references them — Python treats any name
                # assigned in a function as local for the entire scope, so
                # referencing before assignment raises UnboundLocalError.
                _redirect_stripped_early = re.sub(r'[0129]>&[0129]', '', ks)
                has_redirect = ">" in _redirect_stripped_early or ">>" in _redirect_stripped_early
                _has_tee_pipe = bool(re.search(r'\|\s*tee\b', ks))
                if _has_tee_pipe:
                    has_redirect = False
                _is_backgrounded = re.search(
                    r'(?:&\s*(?:#.*)?$|nohup\s)', ks
                ) is not None
                # SAFE FOREGROUND EXEMPTION: Python scripts with output redirect
                # (not | tee), explicit timeout, and moderate duration are safe
                # to run in the foreground — redirect prevents terminal flooding,
                # timeout prevents indefinite hangs. Without this exemption,
                # agents are forced into the nohup & sleep && cat pattern which
                # causes polling spirals (observed: 16+ wasted turns on
                # largest-eigenval from repeatedly checking empty output files
                # after backgrounding short test scripts). The exemption aligns
                # the code behavior with the SKILL.md guidance that recommends
                # `timeout python3 script.py > /tmp/output.txt 2>&1`.
                _python3_is_safe_fg = (
                    _python3_is_script
                    and not _python3_inline
                    and has_redirect
                    and not _has_tee_pipe
                    and not _is_backgrounded
                    and 'timeout' in ks.lower()
                    and cmd.duration_sec < 30
                )
                if _python3_is_safe_fg:
                    _python3_is_script = False
                if _python3_inline and not _python3_is_short_inline and _python3_is_script:
                    # Rewrite python3 -c inline scripts to file-based
                    # execution. Extract the script content, write it
                    # to /tmp/inline_N.py via base64, then run that file.
                    # This avoids shell quoting corruption that traps
                    # the terminal in Python multi-line input mode.
                    import base64 as _b64mod
                    _inline_match = re.search(
                        r"python3?\s+-c\s+['\"](.*)['\"]\s*$", ks, re.DOTALL
                    )
                    if _inline_match:
                        _inline_script = _inline_match.group(1)
                        # Unescape common bash escape sequences
                        _inline_script = _inline_script.replace("\\'", "'")
                        _inline_script = _inline_script.replace('\\"', '"')
                        _inline_script = _inline_script.replace('\\n', '\n')
                        _inline_script = _inline_script.replace('\\t', '\t')
                        _inline_b64 = _b64mod.b64encode(
                            _inline_script.encode('utf-8')
                        ).decode('ascii')
                        _inline_idx = sum(
                            1 for c in commands[:cmd_idx]
                            if 'inline_' in c.keystrokes
                        )
                        _inline_file = f'/tmp/inline_{_inline_idx}.py'
                        _new_ks = (
                            f"echo '{_inline_b64}' | base64 -d > {_inline_file} "
                            f"&& timeout 300 python3 {_inline_file}"
                        )
                        # Preserve any trailing redirect from the
                        # original command.
                        _redirect_match = re.search(
                            r"[12]?>\S+|>>\S+|\|\s*\S+", ks[ks.rfind("'")+1:]
                        )
                        if _redirect_match:
                            _new_ks += ' ' + _redirect_match.group(0)
                        logger.info(
                            "[inline-rewrite] Rewrote python3 -c inline "
                            "script (%d chars) to file-based execution: %s",
                            len(_inline_script), _inline_file,
                        )
                        commands[cmd_idx] = Command(
                            keystrokes=_new_ks + '\n',
                            duration_sec=max(cmd.duration_sec, 3.0),
                        )
                        self._force_action_feedback = (
                            "NOTE: Your `python3 -c '...'` command was "
                            "automatically rewritten to write the script "
                            f"to {_inline_file} and run it from there, "
                            "because inline Python scripts frequently "
                            "corrupt bash quoting and leave the terminal "
                            "stuck in Python input mode. For future "
                            "scripts, write them to a file first: "
                            "`cat << 'PYEOF' > /tmp/script.py` then "
                            "`python3 /tmp/script.py`."
                        )
                        _python3_is_script = False  # already handled
                # Detect shell-installer patterns that spawn interactive
                # prompts reading from /dev/tty (deno, rustup, nvm, etc).
                # These trap the agent in an unrecoverable death spiral
                # because tmux keystrokes go to stdin but the prompt
                # reads from /dev/tty. Code enforcement is needed (same
                # pattern as sleep stripping and Python script enforcement).
                # `yes |` prefix does NOT prevent interactive prompts in
                # shell-installer commands (e.g., `yes | curl ... | sh`). In
                # a Unix pipeline `A | B | C`, A's stdout goes to B's stdin
                # (not C's), so `yes` output goes to `curl` stdin (ignored),
                # never reaching `sh`. Additionally, installers like deno
                # read from /dev/tty, ignoring stdin entirely. Therefore,
                # ALL curl/wget | sh/bash commands are stripped regardless
                # of `yes |` presence.
                _is_shell_installer = bool(re.search(
                    r'(?:curl|wget)\s+.*\|\s*(?:sh|bash)', ks
                ))
                _is_ytdlp = bool(re.search(r'\byt-dlp\b', ks))
                _is_pip_compile = any(p in ks for p in _pip_compile_patterns)
                is_foreground_program = (
                    any(ks.startswith(p) or ks.startswith(f"{p} ") for p in _LARGE_FILE_PROGRAM_PATTERNS)
                    or _python3_is_model
                    or _python3_is_script
                    or _is_shell_installer
                    or _is_ytdlp
                    or _is_pip_compile
                )
                is_db_cli_foreground = (
                    any(p in ks for p in _DB_CLI_PATTERNS)
                    and ("<" in ks or "-f " in ks or "-c " in ks or "|" in ks)
                )
                # Redirect/tee/backgrounded detection was moved above the
                # _python3_is_safe_fg block to avoid UnboundLocalError.
                # Apply the _has_tee_pipe side effect here (setting
                # is_foreground_program = True) since is_foreground_program
                # is only now defined.
                if _has_tee_pipe:
                    is_foreground_program = True
                _is_python_script_stripped = False  # track for Python-specific feedback
                if is_foreground_program and _python3_is_script and not _is_backgrounded:
                    _is_python_script_stripped = True
                # Long-running non-backgrounded CLI commands with file
                # redirects block the terminal for their entire duration —
                # the redirect only controls where output goes, not whether
                # the process blocks. This catches ML training CLIs
                # (fasttext supervised, torchrun, etc.) that aren't matched
                # by the specific patterns above. A command with redirect
                # that isn't backgrounded and has duration >= 30s is
                # expected to block for a significant time.
                _is_long_fg_with_redirect = (
                    has_redirect
                    and not _is_backgrounded
                    and cmd.duration_sec >= 30
                )
                if _is_long_fg_with_redirect:
                    is_foreground_program = True
                # Strip any non-backgrounded foreground program, regardless
                # of whether it has a file redirect. A foreground process
                # blocks the terminal until it exits — has_redirect only
                # controls where output goes, not whether the terminal is
                # blocked. The old condition `not (_is_backgrounded or
                # has_redirect)` incorrectly exempted commands with file
                # redirects, causing 89-turn death spirals from ML training
                # commands like `fasttext supervised ... > /tmp/log.txt
                # 2>&1; tail /tmp/log.txt`.
                if is_foreground_program and not _is_backgrounded:
                    if _is_python_script_stripped:
                        self._force_action_feedback = (
                            "STOP: Your `python3 script.py` command was "
                            "STRIPPED because ALL Python scripts run in "
                            "the foreground block the terminal (they can "
                            "hang on PTY I/O or infinite loops regardless "
                            "of duration). Reducing the duration will NOT "
                            "help. You MUST background the command: "
                            "`nohup python3 script.py > /tmp/output.txt "
                            "2>&1 &` then check results with: "
                            "`tail /tmp/output.txt` (repeat until output "
                            "appears). For build commands like "
                            "`setup.py build_ext`, use: "
                            "`nohup python3 setup.py build_ext --inplace "
                            "> /tmp/build.log 2>&1 & sleep 5 && "
                            "cat /tmp/build.log && ls *.so`"
                        )
                    # Strip the command from the batch to prevent execution,
                    # same pattern as sleep enforcement. _force_action_feedback
                    # alone is insufficient because it only affects the NEXT
                    # model turn — by then the command has already flooded
                    # the tmux buffer and potentially caused a death spiral.
                    elif _is_long_fg_with_redirect:
                        self._force_action_feedback = (
                            "STOP: Your command was STRIPPED from the "
                            "execution batch because it runs in the "
                            "FOREGROUND with output redirected to a file "
                            "but NOT backgrounded. A foreground process "
                            "with `> file.txt` STILL blocks the terminal "
                            "until it exits — the redirect only controls "
                            "where output goes, not whether the process "
                            "blocks. This has caused 89+ turn terminal "
                            "death spirals from ML training commands in "
                            "prior runs. Instead, background the command: "
                            "`nohup command > /tmp/output.txt 2>&1 &` "
                            "then poll with a single long-wait: "
                            "`sleep <estimated_remaining> && tail -20 "
                            "/tmp/output.txt` with duration = estimated "
                            "remaining + 30s buffer."
                        )
                    elif _has_tee_pipe:
                        self._force_action_feedback = (
                            "STOP: Your command using `| tee` was STRIPPED from the "
                            "execution batch. The `tee` command writes to BOTH the file "
                            "AND stdout — the stdout output floods the tmux buffer and causes "
                            "an unrecoverable terminal death spiral. Instead, redirect output "
                            "to a file: `command > /tmp/output.txt 2>&1` then poll with "
                            "`tail /tmp/output.txt`. For long-running commands (training, "
                            "benchmarking, data processing), also run in the background: "
                            "`nohup command > /tmp/output.txt 2>&1 &` then poll with "
                            "`tail /tmp/output.txt` or `ps aux | grep <proc>`. "
                            "This is not optional — `| tee` with long-running commands has "
                            "caused 100+ turn terminal death spirals in prior runs."
                        )
                    elif _is_shell_installer:
                        self._force_action_feedback = (
                            "STOP: Your shell-installer command was STRIPPED from the "
                            "execution batch. Piping a downloaded script into sh/bash "
                            "(curl | sh) spawns interactive prompts that read from "
                            "/dev/tty — NOT from stdin — making the terminal "
                            "permanently stuck. `yes |` prefix does NOT help: in "
                            "`yes | curl | sh`, `yes` feeds `curl` (not `sh`), and "
                            "the prompt ignores stdin anyway. Instead: (1) Download "
                            "the binary directly from GitHub releases (e.g., "
                            "`curl -fsSL https://github.com/OWNER/REPO/releases/"
                            "download/TAG/binary.zip -o /tmp/binary.zip && "
                            "unzip /tmp/binary.zip -d /usr/local/bin/`). "
                            "(2) Use package managers with non-interactive flags "
                            "(`apt-get install -y`, `pip install --no-input`). "
                            "(3) For deno specifically: `curl -fsSL "
                            "https://github.com/denoland/deno/releases/latest/"
                            "download/deno-aarch64-unknown-linux-gnu.zip -o /tmp/deno.zip "
                            "&& unzip /tmp/deno.zip -d /usr/local/bin/`. "
                            "This has caused 150+ turn death spirals in prior runs."
                        )
                    elif _is_ytdlp:
                        self._force_action_feedback = (
                            "STOP: Your yt-dlp command was STRIPPED from the "
                            "execution batch because it was run WITHOUT timeout "
                            "and output redirection. yt-dlp can hang indefinitely "
                            "on blocked/slow network endpoints (YouTube bot "
                            "detection, unreachable proxies, SSL timeouts) and "
                            "floods the tmux buffer with progress output, causing "
                            "an unrecoverable terminal death spiral. This has "
                            "caused 150+ turn death spirals in prior runs. "
                            "Instead, ALWAYS use timeout + output redirect: "
                            "`timeout 120 yt-dlp -o '/app/video.%(ext)s' URL > "
                            "/tmp/ytdlp_output.txt 2>&1` then poll with "
                            "`tail /tmp/ytdlp_output.txt`. Set timeout generously "
                            "(120s for short videos, 300s for long ones). "
                            "If the download fails, check the output file for the "
                            "error instead of retrying without redirect."
                        )
                    elif _python3_is_script and not _python3_is_model:
                        self._force_action_feedback = (
                            "STOP: Your Python script command was STRIPPED from the "
                            "execution batch because it was run WITHOUT redirecting "
                            "output. Python scripts can hang, produce voluminous output, "
                            "or crash with tracebacks — all of which WILL flood the tmux "
                            "buffer and cause an unrecoverable terminal death spiral. "
                            "Instead, redirect output to a file: "
                            "`timeout DURATION python3 script.py > /tmp/output.txt 2>&1` "
                            "then poll with `tail /tmp/output.txt`. This is not optional — "
                            "foreground Python scripts without output redirection have "
                            "caused 100+ turn terminal death spirals in prior runs."
                        )
                    else:
                        self._force_action_feedback = (
                            "STOP: Your program command was STRIPPED from the "
                            "execution batch because it was run WITHOUT redirecting output. "
                            "Programs that load large files (model weights, checkpoints, "
                            "large datasets) WILL flood the terminal buffer and cause an "
                            "unrecoverable death spiral. Instead, redirect output: "
                            "timeout 90 ./a.out args > /tmp/output.txt 2>&1 then poll with "
                            "tail /tmp/output.txt. This is not optional — foreground "
                            "execution of model-loading programs has wasted 40+ turns "
                            "in prior runs."
                        )
                    # Strip the foreground-program command from the batch to
                    # prevent execution, the same pattern used for sleep
                    # commands. Advisory-only _force_action_feedback is
                    # insufficient because: (1) the command executes and
                    # blocks the terminal before the feedback reaches the
                    # model on the next turn; (2) when the terminal becomes
                    # stuck, the empty-turn/force-action detector overrides
                    # _force_action_feedback with recovery messages, causing
                    # the original foreground warning to be silently lost.
                    _fg_strip_indices.append(cmd_idx)
                    break
                # Database CLI commands with query input but no output redirect
                # block the terminal while the query runs and flood it with
                # results. Redirect output to a file and run in background for
                # slow queries.
                if is_db_cli_foreground and not has_redirect:
                    self._force_action_feedback = (
                        "STOP: You are about to run a database query in the foreground "
                        "terminal WITHOUT redirecting output. If the query is slow "
                        "(seconds to minutes), the terminal will block and become "
                        "unresponsive; if the result set is large, the output will "
                        "flood the terminal buffer causing an unrecoverable death spiral. "
                        "Instead, redirect output to a file and run in background: "
                        "nohup timeout 120 sqlite3 /app/db.sql < query.sql > /tmp/output.txt 2>&1 && touch /tmp/done.marker & "
                        "then poll with `tail /tmp/output.txt` or `ls /tmp/done.marker`. "
                        "For quick one-liner queries (e.g., `SELECT COUNT(*)`), a short "
                        "foreground run is fine, but any query sourced from a .sql file "
                        "or piped input is likely non-trivial. This is not optional — "
                        "foreground sqlite3 execution has wasted 30+ turns in prior runs "
                        "due to terminal blocking."
                    )
                    break
                # Build commands without output redirection flood the terminal.
                if not has_redirect:
                    ks_lower = ks.lower()
                    is_build = any(
                        ks_lower == pat or ks_lower.startswith(pat + " ")
                        or f"&& {pat}" in ks_lower or f"; {pat}" in ks_lower
                        for pat in _BUILD_CMD_PATTERNS
                    )
                    if is_build:
                        self._force_action_feedback = (
                            "STOP: You are about to run a build command "
                            "(make/cmake/cargo/ninja) directly in the foreground "
                            "terminal WITHOUT redirecting output. Build commands "
                            "produce voluminous output that WILL flood the tmux "
                            "buffer and cause terminal unresponsiveness, making all "
                            "subsequent commands fail. Instead, redirect output: "
                            "`make > /tmp/build.log 2>&1` then poll with "
                            "`tail -20 /tmp/build.log`. If using nohup, include the "
                            "redirect: `nohup make > /tmp/build.log 2>&1 &`. "
                            "This is not optional — foreground builds have wasted "
                            "50+ turns in prior runs due to terminal death spirals."
                        )
                        break


        # Detect turn-by-turn polling of background processes.
        # If the agent polls the same process/file 3+ consecutive turns
        # with short-duration commands, inject feedback to use a single
        # sleep-based long-wait instead. This prevents 100+ turn polling
        # spirals where the agent checks training progress at 1 turn per
        # percentage point.
        #
        # Three polling patterns are detected:
        # 1. tail/cat against ANY file (not just .log — agents use
        #    .txt, .out, and other extensions)
        # 2. ps aux | grep <proc> repeated process status checks
        # 3. ls -la / du -h repeated file-size checks
        #
        # Previous regex only matched .log files, producing zero
        # matches across 250+ polling commands in train-fasttext.
        _poll_key = None
        for cmd in commands:
            ks = cmd.keystrokes.strip()
            # Pattern 1: tail/cat against any file
            _file_poll_match = re.search(
                r'(?:tail|cat)\s+.*?(/\S+\.\w+)', ks
            )
            # Pattern 2: ps aux | grep <proc> repeated status checks
            _ps_poll_match = re.search(
                r'ps\s+aux.*?grep\s+(\S+)', ks
            )
            # Pattern 3: ls -la / du -h repeated file-size polling
            # (only counts if combined with ps aux or appears without
            # other productive commands)
            _size_poll_match = re.search(
                r'(?:ls\s+-la|du\s+-h)\s+(\S+\.\w+)', ks
            )
            if _file_poll_match and cmd.duration_sec < 60:
                _poll_key = 'file:' + _file_poll_match.group(1)
            elif _ps_poll_match and cmd.duration_sec < 60:
                _poll_key = 'proc:' + _ps_poll_match.group(1)
            elif _size_poll_match and cmd.duration_sec < 60:
                _poll_key = 'file:' + _size_poll_match.group(1)
            else:
                _poll_key = None
            if _poll_key:
                self._polling_log_tracker[_poll_key] = (
                    self._polling_log_tracker.get(_poll_key, 0) + 1
                )
                if self._polling_log_tracker[_poll_key] >= 3:
                    # Determine feedback target based on poll type
                    if _poll_key.startswith('file:'):
                        _target = _poll_key[5:]
                        _check_cmd = f"tail -20 {_target}"
                    else:
                        _target = _poll_key[5:]
                        _check_cmd = f"ps aux | grep {_target}"
                    self._force_action_feedback = (
                        "POLLING SPIRAL DETECTED: You have polled "
                        + _target
                        + " for "
                        + str(self._polling_log_tracker[_poll_key])
                        + " consecutive turns. This wastes 1 turn per "
                        "percentage point of progress. Instead, estimate "
                        "the remaining time and use a SINGLE command: "
                        "`sleep <remaining_seconds> && " + _check_cmd
                        + "` with duration = estimated remaining + 30s "
                        "buffer. This consumes 1 turn instead of dozens."
                    )
                    # Reset after feedback to avoid spamming every turn
                    self._polling_log_tracker[_poll_key] = 0
                    break
            else:
                # Non-polling command resets all trackers
                self._polling_log_tracker.clear()
        # Strip foreground-program commands after the loop (same pattern
        # as sleep-command stripping below).
        if _fg_strip_indices:
            for idx in reversed(_fg_strip_indices):
                stripped = commands.pop(idx)
                logger.info(
                    "[fg-block] Stripped foreground-program command from "
                    "execute_commands batch: %r (duration=%.1fs)",
                    stripped.keystrokes[:80], stripped.duration_sec,
                )

        # Sleep commands in execute_commands batches block the terminal
        # queue, causing a death spiral: the agent sends 'sleep N && tail'
        # to check a background process, but during those N seconds it
        # generates more tool calls that queue up, making the terminal
        # unresponsive. SKILL.md warns against this but the model ignores
        # it. Code-level enforcement prevents the polling death spiral.
        # Detect echo-wait pseudo-sleep commands: the model sends `echo __WAIT__`
        # or similar marker-only echo commands with long durations as a substitute
        # for `sleep`. These produce no diagnostic output and only wait for the
        # duration to expire — functionally identical to sleep but bypassing the
        # literal 'sleep' detection. Observed: 67 echo-wait turns in kv-store-grpc
        # while waiting for pip install, 75% of the total turn budget wasted.
        # Compound commands that combine sleep with a useful output command
        # (tail, cat, ps, ls, head, grep, test, find, wc, diff) should NOT
        # be stripped — they are the recommended pattern for monitoring
        # background processes (sleep N && cmd). Only strip standalone sleep
        # commands and echo-wait pseudo-sleep that produce no useful output.
        _SLEEP_COMPOUND_OUTPUT_RE = re.compile(
            r'(?:tail|cat|ps|ls|head|grep|test|find|wc|diff)\b',
            re.IGNORECASE,
        )
        _echo_wait_re = re.compile(r'^\s*echo\s+__\w+__\s*$', re.IGNORECASE)
        _sleep_cmds = [
            i for i, c in enumerate(commands)
            if (
                ('sleep' in c.keystrokes and not _SLEEP_COMPOUND_OUTPUT_RE.search(c.keystrokes))
                or (_echo_wait_re.match(c.keystrokes) and c.duration_sec >= 10)
            )
        ]
        if _sleep_cmds:
            # Strip sleep-containing commands from the list to prevent them
            # from executing. _force_action_feedback alone is insufficient
            # because it only affects the NEXT model turn — by then the
            # sleep command has already blocked the terminal for N seconds
            # and queued commands have piled up, causing the death spiral.
            # In fix-ocaml-gc runs, `sleep 30 && tail` and `sleep 45 && tail`
            # both executed despite advisory enforcement, wasting 67+ spans
            # in terminal death spirals. Stripping the commands prevents
            # execution; the feedback tells the model what to do instead.
            for idx in reversed(_sleep_cmds):
                stripped = commands.pop(idx)
                logger.info(
                    "[sleep-block] Stripped sleep/echo-wait command from "
                    "execute_commands batch: %r (duration=%.1fs)",
                    stripped.keystrokes[:80], stripped.duration_sec,
                )
            sleep_feedback = (
                "STOP: Your 'sleep' or 'echo __WAIT__' commands were STRIPPED "
                "from the execution batch because they block the terminal "
                "queue and cause a death spiral — sending `echo __WAIT__` "
                "with a long duration is functionally identical to `sleep` "
                "and wastes turns without producing diagnostic output. "
                "When a foreground command (pip install, make, etc.) is "
                "still running, do NOT poll with echo-wait commands. "
                "Instead: (1) if the command is still running in the "
                "foreground, set a generous duration on the NEXT "
                "execute_commands call so the marker-polling mechanism "
                "waits for completion; (2) if you are waiting for a "
                "background process, background the long command with "
                "`nohup cmd > /tmp/output.txt 2>&1 &` then poll with "
                "`tail /tmp/output.txt` or `ps aux | grep <proc>`; (3) for "
                "pip install specifically, if it is slow, background it "
                "with `nohup pip install ... > /tmp/pip.log 2>&1 &` and "
                "check completion with `pip show <package>` or "
                "`tail /tmp/pip.log`. This pattern has wasted 67+ turns "
                "in a single run due to echo-wait polling spirals."
            )
            if self._force_action_feedback:
                self._force_action_feedback += "\n\n" + sleep_feedback
            else:
                self._force_action_feedback = sleep_feedback

        _ENDIAN_CHANGE_PATTERNS = [
            "readUInt32LE", "writeUInt32LE", "readUInt16LE", "writeUInt16LE",
            "readInt32LE", "writeInt32LE", "readInt16LE", "writeInt16LE",
        ]
        _ENDIAN_MODIFY_INDICATORS = [
            "sed -i", "sed -c", ".replace(", "open(", ".write(",
            "content =", "subprocess", "> /app/", ">> /app/",
        ]
        for cmd in commands:
            ks = cmd.keystrokes
            if ks.strip() and any(p in ks for p in _ENDIAN_CHANGE_PATTERNS):
                # Only trigger when the command also looks like a modification
                ks_lower = ks.lower()
                is_modify = (
                    "sed -i" in ks
                    or "sed -c" in ks
                    or any(p in ks for p in _ENDIAN_MODIFY_INDICATORS)
                    or ("python" in ks_lower and "grep" not in ks_lower)
                )
                if is_modify:
                    self._force_action_feedback = (
                        "STOP: You just modified code that uses an endianness convention "
                        "(readUInt32LE/writeUInt32LE or similar). Per the SKILL.md rule "
                        "'Systemic-property fixes must be atomic', you MUST immediately grep "
                        "ALL files for other code that reads or transforms the same "
                        "data using the old convention. Example: if you changed "
                        "readUInt32LE→readUInt32BE for section loading, you MUST also "
                        "find and fix readMemoryByte/writeMemoryByte that consume the "
                        "loaded data using little-endian byte offsets. A partial fix "
                        "is worse than no fix — it produces code that works on one "
                        "path but silently fails on another, and the downstream "
                        "failure is harder to diagnose than the original problem. "
                        "Also: VALIDATE each replacement actually took effect before "
                        "testing — sed and content.replace() silently succeed when the "
                        "pattern doesn't match, producing a file that looks modified "
                        "but isn't."
                    )
                    break

        # Cross-compiler installation reminder: when the agent tries to install
        # a cross-compiler (apt-get install *-gnu-gcc, etc.), it has likely
        # decided to switch toolchains instead of modifying a custom runtime.
        # Inject a strong reminder about the SKILL.md rule.
        _CROSS_COMPILER_INSTALL_PATTERNS = [
            "apt-get install",
            "apt install",
            "apk add",
        ]
        _CROSS_COMPILER_PKG_PATTERNS = [
            "-gnu-gcc",
            "-gcc-",
            "gcc-",
            "crossbuild-essential",
        ]
        for cmd in commands:
            if cmd.keystrokes.strip():
                ks_lower = cmd.keystrokes.lower()
                if any(p in ks_lower for p in _CROSS_COMPILER_INSTALL_PATTERNS):
                    if any(p in ks_lower for p in _CROSS_COMPILER_PKG_PATTERNS):
                        self._force_action_feedback = (
                            "STOP: You are about to install a cross-compiler. "
                            "If the task involves a custom runtime/VM whose source "
                            "code is in the working directory, you MUST modify the "
                            "runtime instead of switching the toolchain. "
                            "Runtime fixes (endianness, missing instructions, "
                            "syscalls) are incremental and testable. "
                            "Switching toolchains produces binaries that crash "
                            "due to missing runtime features (uninitialized GOT, "
                            "wrong entry point, missing __start). "
                            "Use the STANDARD cross-compiler with -static linking "
                            "and modify the VM/runtime to match."
                        )
                        break

        # Pre-execution safety: replace empty keystrokes with polling command.
        # The SKILL.md rule ("never submit empty keystrokes") and the
        # _check_empty_keystrokes NEXT_TURN reminder are insufficient — the
        # agent still submits empty keystrokes and the turn is wasted by the
        # time the reminder fires. Replace empty keystrokes with a diagnostic
        # polling command so the turn produces useful terminal state.
        for i, cmd in enumerate(commands):
            if not cmd.keystrokes.strip():
                logger.info(
                    "[empty-keystrokes-replace] Replacing empty keystrokes with "
                    "diagnostic polling command (duration=%.1fs)",
                    cmd.duration_sec,
                )
                commands[i] = Command(
                    keystrokes=(
                        "echo __POLL__ && "
                        "ps aux --sort=-%cpu 2>/dev/null | head -5 && "
                        "echo __POLL_END__\n"
                    ),
                    duration_sec=cmd.duration_sec,
                )

        # Auto-backup companion files before sqlite3 opens a database.
        # SQLite deletes corrupted/invalid WAL files upon opening a
        # database — this is instant and irreversible. The SKILL.md
        # rule "Preserve files before opening with potentially-destructive
        # tools" is routinely ignored in execution mode. A prompt-level
        # reminder is too late: the destruction happens before the agent
        # can react. The only effective intervention is to inject a
        # backup command BEFORE the sqlite3 command runs.
        import re as _re
        _SQLITE_CMD_RE = _re.compile(
            r'(?:^|&&|;|\|\||\|)\s*sqlite3\s+'
        )
        for i, cmd in enumerate(commands):
            ks = cmd.keystrokes
            if _SQLITE_CMD_RE.search(ks) or _re.match(r'\s*sqlite3\s+', ks):
                # Extract the database path from the sqlite3 command.
                # Common patterns:
                #   sqlite3 /app/main.db 'SELECT ...'
                #   sqlite3 /path/to/file.db '.schema'
                _db_path_match = _re.search(
                    r'sqlite3\s+["\']?(/\S+\.db)["\']?', ks
                )
                if _db_path_match:
                    _db_path = _db_path_match.group(1)
                    # Check for companion files (.db-wal, .db-shm)
                    # that SQLite will delete if invalid.
                    _companions = []
                    for _ext in ('-wal', '-shm'):
                        _cpath = _db_path + _ext
                        _companions.append(_cpath)
                    # Inject a backup command before the sqlite3 command.
                    # Use cp --preserve=timestamps to avoid altering
                    # original metadata.
                    _backup_cmd = (
                        f"cp --preserve=timestamps "
                        f"{_db_path} {' '.join(_companions)} /tmp/ "
                        f"2>/dev/null; echo '[auto-backup] Database and "
                        f"companion files backed up to /tmp/'\n"
                    )
                    # Prepend the backup command as a new Command.
                    commands.insert(i, Command(
                        keystrokes=_backup_cmd,
                        duration_sec=0.5,
                    ))
                    logger.info(
                        "[sqlite3-auto-backup] Injecting backup of %s "
                        "and companions before sqlite3 command",
                        _db_path,
                    )
                    # Only inject once per batch — break after first
                    # sqlite3 command found.
                    break

        # Auto-prefix timeout to python3/python commands that lack it.
        # Without timeout, a long-running Python script blocks the terminal
        # with no recovery. 60 seconds is generous for most scripts; if a
        # script needs more time the agent should explicitly specify a longer
        # timeout. The previous default of 300s caused a 100+ turn terminal
        # death spiral when a PTY-debugging script blocked for the full 5
        # minutes — the agent could not recover, verify, or debug its solution.
        import re as _re
        _PYTHON_DEFAULT_TIMEOUT = 60
        for i, cmd in enumerate(commands):
            ks = cmd.keystrokes
            # Check if python3/python appears as a standalone command word
            # (not inside another word like python3-config). Matches after
            # command separators (&&, ;, ||, |) or at start of string.
            has_python_cmd = bool(_re.search(
                r'(?:^|&&|;|\|\||\|)\s*python3?(?=\s|$)', ks,
            )) or bool(_re.match(r'\s*python3?(?=\s|$)', ks))
            if has_python_cmd and 'timeout' not in ks:
                new_ks = f'timeout {_PYTHON_DEFAULT_TIMEOUT} {ks.lstrip()}'
                logger.info(
                    "[python-auto-timeout] Auto-prefixed: %s -> %s",
                    ks[:80], new_ks[:80],
                )
                commands[i] = Command(
                    keystrokes=new_ks,
                    duration_sec=cmd.duration_sec,
                )

        # Output-driven reminders (feedback flows to the next turn).
        # When format-collapse recovery produced commands, suppress the
        # "no_tool_calls" reminder by providing a non-empty tool_calls
        # alias. Without this, the model receives contradictory
        # "ALERT: No executable command" messages even though commands
        # were recovered and executed — observed in 19 consecutive
        # format-collapsed turns in torch-pipeline-parallelism runs,
        # perpetuating the FC loop.
        _fc_recovered = (
            not tool_response.tool_calls
            and bool(commands)
            and not is_task_complete
        )
        reminders = _run_reminder_checkers(
            content=tool_response.content,
            tool_calls=(
                [{"function": {"name": "execute_commands"}}]
                if _fc_recovered
                else tool_response.tool_calls
            ),
            reasoning_content=tool_response.reasoning_content,
            turn_index=len(self._trajectory_steps),
        )
        for reminder in reminders:
            self._emit_reminder(reminder)

        # Escalate repeated empty turns (no commands / no task_complete): the task
        # is stalled and will time out. Emitted as a reminder so it's visible too.
        # Also counts commands with only empty/whitespace keystrokes as inactive,
        # since they produce no terminal output and are functionally equivalent
        # to a no-op.
        has_effective_commands = bool(commands) and any(
            c.keystrokes.strip() for c in commands
        )
        # When format-collapse recovery extracted commands, suppress the
        # empty-turn counter even if the commands came from text recovery
        # rather than proper tool_calls. Without this, FC-recovered turns
        # produce false "ALERT: No executable command" messages that
        # contradict the visible terminal output, confusing the model
        # into thinking the terminal is unresponsive (observed: 89 false
        # ALERTs across 118 FC turns in polyglot-rust-c).
        if _fc_recovered:
            has_effective_commands = True
        if not has_effective_commands and not is_task_complete:
            self._consecutive_empty_turns += 1
            if self._consecutive_empty_turns >= 2:
                escalation_text = (
                    f"ALERT: No executable command has been produced for "
                    f"{self._consecutive_empty_turns} consecutive turns — the task is "
                    f"stalled and will time out if this continues. Stop describing "
                    f"what you would do; emit the single next command the task needs "
                    f"as a structured execute_commands tool call now."
                )
                self._emit_reminder(Reminder(
                    code="repeated_empty_commands",
                    message=(
                        f"{self._consecutive_empty_turns} consecutive turns "
                        f"produced no executable command — task stalled."
                    ),
                    span_message=escalation_text,
                    feedback_text=escalation_text,
                    delivery=_REMINDER_DELIVERY_NEXT_TURN,
                ))
        else:
            self._consecutive_empty_turns = 0

        # Force-inject fresh terminal state after prolonged inactivity.
        # When the model enters a degenerate reasoning-only loop (producing
        # reasoning_content but no text or tool_calls), text reminders are
        # ineffective — the model reads the reminder, thinks, and produces
        # only reasoning again. Injecting concrete terminal output gives the
        # model something external to react to, breaking the self-reinforcing
        # loop. Threshold chosen to allow a few empty turns (the reminder
        # system handles 2-4) before escalating to forced interaction.
        _FORCE_ACTION_THRESHOLD = 3
        _HARD_RESET_FORCE_COUNT = 3  # hard reset after this many force-action interventions
        _REASONING_ONLY_CIRCUIT_BREAKER = 3  # after this many hard-resets, inject a command directly. Previous value of 10 required ~90 empty turns before firing, which is far too slow — the run budget is typically exhausted before the breaker activates.
        if (
            self._consecutive_empty_turns >= _FORCE_ACTION_THRESHOLD
            and session is not None
        ):
            # Track total hard-resets across the entire run
            if not hasattr(self, '_total_hard_resets'):
                self._total_hard_resets = 0
            # Execute a simple diagnostic command to get fresh terminal state.
            # Guard: before injecting a force-action command, capture the
            # current pane to check if previous force-action commands are
            # still queued (visible as unexecuted echoes). If they are,
            # skip injection — adding more commands only increases
            # congestion. Instead, send Ctrl-C to try to break the
            # stuck state, then capture again.
            try:
                pre_capture = await self._with_block_timeout(session.capture_pane())
                _force_cmd_pending = (
                    "__FORCE_ACTION__" in pre_capture
                    and "$" not in pre_capture.split("\n")[-1]
                )
                if _force_cmd_pending:
                    # Previous force-action commands are still queued.
                    # Try Ctrl-C to break the stuck state instead of
                    # adding more commands.
                    await self._with_block_timeout(
                        session.send_keys("C-c", block=False, min_timeout_sec=0.0)
                    )
                    await asyncio.sleep(2.0)
                    forced_output = await self._with_block_timeout(session.capture_pane())
                else:
                    await self._with_block_timeout(
                        session.send_keys("echo __FORCE_ACTION__ && pwd && ls /app 2>/dev/null || pwd\n", block=False, min_timeout_sec=0.0)
                    )
                    await asyncio.sleep(1.0)
                    forced_output = await self._with_block_timeout(session.capture_pane())
            except Exception:
                forced_output = "(failed to capture forced terminal state)"
            # Emit as a NEXT_TURN reminder so its feedback flows into the
            # next observation. Also store for direct prompt injection.
            force_feedback = (
                f"SYSTEM: You have produced NO executable commands for "
                f"{self._consecutive_empty_turns} consecutive turns. "
                f"This is a critical failure — the task will time out. "
                f"Your current terminal state:\n{forced_output}\n\n"
                f"You MUST call execute_commands NOW with at least one command. "
                f"Even a simple `ls /app` is better than nothing. "
                f"Do NOT just think about what to do — actually do it.\n\n"
                f"CRITICAL: You must produce a FUNCTION CALL to execute_commands, "
                f"not text that describes commands. Your response must contain a "
                f"tool call with function name 'execute_commands' and arguments "
                f"including a 'commands' array with objects containing 'keystrokes' "
                f"and 'duration'. Do NOT write commands as plain text or JSON "
                f"inside your message content — use the tool-calling interface."
            )
            forced_reminder = Reminder(
                code="force_terminal_refresh",
                message=(
                    f"Forced terminal refresh after {self._consecutive_empty_turns} "
                    f"consecutive inactive turns."
                ),
                span_message=(
                    f"SYSTEM: The agent has been inactive for "
                    f"{self._consecutive_empty_turns} consecutive turns. "
                    f"Forcing a terminal state refresh to break the loop. "
                    f"Current terminal output:\n{forced_output}"
                ),
                feedback_text=force_feedback,
                delivery=_REMINDER_DELIVERY_NEXT_TURN,
            )
            self._emit_reminder(forced_reminder)
            # Track how many times force-action has intervened.
            self._force_action_count = getattr(self, '_force_action_count', 0) + 1
            # HARD RESET: When force-action has fired multiple times without
            # breaking the loop, inject a direct user message into the chat
            # history that is impossible to ignore — it becomes a permanent
            # part of the conversation, not just observation text the model
            # can reason about. This dislodges deep reasoning-only loops
            # where the model treats all feedback as context to think about
            # rather than an imperative to act.
            if self._force_action_count >= _HARD_RESET_FORCE_COUNT:
                hard_reset_message = (
                    f"STOP REASONING. You have produced only thoughts and zero "
                    f"executable commands for many consecutive turns. "
                    f"The task is about to time out. YOU MUST "
                    f"call execute_commands NOW with a single simple command. "
                    f"Do not think about what to do. Do not plan. Just call "
                    f"execute_commands with analysis='breaking loop', "
                    f"plan='run a command', commands=[{{'keystrokes': 'echo hello\\n', "
                    f"'duration': 1.0}}]. This is not optional — the task "
                    f"WILL fail if you do not produce a tool call this turn."
                )
                # Inject directly into chat messages so it persists across
                # retries and cannot be dismissed as observation noise.
                chat._messages.append({"role": "user", "content": hard_reset_message})
                chat._messages.append({"role": "assistant", "content": "Acknowledged. I will call execute_commands now."})
                chat.reset_response_chain()
                # Track cumulative hard-resets across the entire run.
                # If the reasoning-only loop persists through multiple
                # hard-resets, inject a command directly to break it.
                self._total_hard_resets = getattr(self, '_total_hard_resets', 0) + 1
                if self._total_hard_resets >= _REASONING_ONLY_CIRCUIT_BREAKER:
                    # Circuit breaker: force-execute a diagnostic command
                    # directly, bypassing the model entirely. This produces
                    # genuinely new terminal state that the model cannot
                    # ignore by reasoning about it.
                    try:
                        await self._with_block_timeout(
                            session.send_keys("echo __CIRCUIT_BREAKER__ && ls -la /app/ && echo __CIRCUIT_BREAKER_END__\n", block=False, min_timeout_sec=0.0)
                        )
                        await asyncio.sleep(2.0)
                        cb_output = await self._with_block_timeout(session.capture_pane())
                    except Exception:
                        cb_output = "(circuit breaker command failed)"
                    self._force_action_feedback = (
                        f"SYSTEM CIRCUIT BREAKER: After {self._total_hard_resets} consecutive "
                        f"hard-reset attempts, the system is forcing execution to break "
                        f"a persistent reasoning-only loop. Terminal state after forced command:\n"
                        f"{cb_output}\n\n"
                        f"You MUST respond with a tool call to execute_commands. "
                        f"Do NOT reason about this message — call execute_commands "
                        f"with analysis='circuit breaker response' and ONE command "
                        f"like 'ls /app/' or 'pwd'."
                    )
                # Reset per-cycle counters but preserve total_hard_resets.
                self._consecutive_empty_turns = 0
                self._force_action_count = 0
                if not self._force_action_feedback:
                    self._force_action_feedback = hard_reset_message
            else:
                # Partially reset counter so we can escalate again if the loop
                # continues, but don't fully reset — we want to track that this
                # intervention was needed.
                self._consecutive_empty_turns = 2
                # Store feedback for direct prompt injection in _run_agent_loop.
                # The NEXT_TURN reminder mechanism only includes feedback when
                # the observation contains "WARNINGS:", so we use a dedicated
                # injection path.
                self._force_action_feedback = force_feedback

        # Reasoning suppression is now handled continuously in
        # _call_llm_with_tools: reasoning_effort is suppressed whenever
        # _consecutive_reasoning_only > 0, and _force_action_feedback is
        # injected after 3+ consecutive turns. No one-shot flag needed.

        # Drain NEXT_TURN reminders into the feedback for the next turn.
        if self._pending_next_turn_reminders:
            next_text = "\n\n".join(
                r.feedback_text for r in self._pending_next_turn_reminders
                if r.feedback_text
            )
            if next_text:
                feedback = f"{next_text}\n\n{feedback}" if feedback else next_text
            self._pending_next_turn_reminders = []

        llm_response = LLMResponse(
            content=tool_response.content or "",
            reasoning_content=tool_response.reasoning_content,
            usage=tool_response.usage,
        )
        return commands, is_task_complete, feedback, analysis, plan, llm_response, image_read

    def _record_chat_turn(self, chat: Chat, prompt: str, tool_response: ToolCallResponse) -> None:
        """Append this turn's user/assistant/tool messages and update metrics."""
        assistant_message: dict[str, Any] = {
            "role": "assistant",
            "content": tool_response.content,
        }
        if tool_response.reasoning_content:
            assistant_message["reasoning_content"] = tool_response.reasoning_content
        if tool_response.tool_calls:
            assistant_message["tool_calls"] = tool_response.tool_calls
        chat._messages.append({"role": "user", "content": prompt})
        chat._messages.append(assistant_message)
        if tool_response.tool_calls:
            for tc in tool_response.tool_calls:
                chat._messages.append({
                    "role": "tool",
                    "tool_call_id": tc.get("id", ""),
                    "content": "executed",
                })
            chat.reset_response_chain()
        if tool_response.usage:
            chat._cumulative_input_tokens += tool_response.usage.prompt_tokens
            chat._cumulative_output_tokens += tool_response.usage.completion_tokens
            chat._cumulative_cache_tokens += tool_response.usage.cache_tokens
            chat._cumulative_cost += tool_response.usage.cost_usd

    async def _summarize_or_fallback(
        self, chat: Chat, original_instruction: str, session: TmuxSession
    ) -> str:
        summary_prompt = None
        try:
            summary_prompt, subagent_refs = await self._with_block_timeout(
                self._summarize(chat, original_instruction, session)
            )
            self._pending_subagent_refs = subagent_refs
            self._pending_handoff_prompt = summary_prompt
        except Exception as e:
            self.logger.debug(f"SUMMARIZATION failed: {e}")
        if summary_prompt is None:
            current_screen = await self._with_block_timeout(session.capture_pane(capture_entire=False))
            limited = current_screen[-1000:] if current_screen else ""
            summary_prompt = f"{original_instruction}\n\nCurrent state: {limited}"
        return summary_prompt

    # -- agent loop (override Terminus2) -----------------------------------

    async def _run_agent_loop(
        self,
        initial_prompt: str,
        chat: Chat,
        original_instruction: str = "",
    ) -> None:
        if self._context is None or self._session is None:
            raise RuntimeError("Agent context/session not set")

        # Auto-configure shell environment to prevent pagers from trapping the
        # agent. Pagers (less, more) open interactive interfaces the agent
        # cannot navigate, wasting many turns to escape (9+ in observed runs).
        # Setting PAGER=cat globally prevents any command (help(), man,
        # git log/diff, pydoc) from opening a pager. GIT_PAGER and MANPAGER
        # cover git and man respectively. Non-critical: failure is logged but
        # does not block the run (the skill rules still advise PAGER=cat).
        try:
            await self._with_block_timeout(
                self._session.send_keys(
                    "export PAGER=cat GIT_PAGER=cat MANPAGER=cat\n"
                    "set +H\n",
                    block=False,
                    min_timeout_sec=0.0,
                )
            )
            await asyncio.sleep(0.3)
        except Exception:
            logger.info("[pager-setup] Failed to set PAGER=cat, continuing without it.")

        prompt = initial_prompt
        self._context.n_input_tokens = 0
        self._context.n_output_tokens = 0
        self._context.n_cache_tokens = 0
        self._context.cost_usd = None

        for episode in range(self._max_episodes):
            self._n_episodes = episode + 1
            if not await self._with_block_timeout(self._session.is_session_alive()):
                return

            if original_instruction and self._enable_summarize:
                proactive = await self._with_block_timeout(
                    self._check_proactive_summarization(chat, original_instruction, self._session)
                )
                if proactive:
                    prompt, subagent_refs = proactive
                    self._pending_subagent_refs = subagent_refs
                    self._pending_handoff_prompt = prompt

            # Time-budget reminder (THIS_TURN): emit its span, then prepend the
            # feedback to this turn's prompt so it leads the model's attention.
            time_reminder = self._time_reminder_due()
            if time_reminder is not None:
                self._emit_reminder(time_reminder)
                prompt = (
                    f"{time_reminder.feedback_text}\n\n{prompt}"
                    if prompt else time_reminder.feedback_text
                )

            tokens_before_input = chat.total_input_tokens
            tokens_before_output = chat.total_output_tokens
            tokens_before_cache = chat.total_cache_tokens
            cost_before = chat.total_cost

            (
                commands, is_task_complete, feedback, analysis, plan,
                llm_response, image_read,
            ) = await self._handle_llm_interaction(
                chat, prompt, original_instruction, self._session
            )

            if self._pending_subagent_refs:
                self._trajectory_steps.append(
                    Step(
                        step_id=len(self._trajectory_steps) + 1,
                        timestamp=datetime.now(timezone.utc).isoformat(),
                        source="system",
                        message="Performed context summarization and handoff to continue task.",
                        observation=Observation(results=[ObservationResult(
                            subagent_trajectory_ref=self._pending_subagent_refs
                        )]),
                        extra={"context_management": {"type": "compaction", "boundary": "replace"}},
                    )
                )
                self._pending_subagent_refs = None

            if self._pending_handoff_prompt:
                if self._linear_history:
                    self._split_trajectory_on_summarization(self._pending_handoff_prompt)
                else:
                    self._trajectory_steps.append(Step(
                        step_id=len(self._trajectory_steps) + 1,
                        timestamp=datetime.now(timezone.utc).isoformat(),
                        source="user",
                        message=self._pending_handoff_prompt,
                    ))
                self._pending_handoff_prompt = None

            if self._save_raw_content_in_trajectory:
                message_content = llm_response.content
            else:
                parts = []
                if analysis:
                    parts.append(f"Analysis: {analysis}")
                if plan:
                    parts.append(f"Plan: {plan}")
                message_content = "\n".join(parts) if parts else ""

            self._context.n_input_tokens = chat.total_input_tokens
            self._context.n_output_tokens = chat.total_output_tokens
            self._context.n_cache_tokens = chat.total_cache_tokens
            self._context.cost_usd = chat.total_cost if chat.total_cost > 0 else None

            if feedback and "ERROR:" in feedback:
                prompt = (
                    f"Previous response had parsing errors:\n{feedback}\n\n"
                    f"Please fix these issues and provide a proper "
                    f"{self._get_error_response_type()}."
                )
                self._append_step(
                    episode, chat, llm_response, message_content, prompt,
                    tokens_before_input, tokens_before_output, tokens_before_cache, cost_before,
                    tool_calls=None,
                )
                self._dump_trajectory()
                continue

            if image_read is not None:
                observation = await self._execute_image_read(image_read, chat, original_instruction)
                was_pending = self._pending_completion
                if is_task_complete:
                    if self._pending_completion:
                        obs = observation
                    else:
                        self._pending_completion = True
                        obs = self._get_completion_confirmation_message(observation)
                else:
                    self._pending_completion = False
                    # Same fix as the execute_commands branch: always show
                    # observation first, append feedback after. See the
                    # execute_commands branch comment for rationale.
                    obs = (
                        f"{observation}\n\n[System feedback]: {feedback}"
                        if feedback
                        else observation
                    )

                tool_calls_list: list[ToolCall] = []
                if not self._save_raw_content_in_trajectory:
                    tool_calls_list.append(ToolCall(
                        tool_call_id=f"call_{episode}_image_read",
                        function_name="image_read",
                        arguments={"file_path": image_read.file_path,
                                   "image_read_instruction": image_read.image_read_instruction},
                    ))
                    if is_task_complete:
                        tool_calls_list.append(ToolCall(
                            tool_call_id=f"call_{episode}_task_complete",
                            function_name="task_complete", arguments={},
                        ))
                self._append_step(
                    episode, chat, llm_response, message_content, obs,
                    tokens_before_input, tokens_before_output, tokens_before_cache, cost_before,
                    tool_calls=tool_calls_list or None,
                )
                self._dump_trajectory()
                if is_task_complete and was_pending:
                    return
                prompt = obs
            else:
                timeout_occurred, terminal_output = await self._with_block_timeout(
                    self._execute_commands(commands, self._session)
                )
                was_pending = self._pending_completion
                if is_task_complete:
                    if self._pending_completion:
                        observation = terminal_output
                    else:
                        self._pending_completion = True
                        observation = self._get_completion_confirmation_message(terminal_output)
                else:
                    # Preserve pending_completion when the agent runs
                    # execute_commands for verification between task_complete
                    # cycles. SKILL.md rule 11 and the confirmation message
                    # both state that execute_commands should NOT reset the
                    # confirmation state — only non-execute_commands tools
                    # (e.g., image_read) should reset it. Without this, an
                    # agent that correctly runs verification commands after
                    # the first task_complete must go through an extra
                    # confirmation cycle, wasting 4+ turns per cycle.
                    if not (commands and not image_read):
                        self._pending_completion = False
                    # Always show terminal output FIRST so the model can see
                    # command results. Append feedback/warnings AFTER so they
                    # don't bury the output. This fixes format-collapse (FC)
                    # feedback being silently dropped: FC uses "WARNING:" but
                    # the old check required "WARNINGS:" (plural), so the
                    # model never saw the FC correction message.
                    limited_output = self._limit_output_length(terminal_output)
                    observation = (
                        f"{limited_output}\n\n[System feedback]: {feedback}"
                        if feedback
                        else limited_output
                    )

                # Observation-driven check: compiler warnings in terminal output.
                compiler_warning_reminder = _check_compiler_warnings_in_output(
                    observation
                )
                if compiler_warning_reminder is not None:
                    self._emit_reminder(compiler_warning_reminder)

                # Observation-driven check: git forensics commands. The SKILL.md
                # reference trigger is prompt-level and routinely skipped. A
                # code-level reminder fires once when the agent first runs a git
                # forensics command (reflog, fsck, log --all, cherry-pick, reset,
                # rebase, stash), nudging it to load the reference for advanced
                # procedures (fsck --lost-found, dangling object inspection,
                # cleanup sequences, verification checklists) that go beyond
                # basic git log/reflog knowledge.
                if not self._git_forensics_reminder_fired and commands:
                    _GIT_FORENSICS_PATTERNS = (
                        'git reflog', 'git fsck', 'git log --all',
                        'git cherry-pick', 'git reset', 'git rebase',
                        'git stash', 'git bisect',
                    )
                    for cmd in commands:
                        if any(p in cmd.keystrokes for p in _GIT_FORENSICS_PATTERNS):
                            self._git_forensics_reminder_fired = True
                            self._emit_reminder(Reminder(
                                code='git_forensics_reference',
                                message=(
                                    f'Agent ran a git forensics command '
                                    f'({cmd.keystrokes[:60]}). Reminding '
                                    f'to load the git-forensics reference.'
                                ),
                                span_message=(
                                    'REMINDER: git forensics command detected. '
                                    'The git-forensics reference covers '
                                    'procedures beyond basic git: '
                                    'git fsck --lost-found for commits '
                                    'not in reflog, dangling object '
                                    'inspection, proper cleanup sequences, '
                                    'and a verification checklist. Run: '
                                    'cat /skill/references/git-forensics.md'
                                ),
                                feedback_text=(
                                    'TIP: You are working with git history. '
                                    'The reference at '
                                    '/skill/references/git-forensics.md '
                                    'provides critical procedures beyond '
                                    'basic git commands: git fsck '
                                    '--lost-found for commits not in the '
                                    'reflog, dangling object inspection '
                                    'with git cat-file, proper cleanup '
                                    'sequences (reflog expire + gc '
                                    '--prune=now), and a verification '
                                    'checklist. Run `cat /skill/references/'
                                    'git-forensics.md` to load it before '
                                    'proceeding with git history operations.'
                                ),
                                delivery=_REMINDER_DELIVERY_NEXT_TURN,
                            ))
                            break

                tool_calls_list = []
                if not self._save_raw_content_in_trajectory:
                    if commands:
                        for i, cmd in enumerate(commands):
                            tool_calls_list.append(ToolCall(
                                tool_call_id=f"call_{episode}_{i + 1}",
                                function_name="execute_commands",
                                arguments={"keystrokes": cmd.keystrokes, "duration": cmd.duration_sec},
                            ))
                    if is_task_complete:
                        tool_calls_list.append(ToolCall(
                            tool_call_id=f"call_{episode}_task_complete",
                            function_name="task_complete", arguments={},
                        ))
                self._append_step(
                    episode, chat, llm_response, message_content, observation,
                    tokens_before_input, tokens_before_output, tokens_before_cache, cost_before,
                    tool_calls=tool_calls_list or None,
                )
                self._dump_trajectory()
                if is_task_complete and was_pending:
                    return
                prompt = observation

            # Inject force-action feedback if the degenerate-loop detector fired.
            # This ensures the corrective message (with fresh terminal state)
            # leads the next model turn's prompt, regardless of whether the
            # normal feedback-in-observation path included it.
            if self._force_action_feedback:
                prompt = f"{self._force_action_feedback}\n\n{prompt}"
                self._force_action_feedback = None

    def _append_step(
        self,
        episode: int,
        chat: Chat,
        llm_response: LLMResponse,
        message_content: str,
        observation_text: str,
        tbi: int, tbo: int, tbc: int, cost_before: float,
        tool_calls: list[ToolCall] | None,
    ) -> None:
        cache_tokens_used = chat.total_cache_tokens - tbc
        step_cost = chat.total_cost - cost_before

        # Model-turn Step. Reminders are their own source="user" Steps, not folded in here.
        self._trajectory_steps.append(
            Step(
                step_id=len(self._trajectory_steps) + 1,
                timestamp=datetime.now(timezone.utc).isoformat(),
                source="agent",
                model_name=self._model_name,
                message=message_content,
                reasoning_content=llm_response.reasoning_content,
                tool_calls=tool_calls,
                observation=Observation(results=[ObservationResult(content=observation_text)]),
                metrics=Metrics(
                    prompt_tokens=chat.total_input_tokens - tbi,
                    completion_tokens=chat.total_output_tokens - tbo,
                    cached_tokens=cache_tokens_used if cache_tokens_used > 0 else None,
                    cost_usd=step_cost if step_cost > 0 else None,
                    prompt_token_ids=llm_response.prompt_token_ids,
                    completion_token_ids=llm_response.completion_token_ids,
                    logprobs=llm_response.logprobs,
                ),
            )
        )
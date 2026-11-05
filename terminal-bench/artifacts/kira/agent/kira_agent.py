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


# Registry of output-driven checkers. Time-driven reminders are produced in the
# loop (_time_reminder_due), not here.
REMINDER_CHECKERS: list[ReminderChecker] = [
    _check_no_tool_calls,
    # _check_repeated_empty_commands,
    # _check_ignoring_terminal_output,
]


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
_COMMANDS_DESC = "The commands array can be empty if you want to wait without taking action."
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
    "On immediate tasks (e.g., cd, ls, echo, cat) set a duration of 0.1 seconds. "
    "On commands (e.g., gcc, find, rustc) set a duration of 1.0 seconds. "
    "On slow commands (e.g., make, python3 [long running script], wget [file]) set an appropriate duration. "
    "It is better to set a smaller duration than a longer duration. "
    "Never wait longer than 60 seconds; prefer to poll to see intermediate result status."
)
_TASK_COMPLETE_DESC = "Call this when the task is complete."
_IMAGE_READ_DESC = (
    "Read and analyze an image file. Use this ONLY for image files that you need to visually analyze. "
    "Do NOT use this for text files — use shell commands (cat, head, etc.) instead. "
    "The image will be sent to the model for visual analysis "
    "and you will receive a text description in the next turn."
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
        # Reminder system state. _time_start_monotonic is captured in run();
        # _time_reminder_fired tracks which time thresholds have already fired
        # so each fires at most once. _pending_next_turn_reminders holds
        # output-driven reminders whose feedback is delivered on the next turn
        # (crewed by _handle_llm_interaction, cleared by _append_step).
        self._time_start_monotonic: float | None = None
        self._time_reminder_fired: set[int] = set()
        self._pending_next_turn_reminders: list[Reminder] = []

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
        """Preload SKILL.md + inject the skill directory location and a file manifest.

        The skill dir is bind-mounted read-only into the container at ``/skill``
        (see job-config-kira.yaml). This method reads directly from the host spec
        dir (no container exec, no find) and emits two sections appended to every
        turn's prompt:

          ## Skill — the full SKILL.md body (frontmatter stripped), so the agent
                    has the skill rules in context from the very first turn.
          ## Skill Directory — the container path of the skill root, EVERY file
                    under it with its absolute container path, and how to access
                    each (cat for references, execute_commands for scripts).
                    Emitted unconditionally when a skill is present, so the agent
                    always knows *where* the skill lives and *what* files exist —
                    not just when references/scripts happen to be non-empty.

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

    def _get_error_response_type(self) -> str:
        return "response with valid tool calls"

    def _get_completion_confirmation_message(self, terminal_output: str) -> str:
        instruction = getattr(self, "_original_instruction", "N/A")
        return (
            f"Original task:\n{instruction}\n\n"
            f"Current terminal state:\n{terminal_output}\n\n"
            "Are you sure you want to mark the task as complete?\n\n"
            "[!] Checklist\n"
            "- Does your solution meet the requirements in the original task above? [TODO/DONE]\n"
            "- Does your solution account for potential changes in numeric values, array sizes, file contents, or configuration parameters? [TODO/DONE]\n"
            "- Have you verified your solution from the all perspectives of a test engineer, a QA engineer, and the user who requested this task?\n"
            "  - test engineer [TODO/DONE]\n"
            "  - QA engineer [TODO/DONE]\n"
            "  - user who requested this task [TODO/DONE]\n\n"
            "After this point, solution grading will begin and no further edits will be possible. "
            "If everything looks good, call task_complete tool again."
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

            await session.send_keys(command.keystrokes, block=False, min_timeout_sec=0.0)
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

            if function_name == "execute_commands":
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

        if self._reasoning_effort:
            completion_kwargs["reasoning_effort"] = self._reasoning_effort
            completion_kwargs["temperature"] = 1

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

        commands, is_task_complete, feedback, analysis, plan, image_read = (
            self._parse_tool_calls(tool_response.tool_calls)
        )

        # Output-driven reminders (feedback flows to the next turn).
        reminders = _run_reminder_checkers(
            content=tool_response.content,
            tool_calls=tool_response.tool_calls,
            reasoning_content=tool_response.reasoning_content,
            turn_index=len(self._trajectory_steps),
        )
        for reminder in reminders:
            self._emit_reminder(reminder)

        # Escalate repeated empty turns (no commands / no task_complete): the task
        # is stalled and will time out. Emitted as a reminder so it's visible too.
        if not commands and not is_task_complete:
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
                    obs = (
                        f"Previous response had warnings:\n{feedback}\n\n{observation}"
                        if feedback and "WARNINGS:" in feedback else observation
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
                    self._pending_completion = False
                    observation = (
                        f"Previous response had warnings:\n{feedback}\n\n{self._limit_output_length(terminal_output)}"
                        if feedback and "WARNINGS:" in feedback
                        else self._limit_output_length(terminal_output)
                    )

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
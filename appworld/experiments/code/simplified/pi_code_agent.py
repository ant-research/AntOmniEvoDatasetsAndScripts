import json
import os
import re
import subprocess
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

from jinja2 import Template

from appworld import AppWorld
from appworld.common.io import dump_yaml, read_file
from appworld.environment import SAID_AVAILABLE_IMPORTS
from appworld_agents.code.common.usage_tracker import Cost, CostPerToken, Tokens, Usage
from appworld_agents.code.simplified.agent import Agent, ExecutionIO, Status


_FULL_CODE_RE = re.compile(r"```python\n(.*?)```", re.DOTALL)
_PARTIAL_CODE_RE = re.compile(r".*```python\n(.*)", re.DOTALL)


# ------------------------------------------------------------------
# Shared prompt-building helpers (used by both template and skill modes)
# ------------------------------------------------------------------

_AGENT_CONSTRAINT = """\
## Environment

You are an AI Assistant whose job is to complete day-to-day tasks fully
autonomously by writing Python code in the AppWorld environment.

- You interact with apps (e.g., spotify, venmo, gmail) through their Python
  APIs using the `apis` object, which is pre-loaded in the execution
  environment. Do NOT create it yourself.
- Code is executed in a sandboxed Python REPL. You write code blocks
  (```python ... ```), the environment executes them and returns output.
- You can use variables in your code in subsequent code blocks.

## Task Completion

You MUST call `apis.supervisor.complete_task()` after completing the task.
- If an answer is needed (e.g., "How many songs?"), pass it as the `answer`
  argument. Keep answers minimal — return only the entity or number, not
  full sentences.
- If no answer is needed (e.g., "Start my music player"), omit the `answer`
  argument or set it to None.
- If the task is impossible, call it with `status="fail"`.

## Key Rules

- Act fully on your own. Never ask for confirmation or clarification.
- Never invent or guess values. Look up credentials via
  `apis.supervisor.show_account_passwords()` and personal info via
  `apis.supervisor.show_profile()`.
- Always obtain the current date/time from `datetime.now()` or
  `apis.phone.get_current_date_and_time()`, never from internal knowledge.
- Paginated APIs: always loop through all pages using `page_index`.
- References to "file system" mean the file_system app, not the OS.
- To interact with apps, only use the provided app APIs, not Python packages.
"""


def load_skill(skill_dir: str | Path) -> str:
    """Load skill content from SKILL.md, stripping YAML frontmatter.

    Args:
        skill_dir: Path to the directory containing SKILL.md.

    Returns:
        The skill content with frontmatter removed, or empty string if
        SKILL.md does not exist.
    """
    skill_file = Path(skill_dir) / "SKILL.md"
    if not skill_file.exists():
        return ""
    content = skill_file.read_text(encoding="utf-8")
    if content.startswith("---"):
        end = content.find("---", 3)
        if end != -1:
            content = content[end + 3:].strip()
    return content


def build_system_prompt(skill_content: str = "") -> str:
    """Build the system prompt from AGENT_CONSTRAINT and optional skill content.

    Args:
        skill_content: The SKILL.md content (frontmatter already stripped).
            If empty, only the constraint is used.

    Returns:
        The complete system prompt string.
    """
    prompt = _AGENT_CONSTRAINT
    if skill_content:
        prompt += "\n\n## Skill\n\n" + skill_content
    return prompt


def build_task_context(
    instruction: str,
    app_descriptions: dict[str, str],
    api_doc_string: str,
    supervisor: Any,
    available_imports: str,
) -> str:
    """Build the per-task user prompt with API docs and instruction.

    Args:
        instruction: The task instruction text.
        app_descriptions: Dict mapping app_name -> description.
        api_doc_string: YAML-formatted compressed API documentation.
        supervisor: The task's supervisor object (with first_name, email, etc.).
        available_imports: The SAID_AVAILABLE_IMPORTS string.

    Returns:
        The user prompt string.
    """
    supervisor_info = ""
    if hasattr(supervisor, "first_name"):
        supervisor_info = (
            f"My name is: {supervisor.first_name} {supervisor.last_name}. "
            f"My personal email is {supervisor.email} "
            f"and phone number is {supervisor.phone_number}."
        )

    app_desc_json = json.dumps(
        [{"name": k, "description": v} for k, v in app_descriptions.items()],
        indent=1,
    )

    parts = [
        "## App Descriptions\n",
        app_desc_json,
        "\n\n## API Documentation\n",
        "```yaml",
        api_doc_string,
        "```\n",
        f"\n## Available Imports\n```python\n{available_imports}\n```\n",
    ]

    if supervisor_info:
        parts.append(f"\n{supervisor_info}\n")

    parts.append(f"\nTask: {instruction}")

    return "\n".join(parts)


# ------------------------------------------------------------------
# SimplifiedPiCodeAgent (template-based or skill-based)
# ------------------------------------------------------------------


@Agent.register("simplified_pi_code_agent")
class SimplifiedPiCodeAgent(Agent):  # type: ignore[misc]  # pyright: ignore[reportCallIssue, reportIncompatibleMethodOverride]
    """An AppWorld agent that drives the ``pi`` CLI as a subprocess.

    Supports two prompt-building modes:

    **Template mode** (default): Pass ``prompt_file_path`` to load a Jinja2
    template that renders the full conversation prompt from task variables.

    **Skill mode**: Pass ``skill_dir`` instead to build the prompt from a
    fixed AGENT_CONSTRAINT plus an optimized SKILL.md.  The system prompt
    is ``AGENT_CONSTRAINT + skill_content`` and the user prompt is built
    per-task from instruction, API docs, and supervisor info.

    Args:
        prompt_file_path: Path to a Jinja2 template file (required for template mode).
        skill_dir: Path to the directory containing SKILL.md (required for skill mode).
        pi_model: Model name used by the pi CLI.
        pi_provider: Model provider used by the pi CLI.
        pi_timeout: Timeout in seconds for a single pi CLI invocation.
        pi_max_turns: Maximum number of turns per pi CLI session.
        pi_thinking: Thinking level setting for the pi CLI.
        pi_extensions: List of extension file paths to load into the pi CLI.
        pi_api_key: Optional API key for the pi CLI.
        pi_base_url: Optional API base URL for the pi CLI.
        ignore_multiple_calls: If True, only execute the first code block; otherwise concatenate all.
        max_prompt_length: Maximum prompt length; truncates history if exceeded.
        **kwargs: Additional keyword arguments passed to the Agent base class (e.g. model_config, max_steps).
    """

    def __init__(
        self,
        prompt_file_path: str | None = None,
        skill_dir: str | None = None,
        pi_model: str = "kimi-k2.5",
        pi_provider: str = "anthropic",
        pi_timeout: int = 300,
        pi_max_turns: int = 50,
        pi_thinking: str | None = None,
        pi_extensions: list[str] | None = None,
        pi_api_key: str | None = None,
        pi_base_url: str | None = None,
        ignore_multiple_calls: bool = True,
        max_prompt_length: int | None = None,
        **kwargs: Any,
    ):
        super().__init__(**kwargs)
        if prompt_file_path is None and skill_dir is None:
            raise ValueError("Either prompt_file_path or skill_dir must be provided")
        self._mode = "skill" if skill_dir is not None else "template"
        self.prompt_template: str | None = None
        self.skill_content: str | None = None
        if self._mode == "template":
            assert prompt_file_path is not None
            self.prompt_template = cast(str, read_file(prompt_file_path.replace("/", os.sep))).lstrip()
        else:
            assert skill_dir is not None
            self.skill_content = load_skill(skill_dir)
            # Collect .ts extension files from the extensions/ sibling directory
            ext_dir = Path(skill_dir).parent / "extensions"
            if pi_extensions is None and ext_dir.is_dir():
                pi_extensions = sorted(
                    str(f) for f in ext_dir.iterdir() if f.suffix == ".ts"
                )
        self.pi_model = pi_model
        self.pi_provider = pi_provider
        self.pi_timeout = pi_timeout
        self.pi_max_turns = pi_max_turns
        self.pi_thinking = pi_thinking
        self.pi_extensions = pi_extensions or []
        self.pi_api_key = pi_api_key
        self.pi_base_url = pi_base_url
        self.ignore_multiple_calls = ignore_multiple_calls
        self.max_prompt_length = max_prompt_length
        self._session_dir: str | None = None
        self._session_id: str | None = None
        self._conversation_text: str = ""

    # ------------------------------------------------------------------
    # Agent lifecycle
    # ------------------------------------------------------------------

    def initialize(self, world: AppWorld) -> None:
        super().initialize(world)
        if self._mode == "template":
            self._initialize_template(world)
        else:
            self._initialize_skill(world)
        # Reset session
        self._session_dir = tempfile.mkdtemp(prefix="pi-appworld-")
        self._session_id = None

    def _initialize_template(self, world: AppWorld) -> None:
        """Build prompt from a Jinja2 template file (original mode)."""
        assert self.prompt_template is not None
        template = Template(self.prompt_template)
        app_descriptions = json.dumps(
            [{"name": k, "description": v} for k, v in world.task.app_descriptions.items()],
            indent=1,
        )
        api_docs = world.task.api_docs
        api_docs = api_docs.compress_parameters().remove_fields(["path", "method"])
        api_doc_string = dump_yaml(dict(api_docs)).rstrip()
        template_params = {
            "instruction": world.task.instruction,
            "main_user": world.task.supervisor,
            "app_descriptions": app_descriptions,
            "api_documentation": api_doc_string,
            "available_imports": SAID_AVAILABLE_IMPORTS,
        }
        output_str = template.render(template_params)
        if self.max_prompt_length is not None:
            output_str = self._truncate_input(output_str)
        self._conversation_text = output_str + "\n\n"

    def _initialize_skill(self, world: AppWorld) -> None:
        """Build prompt from AGENT_CONSTRAINT + SKILL.md (skill mode)."""
        assert self.skill_content is not None
        system_prompt = build_system_prompt(self.skill_content)
        api_docs = world.task.api_docs
        api_docs = api_docs.compress_parameters().remove_fields(["path", "method"])
        api_doc_string = dump_yaml(dict(api_docs)).rstrip()
        task_context = build_task_context(
            instruction=world.task.instruction,
            app_descriptions=world.task.app_descriptions,
            api_doc_string=api_doc_string,
            supervisor=world.task.supervisor,
            available_imports=SAID_AVAILABLE_IMPORTS,
        )
        prompt_text = "SYSTEM:\n" + system_prompt + "\n\nUSER:\n" + task_context
        self._conversation_text = prompt_text + "\n\n"

    def next_execution_inputs_usage_and_status(
        self, last_execution_outputs: Sequence[ExecutionIO]
    ) -> tuple[Sequence[ExecutionIO], Usage, Status]:
        if last_execution_outputs:
            assert len(last_execution_outputs) == 1, "PiCodeAgent expects exactly one execution output."
            last_output = last_execution_outputs[0].content
            self.logger.show_message(
                role="environment",
                content=last_output,
                step_number=self.step_number,
            )
            maybe_newline = "\n" if not last_output.endswith("\n") else ""
            feedback = "Output:\n```\n" + last_output + maybe_newline + "```\n\n"
            self._conversation_text += feedback

        # Invoke pi CLI
        is_followup = self._session_id is not None
        raw_stdout, returncode = self._run_pi(self._conversation_text, followup=is_followup)

        if returncode != 0 and not raw_stdout.strip():
            error_msg = f"pi CLI failed with return code {returncode}"
            self.logger.show_message(role="termination", content=error_msg)
            return [], Usage(), Status(failed=True, message=error_msg)

        # Parse pi output
        events = self._parse_pi_stream(raw_stdout)

        # Extract session id from first invocation
        if not is_followup:
            self._session_id = self._extract_session_id(events)

        # Reconstruct assistant response text from events
        assistant_text = self._reconstruct_assistant_text(events)
        usage = self._extract_usage(events)

        # Extract code blocks
        code = self._extract_code(assistant_text)

        self._conversation_text += assistant_text + "\n\n"

        reasoning_content = ""  # pi stream doesn't expose reasoning in a simple way
        self.logger.show_message(
            role="agent",
            content=assistant_text,
            reasoning_content=reasoning_content,
            step_number=self.step_number,
        )

        if not code:
            self._conversation_text += (
                "Please write Python code in a ```python ... ``` code block to make progress on the task.\n\n"
            )
            return [], usage, Status(failed=False)

        return [ExecutionIO(content=code)], usage, Status(failed=False)

    # ------------------------------------------------------------------
    # pi CLI interaction
    # ------------------------------------------------------------------

    def _build_pi_cmd(self, followup: bool = False) -> list[str]:
        cmd = [
            "pi",
            "--print",
            "--mode", "json",
            "--provider", self.pi_provider,
            "--model", self.pi_model,
            "--no-extensions",
            "--no-skills",
            "--no-context-files",
            "--max-turns", str(self.pi_max_turns),
        ]
        for ext in self.pi_extensions:
            cmd.extend(["--extension", ext])
        if self.pi_api_key:
            cmd.extend(["--api-key", self.pi_api_key])
        if self.pi_base_url:
            cmd.extend(["--base-url", self.pi_base_url])
        if self.pi_thinking:
            cmd.extend(["--thinking", self.pi_thinking])
        if self._session_dir:
            cmd.extend(["--session-dir", self._session_dir])
        if followup and self._session_id:
            cmd.extend(["--session", self._session_id])
        return cmd

    def _make_env(self) -> dict:
        import sys

        env = os.environ.copy()
        venv_bin = os.path.dirname(sys.executable)
        if venv_bin not in env.get("PATH", "").split(os.pathsep):
            env["PATH"] = venv_bin + os.pathsep + env.get("PATH", "")
        env.pop("MallocNanoZone", None)
        env.pop("MallocStackLogging", None)
        env.pop("MallocStackLoggingOptions", None)
        env["OBJC_DISABLE_INITIALIZE_FORK_SAFETY"] = "YES"
        return env

    def _run_pi(self, prompt: str, followup: bool = False) -> tuple[str, int]:
        """Run pi CLI synchronously and return (stdout, returncode)."""
        cmd = self._build_pi_cmd(followup)
        env = self._make_env()
        try:
            result = subprocess.run(
                cmd,
                input=prompt.encode(),
                capture_output=True,
                env=env,
                timeout=self.pi_timeout,
            )
        except subprocess.TimeoutExpired:
            return "", 1
        except FileNotFoundError:
            return "", 1

        raw = result.stdout.decode() if result.stdout else ""
        if result.returncode != 0:
            if not raw.strip():
                return "", result.returncode
        return raw, result.returncode

    # ------------------------------------------------------------------
    # Output parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_pi_stream(raw: str) -> list[dict]:
        events: list[dict] = []
        for line in raw.strip().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return events

    @staticmethod
    def _extract_session_id(events: list[dict]) -> str | None:
        for event in events:
            if event.get("type") == "session" and event.get("id"):
                return event["id"]
        return None

    @staticmethod
    def _reconstruct_assistant_text(events: list[dict]) -> str:
        """Extract the assistant's text content from pi stream events."""
        parts: list[str] = []
        for event in events:
            if event.get("type") == "message_end":
                message = event.get("message", {})
                if message.get("role") == "assistant":
                    for content_block in message.get("content", []):
                        if isinstance(content_block, dict) and content_block.get("type") == "text":
                            parts.append(content_block.get("text", ""))
        if parts:
            return "\n".join(parts)
        # Fallback: try to extract from message_update events
        for event in events:
            if event.get("type") == "message_update":
                delta = event.get("delta", {})
                if delta.get("type") == "text_delta":
                    text = delta.get("text", "")
                    if text:
                        parts.append(text)
        if parts:
            return "".join(parts)
        # Last resort: try result events
        for event in reversed(events):
            if event.get("type") == "result" and not event.get("is_error"):
                result_text = event.get("result", "")
                if result_text:
                    return str(result_text)
        return ""

    @staticmethod
    def _extract_usage(events: list[dict]) -> Usage:
        """Best-effort extraction of token usage from pi stream events."""
        total_input = 0
        total_output = 0
        for event in events:
            usage_dict = event.get("usage")
            if isinstance(usage_dict, dict):
                total_input += usage_dict.get("input_tokens", 0)
                total_output += usage_dict.get("output_tokens", 0)
        if total_input == 0 and total_output == 0:
            return Usage()
        tokens = Tokens(
            input_cache_miss=total_input,
            input_cache_hit=0,
            input_cache_write=0,
            output=total_output,
        )
        cost_per_token = CostPerToken()
        cost = Cost.build(tokens=tokens, cost_per_token=cost_per_token)
        return Usage(tokens=tokens, cost_per_token=cost_per_token, cost=cost)

    def _extract_code(self, text: str) -> str:
        """Extract Python code from the model output.

        If ignore_multiple_calls is True, return the first code block only.
        Otherwise, concatenate all code blocks.
        Also handles partial code blocks (no closing ```).
        """
        output_code = ""
        match_end = 0
        for match in _FULL_CODE_RE.finditer(text):
            code = match.group(1).strip()
            if self.ignore_multiple_calls:
                return code
            output_code += code + "\n"
            match_end = match.end()
        # Check for partial code at the end (no closing ```)
        partial_match = _PARTIAL_CODE_RE.match(text[match_end:])
        if partial_match:
            output_code += partial_match.group(1).strip()
        return output_code

    # ------------------------------------------------------------------
    # Prompt truncation (same logic as SimplifiedReActCodeAgent)
    # ------------------------------------------------------------------

    def _truncate_input(self, input_str: str) -> str:
        if self.max_prompt_length is None:
            return input_str
        goal_index = input_str.rfind("Task:")
        if goal_index == -1:
            raise ValueError(f"No goal found in input string:\n{input_str}")
        next_new_line_index = input_str.find("\n", goal_index) + 1
        init_prompt = input_str[:next_new_line_index]
        prompt = input_str[next_new_line_index:]
        if len(init_prompt) > self.max_prompt_length:
            raise ValueError("Input prompt longer than max allowed length")
        if len(prompt) > self.max_prompt_length - len(init_prompt):
            new_prompt = prompt[-(self.max_prompt_length - len(init_prompt)):]
            cmd_index = new_prompt.find("ASSISTANT:") if "ASSISTANT:" in new_prompt else 0
            prompt = "\n[TRIMMED HISTORY]\n\n" + new_prompt[cmd_index:]
        return init_prompt + prompt


# ------------------------------------------------------------------
# Convenience functions for external callers (skill mode)
# ------------------------------------------------------------------

def run_task_with_skill(
    task_id: str,
    skill_dir: str | Path | None = None,
    *,
    experiment_name: str = "skill_runner",
    pi_model: str = "kimi-k2.5",
    pi_provider: str = "anthropic",
    pi_timeout: int = 300,
    pi_max_turns: int = 50,
    max_steps: int = 50,
    pi_api_key: str | None = None,
    pi_base_url: str | None = None,
    pi_thinking: str | None = None,
    pi_extensions: list[str] | None = None,
    appworld_root: str | None = None,
) -> None:
    """Run a single AppWorld task using a skill-driven pi_code_agent.

    Constructs the system prompt from AGENT_CONSTRAINT + SKILL.md, builds the
    per-task user prompt from task metadata, and runs the agent.

    This function is the main entry point for external callers (e.g. auto-opt).
    It handles:
      1. Loading SKILL.md from ``skill_dir``
      2. Building system + user prompts
      3. Creating and running SimplifiedPiCodeAgent
      4. Writing the prompt to a temp file (cleaned up after)

    Args:
        task_id: The AppWorld task ID to run.
        skill_dir: Path to the directory containing SKILL.md. If None or
            SKILL.md doesn't exist, only AGENT_CONSTRAINT is used.
        experiment_name: Experiment name for output directory.
        pi_model: Model name for the pi CLI.
        pi_provider: Provider name for the pi CLI.
        pi_timeout: Timeout per pi invocation in seconds.
        pi_max_turns: Max turns per pi session.
        max_steps: Max code execution steps.
        pi_api_key: Optional API key.
        pi_base_url: Optional API base URL.
        pi_thinking: Optional thinking level for pi.
        pi_extensions: Optional list of extension file paths.
        appworld_root: Optional override for APPWORLD_ROOT.
    """
    if appworld_root:
        from appworld import update_root
        update_root(appworld_root)

    agent: SimplifiedPiCodeAgent = SimplifiedPiCodeAgent(  # type: ignore[call-arg]
        skill_dir=skill_dir,  # type: ignore[arg-type]
        pi_model=pi_model,  # type: ignore[arg-type]
        pi_provider=pi_provider,  # type: ignore[arg-type]
        pi_timeout=pi_timeout,  # type: ignore[arg-type]
        pi_max_turns=pi_max_turns,  # type: ignore[arg-type]
        pi_api_key=pi_api_key,  # type: ignore[arg-type]
        pi_base_url=pi_base_url,  # type: ignore[arg-type]
        pi_thinking=pi_thinking,  # type: ignore[arg-type]
        pi_extensions=pi_extensions,  # type: ignore[arg-type]
        ignore_multiple_calls=True,  # type: ignore[arg-type]
        max_steps=max_steps,
        skip_if_finished=True,
        model_config={
            "name": pi_model,
            "cost_per_token": {
                "input_cache_miss": 0.0,
                "input_cache_hit": 0.0,
                "input_cache_write": 0.0,
                "output": 0.0,
            },
        },
    )

    agent.solve_tasks(
        task_ids=[task_id],
        experiment_name=experiment_name,
        num_processes=1,
    )


def run_batch_with_skill(
    task_ids: list[str],
    skill_dir: str | Path | None = None,
    *,
    experiment_name: str = "skill_runner",
    pi_model: str = "kimi-k2.5",
    pi_provider: str = "anthropic",
    pi_timeout: int = 300,
    pi_max_turns: int = 50,
    max_steps: int = 50,
    pi_api_key: str | None = None,
    pi_base_url: str | None = None,
    pi_thinking: str | None = None,
    pi_extensions: list[str] | None = None,
    appworld_root: str | None = None,
) -> None:
    """Run a batch of AppWorld tasks using a skill-driven pi_code_agent.

    Each task is run sequentially with the same skill configuration.
    The agent is re-created per task to ensure clean state.

    Args:
        task_ids: List of AppWorld task IDs to run.
        skill_dir: Path to the directory containing SKILL.md.
        experiment_name: Experiment name for output directory.
        pi_model: Model name for the pi CLI.
        pi_provider: Provider name for the pi CLI.
        pi_timeout: Timeout per pi invocation in seconds.
        pi_max_turns: Max turns per pi session.
        max_steps: Max code execution steps.
        pi_api_key: Optional API key.
        pi_base_url: Optional API base URL.
        pi_thinking: Optional thinking level for pi.
        pi_extensions: Optional list of extension file paths.
        appworld_root: Optional override for APPWORLD_ROOT.
    """
    agent: SimplifiedPiCodeAgent = SimplifiedPiCodeAgent(  # type: ignore[call-arg]
        skill_dir=skill_dir,  # type: ignore[arg-type]
        pi_model=pi_model,  # type: ignore[arg-type]
        pi_provider=pi_provider,  # type: ignore[arg-type]
        pi_timeout=pi_timeout,  # type: ignore[arg-type]
        pi_max_turns=pi_max_turns,  # type: ignore[arg-type]
        pi_api_key=pi_api_key,  # type: ignore[arg-type]
        pi_base_url=pi_base_url,  # type: ignore[arg-type]
        pi_thinking=pi_thinking,  # type: ignore[arg-type]
        pi_extensions=pi_extensions,  # type: ignore[arg-type]
        ignore_multiple_calls=True,  # type: ignore[arg-type]
        max_steps=max_steps,
        skip_if_finished=True,
        model_config={
            "name": pi_model,
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
        num_processes=1,
    )

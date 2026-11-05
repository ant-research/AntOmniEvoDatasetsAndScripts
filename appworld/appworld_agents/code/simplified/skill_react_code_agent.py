import os
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

_SKILL_RESOURCE_SUBDIRS = ("references", "scripts")
_SKILL_LISTING_MAX_ENTRIES = 200
_SKILL_LISTING_MAX_DEPTH = 3


def _render_skill_resources(skill_dir: str | os.PathLike[str]) -> str:
    """Return a markdown block describing files under ``skill_dir`` that the
    model can read at runtime via ``open()``.

    Enumerates the top of ``skill_dir`` plus its ``references/`` and
    ``scripts/`` subtrees (up to a small depth) so the model knows what is
    available without a directory-listing tool.
    """
    root = Path(skill_dir).resolve()
    if not root.is_dir():
        return ""

    def _walk(base: Path) -> list[str]:
        rows: list[str] = []
        for path in sorted(base.rglob("*")):
            try:
                rel = path.relative_to(root)
            except ValueError:
                continue
            if len(rel.parts) > _SKILL_LISTING_MAX_DEPTH:
                continue
            if any(part.startswith(".") or part == "__pycache__" for part in rel.parts):
                continue
            if path.is_dir():
                rows.append(f"- {rel.as_posix()}/")
            elif path.is_file():
                try:
                    size = path.stat().st_size
                except OSError:
                    size = 0
                rows.append(f"- {rel.as_posix()} ({size} bytes)")
            if len(rows) >= _SKILL_LISTING_MAX_ENTRIES:
                rows.append("- ... (listing truncated)")
                return rows
        return rows

    sections: list[str] = []
    for name in _SKILL_RESOURCE_SUBDIRS:
        sub = root / name
        if not sub.is_dir():
            continue
        entries = _walk(sub)
        if not entries:
            continue
        sections.append(f"### {name}/\n" + "\n".join(entries))

    if not sections:
        return ""

    return (
        "\n\n## Skill Resources\n\n"
        f"Extra reference material lives on disk at `{root}`. "
        "You can read any of the files listed below inside your Python code — "
        "`open()` is available (read-only) and `pathlib`/`os` are importable. "
        "Example:\n\n"
        "```python\n"
        f"from pathlib import Path\n"
        f"SKILL_DIR = Path({str(root)!r})\n"
        "print((SKILL_DIR / 'references/some_file.md').read_text())\n"
        "```\n\n"
        + "\n\n".join(sections)
    )

from appworld import AppWorld
from appworld.common.io import dump_yaml
from appworld.common.time import freezegun_bypassed_datetime
from appworld.environment import SAID_AVAILABLE_IMPORTS
from appworld_agents.code.common.usage_tracker import Usage
from appworld_agents.code.simplified.agent import Agent, ExecutionIO, Status
from appworld_agents.code.simplified.pi_code_agent import (
    build_system_prompt,
    build_task_context,
    load_skill,
)
from appworld_agents.code.simplified.react_code_agent import SimplifiedReActCodeAgent
from appworld_agents.code.simplified.trajectory import Span, Trajectory


@Agent.register("simplified_skill_react_code_agent")
class SimplifiedSkillReActCodeAgent(SimplifiedReActCodeAgent):  # type: ignore[misc]  # pyright: ignore[reportIncompatibleMethodOverride]
    """A ReAct-style agent that uses a fixed system prompt with skill support.

    Unlike the base SimplifiedReActCodeAgent which renders a Jinja2 template,
    this agent constructs the conversation from:

    - A fixed SYSTEM message built from ``_AGENT_CONSTRAINT`` + optional
      SKILL.md content loaded from ``skill_dir``.
    - A USER message built per-task from API docs, supervisor info, and the
      task instruction (via ``build_task_context``).

    After each task, it writes a trajectory JSON file recording every step's
    input (code) and output (execution result) using the ``Trajectory``/``Span``
    model.
    """

    def __init__(
        self,
        skill_dir: str | None = None,
        prompt_file_path: str | None = None,
        ignore_multiple_calls: bool = True,
        max_prompt_length: int | None = None,
        max_output_length: int | None = None,
        **kwargs: Any,
    ):
        if skill_dir is None and prompt_file_path is None:
            raise ValueError("Either skill_dir or prompt_file_path must be provided")

        self._mode = "skill" if skill_dir is not None else "template"
        self.skill_dir = skill_dir
        self.skill_content: str | None = None

        if self._mode == "skill":
            assert skill_dir is not None
            self.skill_content = load_skill(skill_dir)

        # Initialise base Agent + ReAct attributes.
        # In skill mode we skip SimplifiedReActCodeAgent.__init__ because it
        # requires a prompt_file_path to read.  In template mode we delegate
        # normally so the Jinja2 template is loaded.
        if self._mode == "template":
            super().__init__(  # type: ignore[call-arg]
                prompt_file_path=prompt_file_path,  # type: ignore[arg-type]
                ignore_multiple_calls=ignore_multiple_calls,  # type: ignore[arg-type]
                max_prompt_length=max_prompt_length,  # type: ignore[arg-type]
                max_output_length=max_output_length,  # type: ignore[arg-type]
                **kwargs,
            )
        else:
            # Skill mode: skip SimplifiedReActCodeAgent.__init__, initialise
            # Agent directly and set the ReAct attributes ourselves.
            Agent.__init__(self, **kwargs)
            self.prompt_template: str | None = None
            self.max_prompt_length = max_prompt_length
            self.max_output_length = max_output_length
            self.ignore_multiple_calls = ignore_multiple_calls
            self.partial_code_regex = r".*```python\n(.*)"
            self.full_code_regex = r"```python\n(.*?)```"

        # Trajectory state — populated per task.
        self._trajectory_spans: list[Span] = []

    # ------------------------------------------------------------------
    # Agent lifecycle
    # ------------------------------------------------------------------

    def initialize(self, world: AppWorld) -> None:
        if self._mode == "template":
            super().initialize(world)
            # After parent init, extract system & user prompts from messages.
            system_prompt_text = ""
            user_prompt_text = ""
            for msg in self.messages:
                if msg.get("role") == "system":
                    system_prompt_text = msg.get("content", "")
                elif msg.get("role") == "user":
                    user_prompt_text = msg.get("content", "")
        else:
            # Skill mode — skip SimplifiedReActCodeAgent.initialize (which uses
            # self.prompt_template for Jinja2 rendering) and initialise Agent
            # directly.  Then build messages ourselves.
            Agent.initialize(self, world)

            # 1. Build system prompt.
            assert self.skill_content is not None
            system_prompt_text = build_system_prompt(self.skill_content)
            # Append a listing of references/scripts under skill_dir so the
            # model knows the resources exist and how to read them.
            if self.skill_dir is not None:
                system_prompt_text += _render_skill_resources(self.skill_dir)

            # 2. Build user prompt from task context.
            api_docs = world.task.api_docs
            api_docs = api_docs.compress_parameters().remove_fields(["path", "method"])
            api_doc_string = dump_yaml(dict(api_docs)).rstrip()
            user_prompt_text = build_task_context(
                instruction=world.task.instruction,
                app_descriptions=world.task.app_descriptions,
                api_doc_string=api_doc_string,
                supervisor=world.task.supervisor,
                available_imports=SAID_AVAILABLE_IMPORTS,
            )

            # 4. Set messages.
            self.messages = [
                {"role": "system", "content": system_prompt_text},
                {"role": "user", "content": user_prompt_text},
            ]
            self.num_instruction_messages = len(self.messages)

        # 5. Reset trajectory and record the initial prompts as spans.
        now = freezegun_bypassed_datetime()
        self._trajectory_spans = []
        if system_prompt_text:
            self._trajectory_spans.append(
                Span(
                    name="system_prompt",
                    span_type="system_prompt",
                    input=system_prompt_text,
                    output=None,
                    start_time=now,
                    end_time=now,
                    children=[],
                )
            )
        if user_prompt_text:
            self._trajectory_spans.append(
                Span(
                    name="user_prompt",
                    span_type="user_prompt",
                    input=user_prompt_text,
                    output=None,
                    start_time=now,
                    end_time=now,
                    children=[],
                )
            )

    def next_execution_inputs_usage_and_status(
        self, last_execution_outputs: Sequence[ExecutionIO]
    ) -> tuple[Sequence[ExecutionIO], Usage, Status]:
        # ``last_execution_outputs`` is the output of the PREVIOUSLY-recorded
        # agent step's code. Attach it to the last in-flight span, if any,
        # so that each step's output is correctly paired with its input.
        if last_execution_outputs:
            for prev in reversed(self._trajectory_spans):
                if prev.span_type == "agent_step" and prev.output is None:
                    prev.output = last_execution_outputs[0].content
                    break

        start_time: datetime = freezegun_bypassed_datetime()

        execution_inputs, usage, status = super().next_execution_inputs_usage_and_status(
            last_execution_outputs
        )

        end_time: datetime = freezegun_bypassed_datetime()

        # Append a new in-flight span for the code that is about to be
        # executed. Its output stays None until the next call fills it in
        # (or, for the final step, until ``_save_trajectory`` does).
        code_sent = execution_inputs[0].content if execution_inputs else ""
        span = Span(
            name=f"step_{self.step_number}",
            span_type="agent_step",
            input=code_sent,
            output=None,
            start_time=start_time,
            end_time=end_time,
            children=[],
        )
        self._trajectory_spans.append(span)

        return execution_inputs, usage, status

    # ------------------------------------------------------------------
    # solve_task with trajectory output
    # ------------------------------------------------------------------

    def solve_task(self, task_id: str) -> None:
        super().solve_task(task_id)
        self._save_trajectory()

    def _save_trajectory(self) -> None:
        if self.world is None:
            return
        # Fill the final step's output from the environment log, since
        # ``next_execution_inputs_usage_and_status`` is not called again
        # after the last step executes.
        env_io = getattr(self.world, "environment_io", None)
        if env_io:
            for prev in reversed(self._trajectory_spans):
                if prev.span_type == "agent_step" and prev.output is None:
                    last = env_io[-1]
                    if isinstance(last, dict) and last.get("input") == prev.input:
                        prev.output = last.get("output")
                    break
        trajectory = Trajectory(
            root_span_list=self._trajectory_spans,
            trace_id=self.world.task_id,
        )
        file_path = os.path.join(self.world.output_misc_directory, "trajectory.json")
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(trajectory.to_json())

"""Parse pi coding-agent NDJSON output into a :class:`Trajectory`.

This is a direct port of ``auto_opt.common.utils.trajectory_parser`` —
kept in-package so we don't drag auto-opt in as a dependency. Only the
``parse_pi_json_output`` variant is included because Terminal-Bench 2
runs the Pi agent (see :mod:`tbtest.custom_agent`). Usage-stats tracking
is stripped because Harbor already records that in each trial's
``result.json``.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime

from tbtest.trajectory import Span, Trajectory

logger = logging.getLogger(__name__)


def parse_pi_json_output(raw_output: str) -> Trajectory:
    """Parse a pi ``--mode json`` NDJSON stream into a Trajectory."""
    root_spans: list[Span] = []
    current_model_span: Span | None = None
    current_thinking_span: Span | None = None
    pending_children: list[Span] = []
    tool_result_map: dict[str, str] = {}

    ignored_event_types = {
        "session", "agent_start",
        "compaction_start", "compaction_end",
        "auto_retry_start", "auto_retry_end",
        "thinking_level_changed", "session_info_changed",
        "queue_update", "tool_execution_update",
        "turn_start", "turn_end",
    }

    def flush_current_model_span() -> None:
        nonlocal current_model_span, current_thinking_span, pending_children
        if current_model_span is None:
            pending_children = []
            current_thinking_span = None
            return
        for ch in pending_children:
            if ch.span_type == "tool_call":
                tc_id = ch.metadata.get("tool_call_id")
                if tc_id and tc_id in tool_result_map:
                    ch.output = {"result": tool_result_map.pop(tc_id)}
            current_model_span.add_child(ch)
        current_model_span.end_time = datetime.now()
        root_spans.append(current_model_span)
        current_model_span = None
        current_thinking_span = None
        pending_children = []

    def find_pending_tool_span(tc_id: str) -> Span | None:
        for ch in pending_children:
            if ch.span_type == "tool_call" and ch.metadata.get("tool_call_id") == tc_id:
                return ch
        return None

    def _iter_events(raw: str):
        for line_num, line in enumerate(raw.strip().splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                logger.warning(f"Skipping malformed JSON at line {line_num}: {line[:100]}")
                continue
            if isinstance(parsed, dict):
                yield parsed
            elif isinstance(parsed, list):
                for item in parsed:
                    if isinstance(item, dict):
                        yield item
                    else:
                        logger.warning(
                            f"Skipping non-dict element in nested array at line {line_num}: "
                            f"{type(item).__name__}"
                        )
            else:
                logger.warning(
                    f"Skipping unexpected JSON type at line {line_num}: {type(parsed).__name__}"
                )

    for event in _iter_events(raw_output):
        event_type = event.get("type")
        if event_type in ignored_event_types:
            continue

        if event_type == "message_start":
            message = event.get("message", {})
            if message.get("role") == "assistant":
                flush_current_model_span()
                current_model_span = Span(
                    name="model",
                    span_type="model",
                    input=None,
                    start_time=datetime.now(),
                )

        elif event_type == "message_update":
            if current_model_span is None:
                continue
            assistant_event = event.get("assistantMessageEvent", {})
            delta_type = assistant_event.get("type", "")

            if delta_type == "thinking_start":
                current_thinking_span = Span(
                    name="thinking",
                    span_type="thinking",
                    input=None,
                    output="",
                    start_time=datetime.now(),
                )
                pending_children.append(current_thinking_span)

            elif delta_type == "thinking_delta":
                if current_thinking_span is None:
                    current_thinking_span = Span(
                        name="thinking",
                        span_type="thinking",
                        input=None,
                        output="",
                        start_time=datetime.now(),
                    )
                    pending_children.append(current_thinking_span)
                delta = assistant_event.get("thinking") or assistant_event.get("text") or ""
                current_thinking_span.output = (current_thinking_span.output or "") + delta

            elif delta_type == "thinking_end":
                if current_thinking_span is not None:
                    canonical = assistant_event.get("content") or assistant_event.get("thinking")
                    if canonical:
                        current_thinking_span.output = canonical
                    current_thinking_span.end_time = datetime.now()
                    current_thinking_span = None

            elif delta_type == "text_delta":
                text = assistant_event.get("text", "")
                if text:
                    current_model_span.output = (current_model_span.output or "") + text

            elif delta_type == "toolcall_start":
                tool_call = assistant_event.get("toolCall", {})
                if tool_call.get("name"):
                    pending_children.append(Span(
                        name=tool_call["name"],
                        span_type="tool_call",
                        input=tool_call.get("arguments"),
                        start_time=datetime.now(),
                        metadata={"tool_call_id": tool_call.get("id", "")},
                    ))

            elif delta_type == "toolcall_end":
                tool_call = assistant_event.get("toolCall", {})
                tc_id = tool_call.get("id", "")
                existing = find_pending_tool_span(tc_id) if tc_id else None
                if existing:
                    args = tool_call.get("arguments")
                    if args is not None:
                        existing.input = args

        elif event_type == "message_end":
            message = event.get("message", {})
            role = message.get("role", "")

            if role == "assistant" and current_model_span is not None:
                content = message.get("content", [])
                text_parts: list[str] = []
                thinking_parts: list[str] = []
                if isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        btype = block.get("type", "")
                        if btype == "text":
                            text_parts.append(block.get("text", ""))
                        elif btype == "thinking":
                            thinking_parts.append(block.get("thinking", ""))
                        elif btype == "toolCall":
                            tc_id = block.get("id", "")
                            existing = find_pending_tool_span(tc_id) if tc_id else None
                            if existing:
                                args = block.get("arguments")
                                if args is not None:
                                    existing.input = args
                            else:
                                pending_children.append(Span(
                                    name=block.get("name", "unknown"),
                                    span_type="tool_call",
                                    input=block.get("arguments"),
                                    start_time=datetime.now(),
                                    metadata={"tool_call_id": tc_id},
                                ))

                if text_parts:
                    joined = "\n\n".join(t for t in text_parts if t)
                    if joined:
                        current_model_span.output = joined

                thinking_children = [ch for ch in pending_children if ch.span_type == "thinking"]
                for span_ch, full_text in zip(thinking_children, thinking_parts, strict=False):
                    if full_text:
                        span_ch.output = full_text
                for extra in thinking_parts[len(thinking_children):]:
                    if extra:
                        pending_children.append(Span(
                            name="thinking",
                            span_type="thinking",
                            input=None,
                            output=extra,
                            start_time=datetime.now(),
                            end_time=datetime.now(),
                        ))

                flush_current_model_span()

            elif role == "user":
                content = message.get("content", [])
                text_parts = []
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            text_parts.append(block.get("text", ""))
                if text_parts:
                    root_spans.append(Span(
                        name="input",
                        span_type="span",
                        input="\n".join(text_parts),
                        start_time=datetime.now(),
                        end_time=datetime.now(),
                    ))

            elif role == "toolResult":
                tc_id = message.get("toolCallId", "")
                if tc_id:
                    content = message.get("content", [])
                    text_parts = []
                    if isinstance(content, list):
                        for block in content:
                            if isinstance(block, dict) and block.get("type") == "text":
                                text_parts.append(block.get("text", ""))
                    tool_result_map[tc_id] = "\n".join(text_parts) if text_parts else str(content)

        elif event_type == "tool_execution_start":
            tc_id = event.get("toolCallId", "")
            existing = find_pending_tool_span(tc_id) if tc_id else None
            if existing is not None:
                existing.start_time = datetime.now()

        elif event_type == "tool_execution_end":
            tool_call_id = event.get("toolCallId", "")
            result = event.get("result", {})
            if tool_call_id and result:
                if isinstance(result, dict):
                    content = result.get("content", [])
                    text_parts = []
                    if isinstance(content, list):
                        for block in content:
                            if isinstance(block, dict) and block.get("type") == "text":
                                text_parts.append(block.get("text", ""))
                    tool_result_map[tool_call_id] = "\n".join(text_parts) if text_parts else str(result)
                else:
                    tool_result_map[tool_call_id] = str(result)
            if tool_call_id:
                end_ts = datetime.now()
                stamped = False
                pending = find_pending_tool_span(tool_call_id)
                if pending is not None:
                    pending.end_time = end_ts
                    stamped = True
                if not stamped:
                    for span in root_spans:
                        for child in span.children:
                            if (child.span_type == "tool_call"
                                    and child.metadata.get("tool_call_id") == tool_call_id):
                                child.end_time = end_ts
                                stamped = True
                                break
                        if stamped:
                            break

        elif event_type == "agent_end":
            flush_current_model_span()

    flush_current_model_span()

    for tc_id, result in tool_result_map.items():
        for span in root_spans:
            for child in span.children:
                if child.span_type == "tool_call" and child.metadata.get("tool_call_id") == tc_id:
                    child.output = {"result": result}
                    break

    return Trajectory(root_span_list=root_spans)

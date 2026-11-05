"""Trajectory model — records a generate run as a nested span tree.

A trajectory is a tree of spans serialized to JSON with the envelope
``{trajectory, session_id, trace_id, errors}``; each span carries its
``id``, ``parent_id``, ``name``, ``type``, ``input``, ``output`, timing,
and recursive ``children``. generate.py builds one per run and writes it
to ``trajectory.json``.

This module is a plain dataclass implementation (stdlib only; the project
has no pydantic dependency). ``add_child`` populates ``parent_span_id``
so the serialized tree is navigable both top-down and bottom-up.

Span tree shape produced by generate.py (one root span per run)::

    generate                      (root span)
    ├── load_pipeline             spec_dir -> materialized DAG summary
    ├── load_corpus               corpus path -> doc count
    ├── batch                     all queries
    │   └── query:<id>            one per query  (children added by run_query)
    │       └── node:<id>         one per executed DAG node (hits_in/out, preview)
    ├── write_predictions         -> predictions.jsonl
    └── write_trajectory          -> trajectory.json (this artifact records itself)

Contract this module exists to satisfy: **if a new stage is added to
generate.py, it MUST be wrapped in a span here** so its inputs, outputs,
and timing appear on the trajectory. The intermediate process is the
point — the pipeline's per-node steps are not visible in predictions.jsonl
alone; the trajectory is where they land.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


def _new_id() -> str:
    """Random span id (uuid4 as str)."""
    return str(uuid.uuid4())


@dataclass
class Span:
    """One node in the run's span tree.

    A span is opened (``start_time`` set at construction), optionally given children
    as the run progresses, then closed (``end_time`` + ``output`` stamped)
    once its work is done. ``span_type`` is ``"span"`` for grouping/phase spans
    and ``"tool_call"`` for concrete invocations (a node run, a file write).
    """

    name: str
    span_type: str = "span"
    id: str = field(default_factory=_new_id)
    parent_span_id: str | None = None
    input: Any | None = None
    output: Any | None = None
    start_time: datetime = field(default_factory=datetime.now)
    end_time: datetime | None = None
    children: list["Span"] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def add_child(self, child: "Span") -> "Span":
        """Attach a child span and record the parent link on it.

        Returns the child so callers can chain: ``span.add_child(Span(...))``.
        Populating ``parent_span_id`` here makes the serialized tree navigable
        bottom-up as well as top-down.
        """
        child.parent_span_id = self.id
        self.children.append(child)
        return child

    def close(self, output: Any | None = None) -> "Span":
        """Stamp ``end_time`` and (optionally) ``output``; return self.

        Closing is what makes a span's duration computable on disk
        (``end_time - start_time``). ``output`` records what the step
        produced — counts, paths, hit previews — so the trajectory is a
        faithful transcript, not just a call graph.
        """
        self.end_time = datetime.now()
        if output is not None:
            self.output = output
        return self

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the on-disk span shape (key order preserved)."""
        return {
            "id": self.id,
            "parent_id": self.parent_span_id,
            "name": self.name,
            "type": self.span_type,
            "input": self.input,
            "output": self.output,
            "start_time": self.start_time.isoformat(),
            "end_time": self.end_time.isoformat() if self.end_time else None,
            "children": [child.to_dict() for child in self.children],
        }


@dataclass
class Trajectory:
    """A run's full span tree plus run-level metadata.

    ``root_span_list`` holds the top-level span(s); generate.py uses a single
    root (``name`` ``"generate"``), but the model supports many —
    ``merge_trajectories`` flattens several runs into one such list. ``errors``
    captures fatal failures (load/run) so a caller can see a run failed without
    re-parsing the spans.
    """

    root_span_list: list[Span] = field(default_factory=list)
    session_id: str | None = None
    trace_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        # Note: `metadata` is intentionally omitted from serialization — the
        # on-disk envelope is {trajectory, session_id, trace_id, errors}; it
        # stays available in-process only.
        return {
            "trajectory": [span.to_dict() for span in self.root_span_list],
            "session_id": self.session_id,
            "trace_id": self.trace_id,
            "errors": self.errors,
        }

    def to_json(self, indent: int | None = 2) -> str:
        """Pretty JSON via ensure_ascii=False so Chinese/CJK in inputs survives.

        A duck-typed ``default`` handles non-stdlib scalars/arrays a node might
        return (e.g. numpy float32/int64 from recall_dense) without importing
        numpy here — we never want trajectory serialization to be the thing
        that crashes a run. Falls back to ``str`` so an unknown type degrades
        gracefully instead of raising TypeError.
        """
        def _default(obj: Any) -> Any:
            # numpy arrays/scalars expose .tolist(); python sets -> list.
            if hasattr(obj, "tolist"):
                return obj.tolist()
            if isinstance(obj, (set, frozenset)):
                return list(obj)
            return str(obj)

        return json.dumps(
            self.to_dict(), indent=indent, ensure_ascii=False, default=_default
        )


def merge_trajectories(trajectories: list[Trajectory]) -> Trajectory:
    """Merge multiple trajectories into one (spans + errors flattened)."""
    all_spans: list[Span] = []
    all_errors: list[str] = []
    for t in trajectories:
        all_spans.extend(t.root_span_list)
        all_errors.extend(t.errors)
    return Trajectory(root_span_list=all_spans, errors=all_errors)
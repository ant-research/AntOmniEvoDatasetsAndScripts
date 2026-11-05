"""Helpers for nodes to record typed input/output on the trajectory.

Why this exists — the gap a node fills when it has internal state
---------------------------------------------------------------
`run_query` already records a node's *boundary* I/O automatically: the full
hit lists going in and out, with `golden_present`/`golden_dropped`. That covers
"what did this node produce." But it can't show *how* — a graph node's hop
distance, which seeds it expanded, that dense retrieval silently disabled, the
query an LLM rewrite actually emitted. Those facts live inside the node, so the
node must surface them itself via `ctx.notes.append(...)`.

`ctx.notes` accepts both one-line strings (`"backend=rank_bm25 ..."`) and
structured dicts. For structured dicts, a value with no type tag is ambiguous
later — is `hops` an int? a list? — and numbers especially (score vs rank vs
count) get conflated during analysis. This module gives the node a tiny,
consistent way to attach an explicit type to every recorded value so the
trajectory records *what the value is* alongside the value itself.

Convention — the dict shape a node appends
------------------------------------------
    ctx.notes.append({
        "input":   {"query": io(query, "str"), "hop": io(hop, "int")},
        "output":  {"hits_out_count": io(len(out), "int")},
        "internal": {"scorer": io(scorer, "str"), "expanded": io(expanded, "int")},
    })

`input`/`output` mirror the node fn's signature-level I/O (typed); `internal`
holds purely node-internal state (backend chosen, cache hit, traversal path).
A node may append several entries, and mix str + dict — all land on its span.

This is advisory (a node can append a bare dict too), but following it keeps
trajectories uniform across new modules — graph, rewrite-LLM, fuse, anything.
"""

from __future__ import annotations

from typing import Any


def io(value: Any, type_: str) -> dict[str, Any]:
    """Wrap a recorded value with its declared type tag.

    Args:
        value: the value to record (will be serialized as-is into the span).
        type_: a short type string the node asserts about `value`, e.g.
            "str", "int", "float", "list[dict]", "list[str]", "dict",
            "bool", "list[str]|None". This is a human/analyst-facing label,
            NOT auto-inferred — the node knows its own conventions best, and
            asserting the type explicitly is the whole point (see module doc).

    Returns:
        ``{"value": value, "type": type_}`` — the canonical cell shape for a
        typed field on the trajectory. Leaves it to the node to assemble the
        surrounding {"input"/"output"/"internal"} dict (see convention above).
    """
    return {"value": value, "type": type_}
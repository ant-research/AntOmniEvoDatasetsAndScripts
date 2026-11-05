"""truncate node — take the top-k hits. Terminal node of the DAG."""

from __future__ import annotations

from typing import TYPE_CHECKING

from retrieval_test.node_io import io

if TYPE_CHECKING:
    from retrieval_test.spec_loader import PipelineCtx


def init(params: dict, ctx: "PipelineCtx") -> None:
    pass


async def run(query: str, hits: list[dict], params: dict, ctx: "PipelineCtx") -> list[dict]:
    """Return the top-`k` hits. `k` must be >= 1 (k=0 makes eval degenerate).

    A `min-truncate-k` style constraint was previously a separate extension
    rule; it now lives here as a plain guard: if `k < 1`, warn on the trajectory
    and pass hits through unchanged rather than truncate to zero.
    """
    k = int(params.get("k", 10))
    if k < 1:
        ctx.notes.append({
            "internal": {
                "warning": io("k<1 would truncate to 0 (degenerate); passing hits through unchanged", "str"),
                "k_requested": io(k, "int"),
            }
        })
        return hits
    out = hits[:k]
    ctx.notes.append({
        "output": {"hits_out_count": io(len(out), "int")},
        "internal": {"k": io(k, "int")},
    })
    return out

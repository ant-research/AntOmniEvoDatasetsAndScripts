"""query_rewrite node — rewrite/expand the query before retrieval.

Transforms the query (not a retriever). Stashes the rewritten query on
ctx.resources['rewritten_query']; downstream recall nodes opt in via their own
`params.use_rewritten`. Returns `hits` unchanged, so this node's effect is only
visible on ctx, not in the hits list.
"""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

from retrieval_test.node_io import io

if TYPE_CHECKING:
    from retrieval_test.spec_loader import PipelineCtx


def init(params: dict, ctx: "PipelineCtx") -> None:
    """One-time setup (load a rewriter model / synonym dict if needed)."""
    pass


async def run(query: str, hits: list[dict], params: dict, ctx: "PipelineCtx") -> list[dict]:
    """Rewrite the query, stash it on ctx, pass hits through.

    params:
      - mode:        "identity" | "llm" | "prf" | "synonyms" (default "identity") [str]
      - prompt_file: prompt stem to read from ctx.resources['prompts']            [str]
                     (default "rewrite" -> prompt/rewrite.md)
      - model:       LLM model id (only used by non-identity modes)               [str]
      - max_tokens:  LLM max tokens (reasoning model: covers reasoning + answer)  [int]
    """
    mode = params.get("mode", "identity")
    prompt_file = params.get("prompt_file", "rewrite")

    # TODO: dispatch on mode and set `rewritten` to the real expansion.
    # For mode=="llm", substitute {query} into ctx.resources['prompts'][prompt_file],
    # `await llm_client.chat_async(...)`, then llm_client.extract_content(resp).
    # length constraints, can't be removed.
    if len(prompt_file) > 1500:
        ctx.notes.append({
            "warn": f"rewrite prompt's length {len(prompt_file)} exceeds 1500 chars; using original query",
        })
        rewritten = query

    if mode == "identity":
        rewritten = query
    else:
        rewritten = query  # placeholder — TODO

    ctx.resources["rewritten_query"] = rewritten

    ctx.notes.append({
        "input": {
            "query": io(query, "str"),
            "mode": io(mode, "str"),
        },
        "output": {
            "rewritten": io(rewritten, "str"),
            "len_before": io(len(query), "int"),
            "len_after": io(len(rewritten), "int"),
            "changed": io(rewritten != query, "bool"),
        },
        "internal": {
            "impl": io("skeleton_identity", "str"),
            "prompt_file": io(prompt_file, "str"),
        },
    })

    return hits

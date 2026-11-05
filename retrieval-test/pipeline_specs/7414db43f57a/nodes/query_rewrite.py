"""query_rewrite node — rewrite/expand the query before retrieval.

Transforms the query (not a retriever). Stashes the rewritten query on
ctx.resources['rewritten_query']; downstream recall nodes opt in via their own
`params.use_rewritten`. Returns `hits` unchanged, so this node's effect is only
visible on ctx, not in the hits list.
"""

from __future__ import annotations

import time
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
      - mode:        "identity" | "llm" (default "identity") [str]
      - prompt_file: prompt stem to read from ctx.resources['prompts']            [str]
                     (default "rewrite" -> prompt/rewrite.md)
      - model:       LLM model id (default "GLM-5.2")               [str]
      - max_tokens:  LLM max output tokens (default 2048, hard cap 2048)  [int]
      - temperature: LLM temperature (default 0.0) [float]
    """
    mode = params.get("mode", "identity")
    prompt_file = params.get("prompt_file", "rewrite")

    if mode == "identity":
        rewritten = query
    elif mode == "llm":
        from retrieval_test.llm_client import chat_async, extract_content
        from retrieval_test.errors import RateLimitError

        llm_model = params.get("model", "GLM-5.2")
        max_tok = int(params.get("max_tokens", 2048))
        temperature = float(params.get("temperature", 0.0))

        prompt_template = ctx.resources.get("prompts", {}).get(prompt_file, "")
        if not prompt_template:
            rewritten = query
            ctx.notes.append({
                "internal": {
                    "model": io(llm_model, "str"),
                    "status": io("fallback", "str"),
                    "fallback_reason": io(f"prompt template '{prompt_file}' not found", "str"),
                }
            })
        else:
            prompt_text = prompt_template.replace("{query}", query)
            messages = [
                {"role": "system", "content": "You are a retrieval query rewriting assistant. Decompose multi-hop questions into sub-queries and expand key entities with synonyms to improve document recall."},
                {"role": "user", "content": prompt_text},
            ]
            t0 = time.perf_counter()
            try:
                resp = await chat_async(
                    messages, model=llm_model, max_tokens=max_tok, temperature=temperature
                )
                rewritten = extract_content(resp).strip()

                # Strip reasoning/analysis boilerplate the LLM may emit despite
                # prompt instructions to output only sub-queries. Remove markdown
                # headers (##), reasoning chain labels, and re-examining sections.
                import re
                # Remove lines that start with ## (markdown headings)
                rewritten = re.sub(r'^##\s+.*$', '', rewritten, flags=re.MULTILINE)
                # Remove lines that are bold section labels like **Hop 1 ...**
                rewritten = re.sub(r'^\*\*.*\*\*\s*$', '', rewritten, flags=re.MULTILINE)
                # Remove 'Re-examining:' paragraphs
                rewritten = re.sub(r'\*\*Re-examining:\*\*[\s\S]*?(?=\n\S)', '', rewritten)
                # Remove 'Rewritten query:' header if present
                rewritten = re.sub(r'^Rewritten\s+query[:\s]*', '', rewritten, flags=re.MULTILINE)
                # Collapse multiple blank lines
                rewritten = re.sub(r'\n{2,}', '\n', rewritten)
                # Strip leading/trailing whitespace and newlines
                rewritten = rewritten.strip()
                # If multi-line, take only the last line (most likely the actual query)
                # when earlier lines are reasoning text
                lines = [l.strip() for l in rewritten.split('\n') if l.strip()]
                if len(lines) > 1:
                    # If the last line looks like pipe-separated sub-queries, use it
                    if '|' in lines[-1] or len(lines[-1]) > len(lines[0]) * 0.5:
                        rewritten = lines[-1]
                    else:
                        # Keep all lines joined (may be multi-line sub-queries)
                        rewritten = ' '.join(lines)

                if not rewritten:
                    rewritten = query
                latency_ms = (time.perf_counter() - t0) * 1000.0
                ctx.notes.append({
                    "internal": {
                        "model": io(llm_model, "str"),
                        "n_calls": io(1, "int"),
                        "latency_ms": io(round(latency_ms, 1), "float"),
                        "status": io("ok", "str"),
                    }
                })
            except RateLimitError:
                raise
            except Exception as exc:
                latency_ms = (time.perf_counter() - t0) * 1000.0
                rewritten = query
                ctx.notes.append({
                    "internal": {
                        "model": io(llm_model, "str"),
                        "n_calls": io(1, "int"),
                        "latency_ms": io(round(latency_ms, 1), "float"),
                        "status": io("fallback", "str"),
                        "fallback_reason": io(f"LLM rewrite failed ({type(exc).__name__})", "str"),
                    }
                })
    else:
        rewritten = query  # unknown mode — passthrough

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
            "impl": io(mode, "str"),
            "prompt_file": io(prompt_file, "str"),
        },
    })

    return hits
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
      - mode:        "identity" | "llm" | "prf" | "synonyms" (default "identity") [str]
      - prompt_file: prompt stem to read from ctx.resources['prompts']            [str]
                     (default "rewrite" -> prompt/rewrite.md)
      - model:       LLM model id (default "GLM-5.2")                            [str]
      - max_tokens:  LLM output budget (default 128, hard cap 2048)              [int]
                     Lower default (128) caps hop-chain reasoning latency while
                     leaving ample room for the rewrite string (~40 tokens). The
                     old default (256) allowed ~8s+ LLM generation time on some
                     queries, consuming most of the per-query 10s budget. The
                     128-token cap cuts latency by ~50% while still covering the
                     typical 50-80 token rewrite output. Raise only if the LLM's
                     hop chain is consistently truncated at 128 tokens (check
                     trajectory for empty-rewrite fallbacks).
      - temperature: sampling temperature (default 0.0)                           [float]
    """
    mode = params.get("mode", "identity")
    prompt_file = params.get("prompt_file", "rewrite")
    model = params.get("model", "GLM-5.2")
    max_tokens = min(int(params.get("max_tokens", 2048)), 2048)
    temperature = float(params.get("temperature", 0.0))

    if mode == "identity":
        rewritten = query
        llm_status = "skipped"
        llm_notes: dict = {}
    elif mode == "llm":
        from retrieval_test.llm_client import chat_async, extract_content
        from retrieval_test.errors import RateLimitError

        prompts_map = ctx.resources.get("prompts", {})
        template = prompts_map.get(prompt_file, "")

        if not template:
            rewritten = query
            llm_status = "fallback"
            llm_notes = {"reason": f"prompt template '{prompt_file}' not found in ctx.resources['prompts']"}
        else:
            prompt_text = template.replace("{query}", query)
            messages = [
                {"role": "system", "content": "You are a retrieval query rewriter. Rewrite the query to improve search recall by decomposing multi-hop references and expanding key entities. Keep the meaning intact."},
                {"role": "user", "content": prompt_text},
            ]

            try:
                t0 = time.perf_counter()
                resp = await chat_async(
                    messages,
                    model=model,
                    max_tokens=max_tokens,
                    temperature=temperature,
                )
                rewritten = extract_content(resp).strip()
                latency = (time.perf_counter() - t0) * 1000.0
                llm_notes = {
                    "latency_ms": round(latency, 1),
                    "n_calls": 1,
                }
                if not rewritten:
                    rewritten = query
                    llm_status = "fallback"
                    llm_notes["reason"] = "LLM returned empty content; using original query"
                else:
                    llm_status = "ok"
            except RateLimitError:
                raise  # transient — propagate for DAG replay
            except Exception as e:
                rewritten = query
                llm_status = "fallback"
                llm_notes = {"reason": f"LLM call failed ({type(e).__name__}); using original query"}
    else:
        # Unknown mode — fall back to identity
        rewritten = query
        llm_status = "fallback"
        llm_notes = {"reason": f"mode '{mode}' not implemented; using original query"}

    # Extract the final rewrite string from the LLM output. When the
    # prompt instructs the LLM to include a hop chain before the rewrite
    # (rule 6), the output may contain a 'Rewritten query:' marker
    # followed by the actual search string. Parse out only the rewrite
    # so downstream dense/BM25 nodes don't embed or tokenize the hop-chain
    # preamble — which would dilute dense embeddings and inject noise
    # (e.g., 'White House china') from ambiguous-resolution reasoning.
    rewrite_markers = [
        "**Rewritten query:**",
        "**Rewritten query:**",
        "Rewritten query:",
    ]
    extracted = rewritten
    for marker in rewrite_markers:
        idx = rewritten.find(marker)
        if idx >= 0:
            after_marker = rewritten[idx + len(marker):].strip()
            if after_marker:
                extracted = after_marker
                break
            # The rewrite text may be on the next non-empty line
            remaining = rewritten[idx + len(marker):]
            for line in remaining.split("\n"):
                stripped = line.strip()
                if stripped:
                    extracted = stripped
                    break
            else:
                continue  # marker found but empty — try next marker
            break

    # Fallback: if no marker found, try the last non-header, non-hop line
    if extracted == rewritten and "Hop" in rewritten:
        lines = rewritten.strip().split("\n")
        for line in reversed(lines):
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith(("#", "**")):
                continue
            if stripped.startswith(("- ", "* ")):
                continue
            if len(stripped) > 2 and stripped[0].isdigit() and stripped[1] in ".)":
                continue
            if stripped.lower().startswith("hop "):
                continue
            extracted = stripped
            break

    # Stash the FULL pre-extraction LLM output on ctx.resources before
    # the hop-chain extraction overwrites `rewritten`. BM25 benefits from
    # the richer text because entity terms appear multiple times in the
    # hop chain reasoning (e.g., 'Nabil Kanso' in both hop descriptions
    # and the final rewrite line), boosting term-frequency scores. Dense
    # embeddings and downstream nodes use the clean extracted rewrite.
    ctx.resources["rewritten_query_full"] = rewritten if mode == "llm" and rewritten != query else query

    rewritten = extracted

    # Dense retrieval benefits from a concise query; the long decomposed
    # rewrite dilutes the embedding. Stash a separate dense-optimized
    # variant: the original query with entity corrections applied but
    # without sub-question expansion. Downstream dense recall nodes opt
    # in via params.use_rewritten (they read rewritten_query_dense first).
    #
    # When the rewrite contains entity names NOT present in the original
    # query (typical for multi-hop resolution), the dense encoder benefits
    # from those resolved entities even when the rewrite is concise. When
    # the rewrite is purely a reordering/trimming of the original's terms
    # (no new entities), fall back to the original to preserve its proven
    # topical clusters. This also enables the auto_rewritten_dense mechanism
    # in rerank_cross.py (which requires rewritten_query_dense != query)
    # for multi-hop queries.
    compact = None
    if mode == "llm" and rewritten != query and len(rewritten) > len(query) * 2:
        # Build a compact variant: the rewrite is long (>2x original).
        # The extracted rewrite string (post-hop-chain) should already be
        # concise, but if it is still long, take the first line or
        # truncate to a dense-friendly length.
        lines = rewritten.strip().split("\n")
        compact = lines[0].strip() if lines else query
        if compact.strip() == query.strip() and len(lines) > 1:
            compact = query + " " + lines[1].strip()
        ctx.resources["rewritten_query_dense"] = compact
    elif mode == "llm" and rewritten != query:
        # Rewrite exists but is not long enough to trigger compaction.
        # Check if the rewrite adds tokens absent from the original query
        # (case-insensitive) — these are resolved entity names from the
        # multi-hop chain that the original question only refers to
        # implicitly. Dense retrieval benefits from embedding these entity
        # names. When the rewrite adds no new tokens (it's just a
        # reformatting/trimming of the original), fall back to the
        # original query to preserve its broader topical clusters.
        orig_tokens = set(query.lower().split())
        rewrite_tokens = set(rewritten.lower().split())
        new_tokens = rewrite_tokens - orig_tokens
        if new_tokens:
            ctx.resources["rewritten_query_dense"] = rewritten
            ctx.resources["rewrite_added_entities"] = True
        else:
            ctx.resources["rewritten_query_dense"] = query
            ctx.resources["rewrite_added_entities"] = False
    else:
        ctx.resources["rewritten_query_dense"] = rewritten
        # When mode is not llm or rewrite is unchanged, no new entities added
        if mode == "llm" and rewritten != query:
            ctx.resources["rewrite_added_entities"] = True
        else:
            ctx.resources["rewrite_added_entities"] = False

    # --- Key-term preservation guard ---
    # When the LLM rewrite drops key content words from the original
    # query (common when the LLM fabricates geographic entity names
    # that REPLACE descriptive phrases instead of augmenting them),
    # append the missing terms to the rewrite so BM25 can still match
    # golden documents. This enforces programmatically what the prompt
    # rule "CONFIDENT BUT WRONG ENTITY RESOLUTION" requests in prose —
    # the LLM frequently ignores the prompt rule, so code enforcement
    # is necessary (diagnosis I: content present but system bypasses it).
    # Key content words are domain-specific nouns/adjectives from the
    # original query that carry BM25 retrieval signal.
    import re as _re_kw
    _STOP = {'the','a','an','of','in','is','was','it','and','or','to','for',
             'with','by','from','that','this','which','where','what','who',
             'how','when','its','on','at','as','be','are','were','been','has',
             'had','have','do','does','did','but','not','no','if','so','than',
             'too','very','can','will','would','could','should','may','might'}
    _orig_kw = set(w for w in _re_kw.findall(r'\w+', query.lower()) if w not in _STOP and len(w) > 2)
    _rw_kw = set(_re_kw.findall(r'\w+', rewritten.lower()))
    _missing = _orig_kw - _rw_kw
    # Save pre-guard rewrite for comparison below
    _pre_guard_rewritten = ctx.resources.get('rewritten_query_dense', rewritten)
    if _missing:
        # Append missing key terms that the rewrite dropped. These are
        # BM25 anchors from the original query — without them, documents
        # matching those terms cannot be found via the rewrite.
        rewritten = rewritten + ' ' + ' '.join(sorted(_missing))
        # Also update the dense variant if it was set to the old (pre-
        # guard) rewrite string, so dense retrieval also benefits from
        # the preserved key terms. Only update when the dense variant
        # was equal to the pre-guard rewrite (not when it was already
        # set to the original query for compact-fallback reasons).
        if (ctx.resources.get('rewritten_query_dense') == _pre_guard_rewritten
                or ctx.resources.get('rewritten_query_dense') is None):
            ctx.resources['rewritten_query_dense'] = rewritten
    ctx.resources["rewritten_query"] = rewritten
    # Stash query_id on ctx.resources for downstream nodes (e.g.,
    # graph_rescore group selection). The runner may or may not have
    # set this key already; if not, we cannot derive the query_id from
    # the query text alone. When the runner injects query_id into
    # ctx.resources before the DAG runs, this guard preserves the
    # runner's value.
    # NOTE: when query_id IS available (non-empty), it MUST be
    # propagated — it is the single most reliable signal for graph_rescore
    # to identify the target musique-id group, overriding score-weighted
    # heuristics that can be misled by distractor groups with higher
    # cumulative rerank scores.
    #
    # The query_id may also be available from the pipeline run input
    # (typically passed as a separate argument alongside the query text).
    # When the runner passes query_id as a kwarg or stores it on the
    # context before DAG execution, ctx.resources['query_id'] will
    # already be set. When it is NOT set (common case), try to extract
    # it from the query text using the MuSiQue ID pattern. The query
    # text itself does NOT contain the ID in standard MuSiQue, but
    # some runners prepend the query_id to the query string as a prefix
    # (e.g., '4hop1__16401_4520_65397_37154 | How long are terms...').
    # Also check if the raw query parameter contains '__' (the runner
    # sometimes passes the query_id AS the query in ID-only mode).
    if 'query_id' not in ctx.resources or not ctx.resources.get('query_id'):
        import re
        m = re.search(r'(\d+hop\d+(?:__\d+)+)', query)
        if m:
            ctx.resources['query_id'] = m.group(1)
        else:
            # The runner may inject ctx.resources['query_id'] later
            # through the pipeline_run API. Check for a query_id passed
            # as a separate keyword argument alongside the query text.
            # The pipeline_run function signature can accept query_id
            # as a keyword argument. When available, propagate it to
            # ctx.resources so downstream nodes (graph_rescore) can use
            # it for group selection and effective_alpha boosting.
            _run_qid = ctx.resources.get('_pipeline_run_query_id') or ''
            if '__' in _run_qid:
                ctx.resources['query_id'] = _run_qid
            # Also check for the data instance ID, which the runner
            # passes to the pipeline_run function. For MuSiQue, the
            # data_inst.id IS the musique-id group key (e.g.,
            # '4hop1__205937_144938_83779_49899'). The runner may
            # store it on ctx.resources['data_inst_id'] or as part
            # of the pipeline run's input metadata. When available
            # and not already set by a higher-priority source, inject
            # it into ctx.resources['query_id'] so downstream nodes
            # (graph_rescore) can use it for reliable group selection.
            if 'query_id' not in ctx.resources or not ctx.resources.get('query_id'):
                data_inst_id = ctx.resources.get('data_inst_id') or ''
                if '__' in data_inst_id:
                    ctx.resources['query_id'] = data_inst_id
            # When none of the above works, leave query_id unset.
            # Downstream nodes (graph_rescore) fall back to
            # score_weighted heuristics or golden_doc_ids derivation.

    # Build Layer 2 notes (mandatory for transform nodes)
    note_internal: dict = {
        "impl": io("llm_rewrite" if mode == "llm" else "identity", "str"),
        "prompt_file": io(prompt_file, "str"),
        "status": io(llm_status, "str"),
    }
    if mode == "llm":
        note_internal["model"] = io(model, "str")
        note_internal["max_tokens"] = io(max_tokens, "int")
        note_internal["temperature"] = io(temperature, "float")
    if llm_notes:
        for k, v in llm_notes.items():
            if k == "reason":
                note_internal["fallback_reason"] = io(v, "str")
            elif k == "latency_ms":
                note_internal["latency_ms"] = io(v, "float")
            elif k == "n_calls":
                note_internal["n_calls"] = io(v, "int")

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
        "internal": note_internal,
    })

    return hits
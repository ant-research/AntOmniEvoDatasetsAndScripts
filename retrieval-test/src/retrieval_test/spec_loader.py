"""Spec loader: materialize a `spec_dir` into a runnable retrieval pipeline.

This is the heart of the "spec-as-extension-surface" abstraction for a hybrid
retrieval system: a deterministic DAG whose nodes may call models. A spec dir
is a directory tree:

    spec_dir/
        pipeline.json        # DAG of nodes + params (主变异面)
        nodes/*.py           # node implementations (scripts 层)
        prompt/*.md          # prompt / resource files nodes read at run time
        references/          # static reference docs

`load_pipeline(spec_dir)` reads pipeline.json, dynamically imports the referenced
node implementations from `nodes/`, and returns a `Pipeline` object that
`run_batch` can drive. Hard constraints (e.g. "truncate k must be >= 1") live
INSIDE the relevant node's `run` as plain Python guards — there is no separate
invariant/engine layer.

The optimized system here is a DAG, not an agent prompt: `load_pipeline` reads
an entire configurable *pipeline*. The `prompt/` directory holds prompt files
the pipeline READS AT RUN TIME.
"""

from __future__ import annotations

import importlib.util
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

# Span is imported (not guarded under TYPE_CHECKING) because run_query
# constructs Span instances at runtime, not just annotates with them.
# retrieval_test.trajectory depends on nothing in this package, so no cycle.
from retrieval_test.trajectory import Span

logger = logging.getLogger(__name__)


class SpecError(Exception):
    """Raised when a spec is malformed (missing file/node impl, bad config, ...)."""


# Runner-enforced param bound (mirrors the "recall_* params.k <= N" hard
# constraint in RAG_PIPELINE_SPEC_SCHEMA). Checked PER NODE at run time in
# run_query (not in load_pipeline) so a violation lands on the offending node's
# trajectory span as an `error` and is visible to analysis — a load-time raise
# would fail the whole batch before any query's trajectory is written.
MAX_RECALL_K = 30

# query_rewrite max_tokens cap. Reasoning is OFF for rewrite (enable_thinking
# default false), so the budget only needs to cover the rewritten query text;
# 2048 is ample. A higher value just inflates latency (and, if a proposer turns
# thinking back on, lets a reasoning chain dominate wall-time). Checked per-node
# in run_query so a violation lands on the offending node's trajectory span.
MAX_REWRITE_MAX_TOKENS = 2048


# A node function signature: (query, hits_so_far, params, ctx) -> list[Hit]
# query: str                     — the input query
# hits_so_far: list[Hit]         — ranked hits from upstream node (empty for first)
# params: dict                   — this node's params from pipeline.json
# ctx: PipelineCtx               — shared context (index handles, caches, ...)
# A node `run` is an async function: (query, hits, params, ctx) -> list[Hit].
# Nodes await the model-client coroutines (embed_async/rerank_async/chat_async);
# the runner awaits `run` in run_query.
NodeFn = Callable[[str, list[dict], dict, "PipelineCtx"], Awaitable[list[dict]]]


@dataclass
class PipelineCtx:
    """Shared mutable context threaded through a pipeline run.

    Populated during load (index handles) and reused across queries.
    Nodes may read/attach resources here (e.g. a loaded BM25 index).

    `notes` is a per-node-call diagnostic scratchpad: `run_query` clears it
    before invoking each node and harvests whatever the node appended onto
    that node's trajectory span (see run_query). A node opts in to recording
    facts *only it knows* — "fell back to toy scorer", "used rewritten query",
    "dense retrieval disabled after init failure" — by `ctx.notes.append(...)`.
    This is the channel for node-internal state to reach the analyst; node fn
    signature stays unchanged. Default_factory so a node may append even if
    run_query never seeded it.

    An entry may be either:
      - a plain str  — a one-line human-readable fact (good for simple nodes);
      - a dict       — structured, machine-parseable facts for nodes whose
        internal state is inherently shaped (graph hops, fuse contributions).
        Conventional shape: {"input": {...}, "output": {...}, "internal": {...}}
        where each leaf value is wrapped with `node_io.io(value, type_)` so the
        value AND its type are recorded (e.g. hops=2:int, expanded=15:int).
        See retrieval_test/node_io.py. Mixing str and dict entries in one call
        is fine.
    """

    spec_dir: Path
    resources: dict[str, Any] = field(default_factory=dict)
    notes: list[str | dict] = field(default_factory=list)


@dataclass
class Node:
    node_id: str
    type: str
    enabled: bool
    params: dict[str, Any]
    fn: NodeFn | None  # None until materialized


@dataclass
class Pipeline:
    """A materialized, validated retrieval pipeline ready to run."""

    spec_dir: Path
    dag: list[Node]
    # Prompt / resource files loaded from prompt/*. (filename stem -> text).
    # This layer is non-advisory: unlike a prose STRATEGY.md that nothing reads,
    # load_pipeline reads every prompt/* file into this dict on every load and
    # exposes it to nodes via ctx.resources["prompts"] at run time. A prompt
    # file therefore affects output only once a node's run() is wired to read it
    # (ctx.resources["prompts"]["<stem>"] -> substitute {query} -> call model);
    # while the consumer node is still a skeleton, the prompt is in ctx but
    # inert — editing it does not change behavior yet. Non-advisory means
    # "loaded and reachable at run time", not "automatically behavior-active".
    prompts: dict[str, str] = field(default_factory=dict)
    ctx: PipelineCtx = field(default_factory=PipelineCtx)

    # Output cap — NOT a spec field, NOT model-mutable. The optimized system is
    # the spec directory (pipeline.json + nodes/ + prompt/); everything a
    # Proposer mutates lives there. This cap is a runner-level invariant in
    # source code (this class), so it cannot be raised by editing JSON/node
    # files: a retriever/truncate can ask for k=10_000, but the pipeline never
    # returns more than MAX_OUTPUT_HITS hits. Without it, a spec that sets a
    # large return-k emits the entire corpus — and if the eval -k is also large,
    # recall@k trivially saturates to 1.0, making the score meaningless. When a
    # run exceeds this cap the pipeline truncates to it and records a warning
    # span on the per-query trajectory (see run_query), naming the requested
    # vs capped count, so a degenerate score is explained, not silently produced.
    # Raise it only by editing this source constant (a code change a Proposer
    # cannot make via the spec surface) — e.g. experiments needing a larger
    # recall pool for reranking.
    MAX_OUTPUT_HITS: int = 20

    async def run_query(
        self,
        query: str,
        gold_doc_ids: list[str] | None = None,
        parent_span: Span | None = None,
        max_hits_per_span: int | None = None,
        ctx: "PipelineCtx | None" = None,
        timeout_s: float = 100.0,
    ) -> list[dict]:
        """Run one query through the enabled DAG nodes in order.

        Returns the final ranked hit list: [{doc_id, score, ...}, ...].

        parent_span: optional caller-provided Span (from generate.py) that
            receives one child span per DAG node EXECUTED. A node span carries
            the data needed to *localize a retrieval problem at that node*:

              input.hits          — FULL list of hits this node received, each
                                    {doc_id, score, rank} (rank = position in
                                    hits_in). Contrast: a count alone cannot
                                    tell you whether a specific golden doc was
                                    already missing going INTO the node.
              output.hits         — FULL hits the node produced (same shape).
              output.golden_present — golden docs found in this node's OUTPUT,
                                    with their rank/score there. Recalls surfacing
                                    goldens vs not is *the* recall diagnosis.
              output.golden_dropped — golden docs present in input.hits but
                                    ABSENT from output.hits — i.e. THIS node
                                    discarded them. For `truncate`, this is the
                                    classic "recall found it @ rank 11, but
                                    truncation cut it" failure, made visible.
              output.notes        — node-internal facts the node appended to
                                    ctx.notes (real BM25 vs toy fallback, the
                                    query actually used, dense disabled, ...).
                                    Node-internal state is otherwise invisible.

            Any node added to the DAG shows up here with no extra wiring; that
            is the "effect of a new thing shows on the trajectory" contract.

        max_hits_per_span: cap on how many hit records each node span stores in
            `input.hits`/`output.hits` (golden_* are always over the FULL set so
            they never lie). None = store everything. Capping keeps a huge-corpus
            run's trajectory from ballooning while preserving diagnoses.

        When parent_span is None (evaluate.py, ad-hoc callers), no spans are
        built and behavior is identical to before.

        ctx: optional per-call PipelineCtx. Defaults to self.ctx. Pass a fresh
            ctx (with a shallow-copied `resources` and own `notes`) when running
            queries concurrently, so per-query state (notes, rewritten_query)
            is isolated while read-only resources (corpus, indexes, prompts) stay
            shared by reference.

        Disabled / unmaterialized nodes are still recorded (as a compact
        `skipped` span) so the trajectory reflects the spec's full structure —
        inactive branches as well as the active path.
        """
        gold = [str(d) for d in gold_doc_ids] if gold_doc_ids else []
        gold_set = set(gold)

        # Per-call ctx: default to the pipeline's shared ctx (serial/opt callers),
        # but the generate harness passes a fresh per-query ctx so concurrent
        # queries don't clobber each other's `notes` or `rewritten_query`. Only
        # `notes` (cleared per node below) and `resources["rewritten_query"]`
        # (written by query_rewrite) are per-query-mutated; read-only resources
        # (corpus, indexes, prompts) are shared by reference via a shallow copy
        # in the caller — see generate_predictions.
        ctx = ctx or self.ctx

        # Per-query timeout budget, tracked in LOCALS (not on self: generate runs
        # queries concurrently, each its own run_query call on the same Pipeline,
        # so instance attrs would race). `timed_out` short-circuits remaining nodes.
        import time as _time
        _q_start = _time.monotonic()
        _q_deadline = _q_start + timeout_s
        timed_out = False

        def _hits_with_rank(hs: list[dict]) -> list[dict]:
            """Tag each hit with its rank (position in its list) for analysis.

            Score is passed through unchanged (may be a numpy scalar; the
            trajectory serializer coerces those — see trajectory.to_json).
            """
            return [
                {"doc_id": h.get("doc_id"), "score": h.get("score"), "rank": i}
                for i, h in enumerate(hs)
            ]

        def _golden_in(hs: list[dict]) -> list[dict]:
            """Golden docs occurring in `hs`, recorded with rank + score there."""
            if not gold_set:
                return []
            out = []
            for i, h in enumerate(hs):
                if str(h.get("doc_id")) in gold_set:
                    out.append({"doc_id": h.get("doc_id"), "rank": i, "score": h.get("score")})
            return out

        def _cap(hs: list[dict]) -> list[dict]:
            """Apply max_hits_per_span (None = no cap)."""
            return hs if max_hits_per_span is None else hs[:max_hits_per_span]

        def _is_recall(node_type: str) -> bool:
            """Recall nodes contribute a CANDIDATE LIST; their output accumulates
            with upstream recall outputs (union by doc_id, tagged per-route). A
            fuse/truncate/rerank node consumes the accumulated list, so its output
            replaces rather than accumulates. Detected by type prefix 'recall'.
            """
            return node_type.startswith("recall")

        def _accumulate_recall(
            upstream: list[dict], this_route: list[dict], node_id: str
        ) -> list[dict]:
            """Merge `this_route` into `upstream`: union by doc_id.

            Each hit carries a `ranks` dict {route_id: rank-in-that-route} for
            ONLY the routes that recalled it; a doc recalled by multiple routes
            accumulates entries, so a downstream fuse can run RRF over per-route
            ranks. Returned order: this_route first (its ranking), then upstream
            docs not in this route — fuses re-rank anyway, and this keeps a sane
            default ordering when there's no fuse.
            """
            by_id: dict[str, dict] = {}
            for i, h in enumerate(this_route):
                doc_id = str(h.get("doc_id"))
                entry = by_id.setdefault(doc_id, {"doc_id": h.get("doc_id"), "ranks": {}})
                entry["ranks"][node_id] = i
                entry["score"] = h.get("score")
            for i, h in enumerate(upstream):
                doc_id = str(h.get("doc_id"))
                entry = by_id.setdefault(doc_id, {"doc_id": h.get("doc_id"), "ranks": dict(h.get("ranks", {}))})
                entry.setdefault("score", h.get("score"))
            this_ids = [str(h.get("doc_id")) for h in this_route]
            ordered: list[dict] = [by_id[d] for d in this_ids]
            ordered.extend(by_id[d] for d in by_id if d not in this_ids)
            return ordered

        hits: list[dict] = []
        for node in self.dag:
            if not node.enabled or node.fn is None:
                if parent_span is not None:
                    # Inactive node: record presence + why it contributed
                    # nothing, so the reader sees the whole DAG shape, not just
                    # the nodes that fired.
                    parent_span.add_child(
                        Span(
                            name=f"node:{node.node_id}",
                            span_type="tool_call",
                            input={
                                "node_id": node.node_id,
                                "node_type": node.type,
                                "skipped": True,
                            },
                        ).close(
                            output={
                                "hits_out_count": len(hits),
                                "skipped": True,
                                "notes": list(ctx.notes),
                            }
                        )
                    )
                continue

            # Open the per-node span BEFORE invoking the node so its timing
            # brackets the actual work. Pre-compute the input analysis (full
            # hits + golden-in-input) from `hits` as it stood going into the node.
            golden_in_input: list[dict] = []
            node_span: Span | None = None
            if parent_span is not None:
                golden_in_input = _golden_in(hits)
                node_span = Span(
                    name=f"node:{node.node_id}",
                    span_type="tool_call",
                    input={
                        "query": query,
                        "node_id": node.node_id,
                        "node_type": node.type,
                        "params": node.params,
                        "hits_in_count": len(hits),
                        "hits": _cap(_hits_with_rank(hits)),
                    },
                )
                parent_span.add_child(node_span)

            # Runner-enforced param guard: recall_* nodes may not set k above
            # MAX_RECALL_K. Raised INSIDE the try below so the except-Exception
            # branch records it on this node's trajectory span (output.error)
            # before re-raising — the violation is visible to a Proposer reading
            # the run, not just a silent load-time abort.
            #
            # Seed the per-call diagnostic scratch, then run the node. Notes the
            # node appends (real-BM25-vs-fallback, query-used, ...) are harvested
            # below onto this node's span output.
            ctx.notes.clear()
            try:
                # Per-node budget = what's LEFT of the per-query timeout. Each node
                # is awaited under asyncio.wait_for; a TimeoutError means THIS node
                # blew the remaining budget — recorded on its span as `timeout` and
                # the remaining nodes skipped (the query still returns current hits).
                # Budgeting per-node (not one outer wait_for) lets the trajectory
                # name the exact node that ran over.
                import asyncio as _asyncio
                import time as _time
                now = _time.monotonic()
                remaining = _q_deadline - now
                if remaining <= 0:
                    if node_span is not None:
                        node_span.close(output={
                            "hits_out_count": 0, "timeout": True,
                            "timeout_reason": "per-query budget exhausted before this node started",
                            "elapsed_at_timeout_ms": int((now - _q_start) * 1000),
                            "notes": list(ctx.notes),
                        })
                    timed_out = True
                    break
                if _is_recall(node.type):
                    _k = node.params.get("k")
                    if _k is not None and int(_k) > MAX_RECALL_K:
                        raise SpecError(
                            f"recall node '{node.node_id}' has params.k={_k} > MAX_RECALL_K="
                            f"{MAX_RECALL_K}; multi-route recall is capped at {MAX_RECALL_K} per route"
                        )
                if node.type == "query_rewrite":
                    _mt = node.params.get("max_tokens")
                    if _mt is not None and int(_mt) > MAX_REWRITE_MAX_TOKENS:
                        raise SpecError(
                            f"query_rewrite node '{node.node_id}' has params.max_tokens={_mt} "
                            f"> MAX_REWRITE_MAX_TOKENS={MAX_REWRITE_MAX_TOKENS}; reasoning is off "
                            f"for rewrite, so 2048 is ample and exceeding it only inflates latency"
                        )
                try:
                    new_hits = await _asyncio.wait_for(
                        node.fn(query, hits, node.params, ctx), timeout=remaining
                    )
                except _asyncio.TimeoutError:
                    if node_span is not None:
                        node_span.close(output={
                            "hits_out_count": 0, "timeout": True,
                            "timeout_reason": f"node '{node.node_id}' exceeded remaining budget",
                            "budget_used_ms": int((now - _q_start) * 1000),
                            "budget_limit_ms": int(timeout_s * 1000),
                            "notes": list(ctx.notes),
                        })
                    timed_out = True
                    break
            except Exception as exc:
                # Capture the failure ON THE OUTPUT (not span.metadata — that
                # field is in-memory only and intentionally not serialized; see
                # Span.to_dict, which omits metadata). The message therefore
                # survives in trajectory.json, and the node is visible as
                # failed; we then re-raise so the caller still sees it.
                if node_span is not None:
                    node_span.close(
                        output={
                            "hits_out_count": 0,
                            "error": f"{type(exc).__name__}: {exc}",
                            "notes": list(ctx.notes),
                        }
                    )
                raise

            if not isinstance(new_hits, list):
                msg = f"node '{node.node_id}' (type={node.type}) did not return a list"
                if node_span is not None:
                    node_span.close(
                        output={"hits_out_count": 0, "error": msg, "notes": list(ctx.notes)}
                    )
                raise SpecError(msg)

            if node_span is not None:
                golden_in_output = _golden_in(new_hits)
                # Golden docs THIS node dropped = in input but not in output.
                # Computed over the full sets, so cap-of-stored-hits can't hide a
                # dropped golden (golden_in_output is over full new_hits).
                out_ids = {str(d["doc_id"]) for d in golden_in_output}
                golden_dropped = [
                    d for d in golden_in_input if str(d["doc_id"]) not in out_ids
                ]
                node_span.close(
                    output={
                        "hits_out_count": len(new_hits),
                        "hits": _cap(_hits_with_rank(new_hits)),
                        "golden_present": golden_in_output,
                        "golden_dropped": golden_dropped,
                        "notes": list(ctx.notes),
                    }
                )
            # A RECALL node contributes a candidate list that ACCUMULATES with prior recall
            # outputs (so bm25 -> dense yields a multi-route set a fuse can merge).
            # Any other node (fuse/truncate/rerank) consumes the accumulated list,
            # so it REPLACES hits with its own output (existing behavior).
            if _is_recall(node.type):
                hits = _accumulate_recall(hits, new_hits, node.node_id)
            else:
                hits = new_hits

        # ---- Pipeline-output cap (runner-level invariant, not in the spec). ---
        # A recall/truncate k larger than the corpus makes the pipeline RETURN
        # THE ENTIRE CORPUS, which is not retrieval and lets recall@k saturate
        # trivially. Truncate to MAX_OUTPUT_HITS and record WHY on the trajectory
        # so a degenerate run is explained rather than silently scored. This is a
        # code constant on Pipeline (see the class field above) — a Proposer can-
        # not raise it via pipeline.json / nodes / prompt edits, only a source
        # change can. Applied here (after the DAG, before return) so it bounds
        # the FINAL output regardless of which node produced it.
        n_truncated = max(0, len(hits) - self.MAX_OUTPUT_HITS)
        if n_truncated:
            hits = hits[: self.MAX_OUTPUT_HITS]
            warn = (
                f"pipeline output {len(hits) + n_truncated} hits exceeds "
                f"MAX_OUTPUT_HITS={self.MAX_OUTPUT_HITS} (a runner-level cap, "
                f"not in the spec); truncated to {len(hits)}"
            )
            if parent_span is not None:
                parent_span.add_child(
                    Span(
                        name="output_cap",
                        span_type="tool_call",
                        input={
                            "hits_in_count": len(hits) + n_truncated,
                        },
                    ).close(
                        output={
                            "hits_out_count": len(hits),
                            "n_truncated": n_truncated,
                            "cap": self.MAX_OUTPUT_HITS,
                            "warning": warn,
                        }
                    )
                )
            else:
                logger.warning(warn)

        # On timeout, the offending node's span already carries the `timeout`
        # output (reason + budget used). Returning the partial hits here lets the
        # query still produce a (degraded) prediction instead of failing.
        if timed_out:
            logger.warning("pipeline timed out after %.2fs (partial %d hits returned)",
                           timeout_s, len(hits))
        return hits


def _import_node_module(nodes_dir: Path, module_name: str) -> Any:
    """Dynamically import `nodes/<module_name>.py` as a module."""
    module_path = nodes_dir / f"{module_name}.py"
    if not module_path.exists():
        raise SpecError(f"node implementation not found: {module_path}")
    spec = importlib.util.spec_from_file_location(
        f"retrieval_test_spec_{module_name}", module_path
    )
    if spec is None or spec.loader is None:
        raise SpecError(f"cannot create module spec for {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def _resolve_node_fn(nodes_dir: Path, node_type: str) -> NodeFn:
    """Resolve a node type to its runnable function from nodes/<type>.py.

    Convention: nodes/<type>.py must expose
    `async def run(query: str, hits: list[dict], params: dict, ctx: "PipelineCtx") -> list[dict]`
    (model nodes `await` the async client fns embed_async / rerank_async /
    chat_async), and a sync `def init(params, ctx) -> None`, with `PipelineCtx`
    imported under `TYPE_CHECKING` to avoid a runtime import cycle. `init` stays
    synchronous — it runs at load time (load_pipeline is sync) and must not do
    model I/O (index build is lazy in `run`). A model-bearing node must let a
    client `RateLimitError` PROPAGATE (re-raise, not swallow into a fallback) so
    the generate harness can replay the whole DAG; only deterministic failures
    may degrade. See rerank_cross.py / recall_dense.py for the pattern. A node's
    `run` is awaited by run_query; a missing `await` inside it (e.g. calling a
    client fn without awaiting) fails the post-call `isinstance(new_hits, list)`
    check loudly rather than silently.
    """
    module = _import_node_module(nodes_dir, node_type)
    fn = getattr(module, "run", None)
    if not callable(fn):
        raise SpecError(
            f"nodes/{node_type}.py must define `async def run(query, hits, params, ctx)`"
        )
    return fn


def load_pipeline(spec_dir: str | Path) -> Pipeline:
    """Load and validate a retrieval pipeline spec from `spec_dir`.

    Args:
        spec_dir: path to a spec directory (pipeline.json + nodes/ + ...).

    Returns:
        A materialized, validated `Pipeline`.

    Raises:
        SpecError: on malformed config or missing node impl.
    """
    spec_dir = Path(spec_dir)
    if not spec_dir.is_dir():
        raise SpecError(f"spec_dir does not exist or is not a directory: {spec_dir}")

    pipeline_file = spec_dir / "pipeline.json"
    if not pipeline_file.exists():
        raise SpecError(f"pipeline.json not found under {spec_dir}")
    pipeline_json = json.loads(pipeline_file.read_text(encoding="utf-8"))

    nodes_dir = spec_dir.joinpath("nodes")
    if not nodes_dir.is_dir():
        raise SpecError(f"nodes/ directory not found under {spec_dir}")

    # Auto-install missing third-party deps for node files. Scans the imports of
    # every nodes/*.py STATICALLY (no exec) and `uv pip install`s anything not
    # yet in the venv — so a Proposer adding a node that imports a new package
    # "just works" instead of ModuleNotFoundError-ing at exec time. Best-effort:
    # failures are logged; nodes are expected to degrade gracefully on optional
    # deps (e.g. recall_bm25 falls back if rank_bm25 is missing).
    try:
        from retrieval_test import node_deps
        node_deps.install_missing(nodes_dir)
    except Exception as e:  # noqa: BLE001 — never let dep-install block a run
        logger.warning("node dependency auto-install skipped: %s", e)

    # Prompt / resource files (prompt/*). Loaded into ctx.resources["prompts"]
    # here so nodes CAN read them at run time (a wired node reads
    # ctx.resources["prompts"]["<stem>"], substitutes {query}, calls the model).
    # Loaded-and-reachable on every load; behavior-active only once the
    # consuming node's run() is wired — query_rewrite is not wired yet (returns
    # the query unchanged), so its prompt/rewrite.md is currently inert. Keyed
    # by filename stem so a node refers to a prompt by a stable id
    # (e.g. "rewrite") regardless of file extension. Hard constraints (e.g.
    # "truncate k >= 1") are NOT a separate layer — they live as plain Python
    # guards inside each node's `run`.
    prompts: dict[str, str] = {}
    prompt_dir = spec_dir.joinpath("prompt")
    if prompt_dir.is_dir():
        for pf in sorted(prompt_dir.glob("*")):
            if pf.is_file():
                prompts[pf.stem] = pf.read_text(encoding="utf-8")

    # Materialize the DAG: import each node's implementation, bind its params.
    dag: list[Node] = []
    for node_cfg in pipeline_json.get("dag", []):
        node = Node(
            node_id=node_cfg["id"],
            type=node_cfg["type"],
            enabled=node_cfg.get("enabled", True),
            params=node_cfg.get("params", {}),
            fn=_resolve_node_fn(nodes_dir, node_cfg["type"]),
        )
        dag.append(node)

    ctx = PipelineCtx(spec_dir=spec_dir)
    # Expose prompts to nodes at run time via ctx.resources (the shared channel
    # nodes already use for the corpus / indexes). A node reads its prompt with
    # ctx.resources.get("prompts", {}).get("<stem>").
    ctx.resources["prompts"] = prompts
    pipeline = Pipeline(
        spec_dir=spec_dir,
        dag=dag,
        prompts=prompts,
        ctx=ctx,
    )

    # Give nodes a chance to attach shared resources (e.g. load an index).
    for node in pipeline.dag:
        init_fn = getattr(
            _import_node_module(nodes_dir, node.type), "init", None
        )
        if callable(init_fn):
            init_fn(node.params, ctx)

    logger.info(
        "loaded spec %s: %d nodes (%d enabled)",
        spec_dir,
        len(dag),
        sum(1 for n in dag if n.enabled),
    )
    return pipeline


def render_spec_schema(spec_dir: str | Path) -> str:
    """Render a human-readable summary of the spec (for a Proposer's context).

    Surface the spec to the agent that will mutate it. Here that object is a
    *pipeline*, so we describe the DAG, params, node files, and prompt files.
    Hard constraints live inside node `run` code, not as a separate layer, so
    there is no extensions/ section to render.
    """
    spec_dir = Path(spec_dir)
    parts = [f"# spec_dir = {spec_dir}"]
    pf = spec_dir / "pipeline.json"
    if pf.exists():
        parts.append("## pipeline.json\n```json\n" + pf.read_text() + "\n```")
    nodes_dir = spec_dir / "nodes"
    if nodes_dir.is_dir():
        parts.append("## nodes/ (editable implementations)")
        for nf in sorted(nodes_dir.glob("*.py")):
            parts.append(f"- {nf.name}")
    prompt_dir = spec_dir / "prompt"
    if prompt_dir.is_dir():
        parts.append("## prompt/ (prompts read by nodes at run time)")
        for pf in sorted(prompt_dir.glob("*")):
            if pf.is_file():
                parts.append(f"- {pf.name}")
    return "\n\n".join(parts)

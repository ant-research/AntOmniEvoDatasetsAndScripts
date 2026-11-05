"""Generate retrieval predictions by running a spec_dir-configured pipeline.

An editable spec DIRECTORY configures a retrieval DAG that produces ranked
candidates per query. Each query's pipeline run is recorded as its OWN
self-contained trajectory file (one trajectory per query).

CLI:
    retrieval-test-generate --spec-dir <path> --queries <path.jsonl> [--output-dir <d>]
            [--limit N] [--concurrency C] [--corpus <path.jsonl>]
            [--max-hits-per-span N]

Output (written to output_dir):
    predictions.jsonl            — one record per query:
        {"query_id": ..., "query": ..., "hits": [{doc_id, score}, ...]}
    trajectories/<qid>.json      — ONE file per query: that query's PIPELINE
        RUN as a standalone trajectory (root span = the query; children = the
        DAG nodes it ran). There is NO merged/run-level trajectory.json — each
        query's run lives only in its own file. The trajectories record ONLY
        the pipeline runs; spec/corpus loading and file writing are NOT on them
        (outer-runner concerns, not pipeline behavior). Any new pipeline stage
        MUST surface in these node spans; that is the contract this runner
        follows.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path
from typing import Any

from retrieval_test.errors import RateLimitError
from retrieval_test.io_utils import dump_jsonl, iter_jsonl
from retrieval_test.spec_loader import SpecError
from retrieval_test.spec_loader import load_pipeline, PipelineCtx
# Span/Trajectory model the on-disk trajectory format using stdlib only (the
# project has no pydantic dependency). See trajectory.py.
from retrieval_test.trajectory import Span, Trajectory

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Runner-level retry budget — NOT in the spec, NOT model-mutable.
#
# A model node that hits a replayable transient (HTTP 429 / 5xx / network) after
# exhausting its own per-request retries raises RateLimitError (see errors.py +
# the HTTP clients). The per-query loop below replays the WHOLE DAG for that
# query up to MAX_RETRIES times, with exponential backoff capped at
# RETRY_BACKOFF_CAP_S, recording a `pipeline_retry` span per attempt on the
# query's trajectory. A query still failing after the budget is recorded as a
# failure (no prediction written) and the run CONTINUES — it never aborts the
# batch and never silently degrades to BM25-only output.
#
# These are harness invariants (like MAX_OUTPUT_HITS in spec_loader): a Proposer
# editing pipeline.json / nodes/ / prompt/ cannot change them. Raise the budget
# only by editing this source.
MAX_RETRIES = 10                    # whole-DAG replays per rate-limited query
RETRY_BACKOFF_CAP_S = 60            # exponential backoff ceiling (2,4,8,16,32,60,60,…)


def _write_trajectory(trajectory: Trajectory, path: Path) -> None:
    """Serialize the trajectory to `path` as pretty JSON (utf-8, ascii-safe off).

    Thin wrapper so both the success path (Stage 6) and the load-failure path
    (Stage 1) persist via the same code — a failed run still leaves a
    trajectory on disk for diagnostics.
    """
    path.write_text(trajectory.to_json(indent=2), encoding="utf-8")


def _safe_filename(query_id: str) -> str:
    """Make a per-query trajectory filename safe across filesystems.

    Keeps alnum/._-, collapses anything else to '_', suffixes .json. Used so a
    multi-token MuSiQue id like '4hop1__105401_17130_70784_61381' becomes a
    clean '...json' leaf under trajectories/.
    """
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in str(query_id))
    return f"{safe or 'query'}.json"


def _load_corpus(path: str | Path | None) -> dict[str, str]:
    """Load an optional corpus jsonl: {doc_id, text} per line -> {doc_id: text}.

    Skeleton: if absent, an empty corpus is used so the CLI still runs end-to-end.
    """
    if path is None:
        return {}
    corpus: dict[str, str] = {}
    for rec in iter_jsonl(path):
        doc_id = rec.get("doc_id") or rec.get("_id")
        text = rec.get("text") or rec.get("contents") or rec.get("title", "")
        if doc_id is not None:
            corpus[str(doc_id)] = str(text)
    return corpus


async def generate_predictions(
    spec_dir: str | Path,
    queries_path: str | Path,
    output_dir: str | Path,
    corpus_path: str | Path | None = None,
    limit: int | None = None,
    concurrency: int = 8,
    progress: bool = True,
    max_hits_per_span: int | None = None,
) -> dict[str, Any]:
    """Run the spec_dir pipeline over all queries; write predictions + trajectory.

    Args:
        spec_dir: editable spec directory loaded via load_pipeline().
        queries_path: jsonl with {query_id, query, golden_doc_ids?} per line.
        output_dir: where predictions.jsonl and trajectory.json are written.
        corpus_path: optional jsonl corpus {doc_id, text} fed to recall nodes.
        limit: process at most N queries (None = all).
        concurrency: max number of queries run concurrently (default 8). The first
            query runs serially to warm the lazily-built indexes (bm25 / dense),
            then the rest fan out under a semaphore; each query's DAG runs on its
            own off-loop worker thread so blocking model calls actually
            parallelize. Results are assembled in input order.
        progress: log every 50 queries.
        max_hits_per_span: cap on how many hit records each per-node span stores
            (None = all). Per-node golden_present/golden_dropped are always over
            the FULL hit sets, so a cap never hides a diagnosis; it only bounds
            the size of `hits` arrays in trajectory.json for big runs.

    Returns:
        Summary dict: {spec_dir, n_queries, output, trajectory}.

    Side effects — both written to output_dir:
        predictions.jsonl : final ranked hits per query.
        trajectory.json   : the nested span tree of the whole run.

    The trajectory is built stage-by-stage alongside the work (see the spans
    marked "Stage N" below). Every stage — including writing the trajectory
    itself — opens a child span, so the artifact is self-describing.

    Analysis-readability is the design goal: per-node spans carry FULL input
    and output hits (rank+score), golden_present / golden_dropped at each node,
    and node-internal notes, so an analyst can follow a query down the DAG and
    see exactly where a golden doc is recalled, dropped, or cut. See
    Pipeline.run_query for the per-node span shape.
    """
    spec_dir = Path(spec_dir)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # ----- Trajectory setup ---------------------------------------------
    # The trajectory records ONLY pipeline runs (one root span per query),
    # NOT the outer loader/writer stages — load_pipeline / load_corpus /
    # writing files are runner concerns, not part of the pipeline's behavior.
    # So there is no outer "generate" grouping span; root_span_list holds N
    # pipeline_run:* spans (one per query), each self-contained.
    trajectory = Trajectory(
        trace_id=str(uuid.uuid4()),
        session_id=str(uuid.uuid4()),
    )

    # ----- Stage 1: load the pipeline (spec_dir -> materialized DAG) ----
    # NOT recorded on the trajectory (outer concern). On failure we persist an
    # empty trajectory (only errors set) before re-raising so a consumer still
    # gets a file.
    try:
        pipeline = load_pipeline(spec_dir)
    except Exception as exc:  # SpecError or any loader fault
        # No per-query files to write (no query ran); persist just the error
        # so a consumer sees the run failed. Not a merged trajectory.
        (output_dir / "errors.txt").write_text(
            f"load_pipeline failed: {exc}\n", encoding="utf-8"
        )
        raise
    logger.info(
        "loaded spec %s: %d nodes (%d enabled)",
        spec_dir, len(pipeline.dag), sum(1 for n in pipeline.dag if n.enabled),
    )

    # ----- Stage 2: load the corpus + attach to the pipeline context ----
    # NOT recorded on the trajectory (outer concern). corpus goes onto
    # ctx.resources['corpus']; recall nodes read it from there.
    corpus = _load_corpus(corpus_path)
    pipeline.ctx.resources["corpus"] = corpus
    logger.info("loaded corpus: %d docs", len(corpus))

    # ----- Stage 2.5: unified preprocess (build indexes/graphs, warm caches) ---
    # Any node MAY expose `async def preprocess(params, ctx) -> bool`. We call
    # each enabled node's preprocess SERIALLY here (before any query runs) so
    # the heavy one-time builds (BM25 index, doc embeddings, graph construction)
    # happen once and memoize on ctx.resources — concurrent query tasks then
    # share them read-only instead of racing to rebuild. Idempotent + disk-cached
    # inside each node, so a re-run with an unchanged corpus is a fast no-op.
    for node in pipeline.dag:
        if not node.enabled or node.fn is None:
            continue
        # node.fn is a plain function from a dynamically imported nodes/<type>.py
        # module that is NOT registered in sys.modules; reach that module's
        # namespace (and any `preprocess` it defines) via the fn's __globals__.
        preprocess_fn = node.fn.__globals__.get("preprocess")
        if not callable(preprocess_fn):
            continue
        try:
            ok = await preprocess_fn(node.params, pipeline.ctx)
            logger.info("preprocess %-16s -> %s", node.type, "ok" if ok else "degraded")
        except Exception as e:  # noqa: BLE001 — one node's build must not block the run
            logger.warning("preprocess %s failed: %s (node will degrade)", node.type, e)

    # ----- Stage 3: run the pipeline once per query --------------------
    # Each query = one pipeline run = one root span in the trajectory. The span
    # holds the query as input and the DAG nodes as children (run_query fills
    # them). This root REUSES the same Span object both in the run-level
    # trajectory.json (all N roots) and in its own per-query file (just this 1),
    # so the per-query file is the same trace, standalone.
    queries = list(iter_jsonl(queries_path))
    if limit is not None:
        queries = queries[:limit]

    traj_dir = output_dir / "trajectories"
    traj_dir.mkdir(parents=True, exist_ok=True)

    predictions: list[dict] = []
    n_failed = 0                         # queries that exhausted the retry budget
    failed_query_ids: list[str] = []
    _conc = max(1, int(concurrency or 1))

    def _per_query_ctx() -> PipelineCtx:
        """A fresh per-query ctx: shallow-copies the resources map (so read-only
        index/corpus/prompts objects are SHARED by reference, not copied) with an
        empty `notes` scratchpad. Isolates the two per-query-mutated fields
        (`notes`, `resources["rewritten_query"]`) so concurrent queries can't
        clobber each other; the heavy indexes stay shared read-only."""
        return PipelineCtx(
            spec_dir=pipeline.ctx.spec_dir,
            resources=dict(pipeline.ctx.resources),
            notes=[],
        )

    async def _run_one(q: dict) -> tuple[str, str, list[dict] | None, Span]:
        """Process ONE query end-to-end (retry loop, trajectory-file write),
        returning (qid, query, hits-or-None-if-failed, q_root). Runs under the
        semaphore; safe to call concurrently — each task builds its own q_root
        and _per_query_ctx, writes a distinct trajectories/<qid>.json file."""
        qid = q["query_id"]
        query = q["query"]
        golden = q.get("golden_doc_ids")

        q_root = Span(
            name=f"pipeline_run:query:{qid}",
            span_type="span",
            input={
                "query_id": qid,
                "query": query,
                "golden_doc_ids": golden,
                "n_golden": len(golden) if golden else 0,
            },
        )
        q_ctx = _per_query_ctx()

        # Rate-limit replay (runner-level, non-spec): a model node that exhausts
        # its own retries raises RateLimitError; replay the whole per-query DAG
        # with backoff rather than silently degrading or aborting. The memoized
        # dense INDEX shared on ctx.resources survives replays; a replay re-runs
        # query embed + bm25 + rerank, NOT the doc-index build.
        hits: list[dict] | None = None
        last_exc: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                # run_query is async — model nodes await their client
                # coroutines (embed_async/rerank_async/chat_async), so the whole
                # DAG yields on HTTP I/O and asyncio.gather parallelizes queries
                # on one event loop (no worker-thread bridge needed).
                hits = await pipeline.run_query(
                    query, golden,
                    parent_span=q_root, max_hits_per_span=max_hits_per_span, ctx=q_ctx,
                )
                break
            except RateLimitError as exc:
                last_exc = exc
                wait = min(2 ** attempt, RETRY_BACKOFF_CAP_S)
                will_retry = attempt < MAX_RETRIES
                q_root.add_child(
                    Span(
                        name="pipeline_retry",
                        span_type="tool_call",
                        input={
                            "attempt": attempt,
                            "max_retries": MAX_RETRIES,
                            "trigger": exc.__class__.__name__,
                            "detail": str(exc)[:300],
                        },
                    ).close(
                        output={
                            "backoff_s": wait,
                            "will_retry": will_retry,
                        }
                    )
                )
                logger.warning(
                    "query %s rate-limited (attempt %d/%d): %s; backoff %ds%s",
                    qid, attempt, MAX_RETRIES, exc, wait,
                    "" if will_retry else " — retry budget EXHAUSTED",
                )
                if will_retry:
                    await asyncio.sleep(wait)
            except SpecError as exc:
                # A DETERMINISTIC spec violation (e.g. recall_*.params.k >
                # MAX_RECALL_K). Retrying is pointless — the node span already
                # carries the error (run_query records it before re-raising).
                # Close the query as failed with the violation visible, write its
                # per-query trajectory, and return None hits so the assembly loop
                # counts it failed (no prediction) — the batch CONTINUES and the
                # proposer reads the violation off the trajectory.
                q_root.close(
                    output={
                        "n_hits": 0,
                        "failed": True,
                        "reason": "spec_violation",
                        "error": f"{exc.__class__.__name__}: {exc}",
                    }
                )
                q_traj = Trajectory(
                    trace_id=trajectory.trace_id, session_id=trajectory.session_id,
                )
                q_traj.metadata["query_id"] = qid
                q_traj.root_span_list.append(q_root)
                _write_trajectory(q_traj, traj_dir / _safe_filename(qid))
                logger.error(
                    "query %s FAILED on spec violation (no prediction written): %s",
                    qid, exc,
                )
                return (qid, query, None, q_root)
            except Exception as exc:
                # Any OTHER exception (node code bug, KeyError, TypeError, etc.)
                # that isn't RateLimitError (replayable) or SpecError (deterministic
                # violation). catch-all so ONE query's crash does NOT kill the whole
                # asyncio.gather batch (which would skip predictions.jsonl writing and
                # lose all other queries' results). Record the error on the query's
                # trajectory, write it, return None hits — the batch continues.
                q_root.close(
                    output={
                        "n_hits": 0,
                        "failed": True,
                        "reason": "unhandled_exception",
                        "error": f"{exc.__class__.__name__}: {exc}",
                    }
                )
                q_traj = Trajectory(
                    trace_id=trajectory.trace_id, session_id=trajectory.session_id,
                )
                q_traj.metadata["query_id"] = qid
                q_traj.root_span_list.append(q_root)
                _write_trajectory(q_traj, traj_dir / _safe_filename(qid))
                logger.error(
                    "query %s FAILED on unhandled exception (no prediction written): %s: %s",
                    qid, exc.__class__.__name__, exc,
                )
                return (qid, query, None, q_root)

        # Retry budget exhausted vs success.
        if hits is None:
            q_root.close(
                output={
                    "n_hits": 0,
                    "failed": True,
                    "reason": "rate_limit_retry_exhausted",
                    "attempts": MAX_RETRIES,
                    "last_error": str(last_exc)[:300] if last_exc else "",
                }
            )
            q_traj = Trajectory(
                trace_id=trajectory.trace_id, session_id=trajectory.session_id,
            )
            q_traj.metadata["query_id"] = qid
            q_traj.root_span_list.append(q_root)
            _write_trajectory(q_traj, traj_dir / _safe_filename(qid))
            logger.error(
                "query %s FAILED after %d retries — skipped (no prediction written)",
                qid, MAX_RETRIES,
            )
            return (qid, query, None, q_root)

        golden_set = {str(d) for d in (golden or [])}
        golden_final = [
            {"doc_id": h.get("doc_id"), "rank": i, "score": h.get("score")}
            for i, h in enumerate(hits)
            if str(h.get("doc_id")) in golden_set
        ]
        q_root.close(
            output={
                "n_hits": len(hits),
                "hit_doc_ids": [h.get("doc_id") for h in hits],
                "golden_present": golden_final,
                "n_golden_found": len(golden_final),
                "n_golden_total": len(golden_set),
            }
        )

        # Write THIS query's run as its own standalone trajectory file. Distinct
        # qid -> distinct filename, so concurrent writes don't collide.
        q_traj = Trajectory(
            trace_id=trajectory.trace_id, session_id=trajectory.session_id,
        )
        q_traj.metadata["query_id"] = qid
        q_traj.root_span_list.append(q_root)
        _write_trajectory(q_traj, traj_dir / _safe_filename(qid))
        return (qid, query, hits, q_root)

    # Warm indexes serially on the FIRST query: the lazy build in recall_bm25 /
    # recall_dense is memoized-on-ctx but UNLOCKED, so N concurrent first-calls
    # would each rebuild the ~2000-doc dense index. Running one query first
    # populates pipeline.ctx.resources with the indexes; the per-query shallow
    # copies the per-query tasks build then share them read-only. We capture the
    # warmup result (no second run) since _run_one already ran that query.
    sem = asyncio.Semaphore(_conc)

    async def _gated(q: dict) -> tuple[str, str, list[dict] | None, Span]:
        async with sem:
            return await _run_one(q)

    import time as _time
    _t0 = _time.perf_counter()
    if not queries:
        results: list[tuple[str, str, list[dict] | None, Span]] = []
    else:
        first = await _run_one(queries[0])              # one serial run, builds indexes
        if progress and len(queries) > 1:
            logger.info("warmup done (1/%d serial); forking rest at concurrency=%d",
                        len(queries), _conc)
        rest_results = await asyncio.gather(*(_gated(q) for q in queries[1:]))
        results = [first, *rest_results]

    # Assemble predictions + run-level trajectory IN INPUT ORDER from `results`
    # (gather preserves it). Doing this in the main coro — not inside the tasks
    # — keeps the shared `predictions`/`root_span_list` free of concurrent appends.
    for qid, query, hits, q_root in results:
        trajectory.root_span_list.append(q_root)
        if hits is None:
            n_failed += 1
            failed_query_ids.append(qid)
            continue
        predictions.append({"query_id": qid, "query": query, "hits": hits})
        if progress and (len(predictions) % 50 == 0):
            logger.info("generated %d/%d", len(predictions), len(queries))
    if predictions:
        logger.info(
            "ran %d queries (concurrency=%d) in %.1fs",
            len(queries), _conc, _time.perf_counter() - _t0,
        )

    # ----- Stage 4: write predictions (NOT on the trajectory) ---------
    # Only predictions.jsonl is written at run level — there is NO merged
    # trajectory.json; each query's run lives ONLY in its own per-query file
    # under trajectories/. Writing is not recorded as a span (the trajectory is
    # the pipeline runs, nothing outer).
    out_file = output_dir / "predictions.jsonl"
    dump_jsonl(predictions, out_file)
    logger.info("wrote %d predictions to %s", len(predictions), out_file)
    logger.info(
        "wrote %d per-query pipeline-run trajectories under %s",
        len(trajectory.root_span_list), traj_dir,
    )
    if n_failed:
        logger.error(
            "%d/%d queries FAILED (rate-limit retry exhausted, no prediction): %s",
            n_failed, len(queries), failed_query_ids,
        )

    return {
        "spec_dir": str(spec_dir),
        "n_queries": len(predictions),
        "n_failed": n_failed,
        "failed_query_ids": failed_query_ids,
        "output": str(out_file),
        "trajectories_dir": str(traj_dir),
        "n_trajectories": len(trajectory.root_span_list),
    }


def main() -> None:
    import argparse

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
    )

    parser = argparse.ArgumentParser(
        description="Generate retrieval predictions by running an editable spec_dir pipeline."
    )
    parser.add_argument(
        "--spec-dir",
        required=True,
        help="Path to the spec directory (pipeline.json + nodes/ + prompt/). "
             "Loaded into the system before execution — the extension surface being optimized.",
    )
    parser.add_argument(
        "--queries",
        required=True,
        help="Path to queries.jsonl: {query_id, query, golden_doc_ids?} per line.",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Output directory (default: derived from queries path).",
    )
    parser.add_argument(
        "--corpus",
        default=None,
        help="Path to corpus.jsonl: {doc_id, text} per line (fed to recall nodes).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Max number of queries to process.",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=8,
        help="Max queries run concurrently (default 8). The first query warms indexes "
             "serially, then the rest fan out; each query's DAG runs on its own "
             "off-loop worker thread so blocking model calls parallelize. Results "
             "stay in input order.",
    )
    parser.add_argument(
        "--max-hits-per-span",
        type=int,
        default=None,
        help="Cap the number of hit records stored on each per-node trajectory "
             "span (input.hits / output.hits). Default: store all. Golden-overlap "
             "fields are always computed over the full hit sets, so a cap never "
             "hides a diagnosis — it only bounds trajectory.json size.",
    )
    args = parser.parse_args()

    output_dir = args.output_dir
    if output_dir is None:
        output_dir = str(Path(args.queries).parent.parent)

    asyncio.run(_amain(args, output_dir))


async def _amain(args, output_dir: str) -> None:
    await generate_predictions(
        spec_dir=args.spec_dir,
        queries_path=args.queries,
        output_dir=output_dir,
        corpus_path=args.corpus,
        limit=args.limit,
        concurrency=args.concurrency,
        max_hits_per_span=args.max_hits_per_span,
    )


if __name__ == "__main__":
    main()

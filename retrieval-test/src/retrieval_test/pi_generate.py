"""Agentic-RAG generate: drive the `pi` agent CLI per query.

The optimized object is an **agent spec dir** that contains ONLY skills/ + extensions/
(the extensibility surface). `pi` is the agent: for each query it runs headlessly
(`pi -p --mode json`), loads the spec dir's skill(s) + extension(s), and calls the
`run_rag_pipeline` tool multiple times with reformulated queries; the extension
fuses hits by RRF. The final fused hits are the prediction. Recall is the only
metric (see pi_evaluate.py); the agent is never shown gold doc ids, so it cannot
cheat.

There is no manifest in the spec dir — the harness config (pipeline, model,
max_turn, …) comes from CLI args / defaults, not from the spec.

CLI:
    retrieval-test-pi-generate --spec-dir agent_specs/default \
        --queries datasets/musique/test/queries.jsonl \
        --corpus datasets/musique/test/corpus.jsonl \
        --output-dir output/pi_test [--limit N] [--concurrency C] \
        [--max-turn 8] [--max-output-hits 80] [-k 10]

Output (output_dir):
    predictions.jsonl            — {query_id, query, hits:[{doc_id,score}], n_calls}
    trajectories/<qid>.jsonl     — pi's native --mode json event stream (one event
                                   per line); that IS the agent-run trajectory.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from retrieval_test.io_utils import dump_jsonl, iter_jsonl

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]

# Defaults — the spec dir has no manifest, so config lives here / on the CLI.
DEFAULT_PIPELINE = "pipeline_specs/default"
DEFAULT_MODEL = "anthropic/glm-5.2"
DEFAULT_MAX_TURN = 8
DEFAULT_MAX_OUTPUT_HITS = 80
DEFAULT_FUSE_K = 60
DEFAULT_PI_CONFIG = "src/pi"
DEFAULT_QUERY_TIMEOUT_S = 180


def _safe_filename(query_id: str) -> str:
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in str(query_id))
    return f"{safe or 'query'}.jsonl"


def _resolve_api_key() -> str:
    """Default: the repo's retrieval_test_api_key file; else BAILING/OPENAI env."""
    key_file = REPO_ROOT / "retrieval_test_api_key"
    key = key_file.read_text(encoding="utf-8").strip() if key_file.exists() else ""
    return key or os.environ.get("BAILING_API_KEY") or os.environ.get("OPENAI_API_KEY") or ""


def _discover_resources(spec_dir: Path) -> tuple[list[str], list[str]]:
    """Auto-discover the spec dir's skills (every SKILL.md) + extensions (*.ts)."""
    skills = sorted(str(p) for p in spec_dir.rglob("SKILL.md"))
    exts = sorted(str(p) for p in (spec_dir / "extensions").glob("*.ts")) if (spec_dir / "extensions").is_dir() else []
    return skills, exts


def _run_pi_for_query(
    query: dict,
    skill_paths: list[str],
    ext_paths: list[str],
    corpus_path: str,
    run_env: dict[str, str],
    api_key: str,
    model: str,
    max_turn: int,
    query_timeout_s: int,
) -> tuple[str, str, list[dict], int, list[str], str | None]:
    """Run pi for ONE query; return (qid, query, hits, n_calls, raw_event_lines, error)."""
    qid = query["query_id"]
    qtext = query["query"]
    task = (
        "Retrieve the supporting documents for the question below using the rag-retrieval "
        f"skill. You may call run_rag_pipeline up to {max_turn} times (max_turn) with "
        "reformulated queries, then reply `done`.\n\n"
        f"Question: {qtext}"
    )

    cmd: list[str] = [
        "pi", "-p", "--mode", "json", "--offline", "--no-session",
        "--model", model, "--api-key", api_key,
        "--no-context-files", "--no-skills", "--no-extensions", "--no-builtin-tools",
        "-t", "run_rag_pipeline",
    ]
    for s in skill_paths:
        cmd += ["--skill", s]
    for e in ext_paths:
        cmd += ["-e", e]
    cmd.append(task)

    env = dict(os.environ)
    env.update(run_env)
    try:
        proc = subprocess.run(
            cmd, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=query_timeout_s,
        )
    except subprocess.TimeoutExpired:
        return (qid, qtext, [], 0, [], f"pi timed out after {query_timeout_s}s")
    except Exception as exc:  # noqa: BLE001
        return (qid, qtext, [], 0, [], f"pi spawn failed: {exc}")

    raw_lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    if proc.returncode != 0 and not raw_lines:
        return (qid, qtext, [], 0, [], f"pi exit {proc.returncode}: {proc.stderr[:300]}")

    hits, n_calls = _extract_final_hits(raw_lines)
    err = None if hits or any("agent_end" in ln for ln in raw_lines) else "no run_rag_pipeline result"
    return (qid, qtext, hits, n_calls, raw_lines, err)


def _extract_final_hits(event_lines: list[str]) -> tuple[list[dict], int]:
    """Pull the final fused hits out of pi's event stream (largest n_calls wins)."""
    best: dict | None = None
    for ln in event_lines:
        try:
            ev = json.loads(ln)
        except json.JSONDecodeError:
            continue
        for blob in _iter_text_blobs(ev):
            try:
                payload = json.loads(blob)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(payload, dict) and "hits" in payload and "n_calls" in payload:
                if best is None or payload.get("n_calls", 0) >= best.get("n_calls", 0):
                    best = payload
    if not best:
        return [], 0
    hits = [{"doc_id": h.get("doc_id"), "score": h.get("score")} for h in best.get("hits", [])]
    return hits, int(best.get("n_calls", 0))


def _iter_text_blobs(obj: Any):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _iter_text_blobs(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _iter_text_blobs(v)


def generate_agentic(
    spec_dir: str | Path,
    queries_path: str | Path,
    output_dir: str | Path,
    corpus_path: str | Path | None = None,
    limit: int | None = None,
    concurrency: int = 1,
    pipeline: str = DEFAULT_PIPELINE,
    model: str = DEFAULT_MODEL,
    max_turn: int = DEFAULT_MAX_TURN,
    max_output_hits: int = DEFAULT_MAX_OUTPUT_HITS,
    fuse_k: int = DEFAULT_FUSE_K,
    pi_config: str = DEFAULT_PI_CONFIG,
    query_timeout_s: int = DEFAULT_QUERY_TIMEOUT_S,
) -> dict[str, Any]:
    """Run the pi agent over all queries; write predictions + trajectories."""
    spec_dir = Path(spec_dir).resolve()
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    traj_dir = output_dir / "trajectories"
    traj_dir.mkdir(parents=True, exist_ok=True)

    if not corpus_path:
        raise RuntimeError("--corpus is required for the agentic pi generate")
    api_key = _resolve_api_key()
    if not api_key:
        raise RuntimeError(
            "No API key for pi: create retrieval_test_api_key, or set BAILING_API_KEY/OPENAI_API_KEY."
        )

    skill_paths, ext_paths = _discover_resources(spec_dir)
    if not ext_paths:
        raise RuntimeError(f"No extensions/*.ts found under {spec_dir} — the run_rag_pipeline tool is required.")
    pipeline_dir = REPO_ROOT / pipeline
    pi_config_dir = REPO_ROOT / pi_config

    run_env = {
        "PI_CODING_AGENT_DIR": str(pi_config_dir),
        "PI_RAG_PIPELINE": str(pipeline_dir),
        "PI_RAG_CORPUS": str(Path(corpus_path).resolve()),
        "PI_RAG_PYTHON": str(REPO_ROOT / ".venv" / "bin" / "python"),
        "PI_RAG_MAX_HITS": str(max_output_hits),
        "PI_RAG_MAX_TURN": str(max_turn),
        "PI_RAG_FUSE_K": str(fuse_k),
    }
    logger.info(
        "pi generate: spec=%s pipeline=%s model=%s max_turn=%d max_hits=%d skills=%d exts=%d concurrency=%d",
        spec_dir, pipeline_dir, model, max_turn, max_output_hits, len(skill_paths), len(ext_paths), concurrency,
    )

    queries = list(iter_jsonl(queries_path))
    if limit is not None:
        queries = queries[:limit]

    results: dict[str, tuple] = {}
    failures: list[str] = []

    def _work(q):
        return _run_pi_for_query(q, skill_paths, ext_paths, str(corpus_path), run_env, api_key,
                                 model, max_turn, query_timeout_s)

    conc = max(1, int(concurrency))
    if conc == 1:
        for i, q in enumerate(queries):
            res = _work(q)
            results[q["query_id"]] = res
            if res[5]:
                failures.append(q["query_id"])
            logger.info("pi query %d/%d (%s): n_calls=%d hits=%d%s",
                        i + 1, len(queries), q["query_id"], res[3], len(res[2]),
                        f" [err: {res[5]}]" if res[5] else "")
    else:
        with ThreadPoolExecutor(max_workers=conc) as ex:
            futs = {ex.submit(_work, q): q for q in queries}
            done = 0
            for fut in as_completed(futs):
                res = fut.result()
                results[futs[fut]["query_id"]] = res
                if res[5]:
                    failures.append(futs[fut]["query_id"])
                done += 1
                logger.info("pi query %d/%d: hits=%d%s", done, len(queries), len(res[2]),
                            " [err]" if res[5] else "")

    predictions: list[dict] = []
    for q in queries:
        qid, qtext, hits, n_calls, raw_lines, err = results[q["query_id"]]
        (traj_dir / _safe_filename(qid)).write_text(
            "\n".join(raw_lines) + ("\n" if raw_lines else ""), encoding="utf-8")
        predictions.append({"query_id": qid, "query": qtext, "hits": hits, "n_calls": n_calls})

    out_file = output_dir / "predictions.jsonl"
    dump_jsonl(predictions, out_file)
    logger.info("wrote %d predictions to %s (%d failed)", len(predictions), out_file, len(failures))
    return {
        "spec_dir": str(spec_dir),
        "n_queries": len(predictions),
        "n_failed": len(failures),
        "failed_query_ids": failures,
        "output": str(out_file),
        "trajectories_dir": str(traj_dir),
    }


def main() -> None:
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    p = argparse.ArgumentParser(description="Agentic-RAG generate: drive the pi agent CLI per query.")
    p.add_argument("--spec-dir", required=True, help="Agent spec dir (skills/ + extensions/).")
    p.add_argument("--queries", required=True, help="queries.jsonl ({query_id, query, golden_doc_ids?}).")
    p.add_argument("--corpus", required=True, help="corpus.jsonl the pipeline retrieves over.")
    p.add_argument("--output-dir", required=True, help="Output directory.")
    p.add_argument("--limit", type=int, default=None, help="Max queries to process.")
    p.add_argument("--concurrency", type=int, default=1, help="Concurrent pi processes (default 1).")
    p.add_argument("--pipeline", default=DEFAULT_PIPELINE, help="RAG pipeline spec dir (repo-root relative).")
    p.add_argument("--model", default=DEFAULT_MODEL, help="pi model id.")
    p.add_argument("--max-turn", type=int, default=DEFAULT_MAX_TURN, help="Max run_rag_pipeline calls (default 8).")
    p.add_argument("--max-output-hits", type=int, default=DEFAULT_MAX_OUTPUT_HITS, help="Final fused hit count (default 80).")
    p.add_argument("--fuse-k", type=int, default=DEFAULT_FUSE_K, help="RRF k (default 60).")
    p.add_argument("--pi-config", default=DEFAULT_PI_CONFIG, help="pi config dir (repo-root relative, default src/pi).")
    p.add_argument("--query-timeout", type=int, default=DEFAULT_QUERY_TIMEOUT_S, help="Per-query pi timeout seconds.")
    args = p.parse_args()
    res = generate_agentic(
        spec_dir=args.spec_dir, queries_path=args.queries, output_dir=args.output_dir,
        corpus_path=args.corpus, limit=args.limit, concurrency=args.concurrency,
        pipeline=args.pipeline, model=args.model, max_turn=args.max_turn,
        max_output_hits=args.max_output_hits, fuse_k=args.fuse_k,
        pi_config=args.pi_config, query_timeout_s=args.query_timeout,
    )
    print(json.dumps(res, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()

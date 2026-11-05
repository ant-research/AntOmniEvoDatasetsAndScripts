"""Embedding client via the /v1/embeddings HTTP endpoint (no SDK).

Calls the OpenAI-shaped POST {base}/embeddings (same base as the /v1/rerank
endpoint), with batching + retry + env credential resolution. Pure stdlib
urllib.

    POST https://antchat.alipay.com/v1/embeddings
    Authorization: Bearer $KEY
    body: {"model": "bge_m3", "input": ["text", ...]}
    resp: {"data": [{"index": i, "embedding": [...]}, ...]}
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Awaitable, Sequence, TypeVar

from tenacity import (
    Retrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
    wait_random,
)

# Disk cache so re-embedding the same corpus across runs / spec edits is free
# once warm. See embedding_cache for the on-disk layout (one JSON per vector,
# keyed by (model, sha1(text))).
from retrieval_test import embedding_cache
from retrieval_test.errors import RateLimitError

logger = logging.getLogger(__name__)


def _should_retry(exc: BaseException) -> bool:
    """Retry everything EXCEPT auth (HTTP 401/403) — see rerank_client._should_retry."""
    if isinstance(exc, urllib.error.HTTPError) and exc.code in (401, 403):
        return False
    return True


# ─────────────────────────────────────────────────────────────────────────────
# Available embedding models on the /v1/embeddings endpoint.
#
#   bge_m3                       BAAI/bge-m3. Sparse + Dense + multi-vector
#                                (ColBERT) in one model; strong multilingual
#                                retrieval baseline. DEFAULT for recall_dense.
#   bailing_1b_embedding         Internal ~1B-param general embedding. Heavier
#                                than bge_m3; good general-purpose option.
#   (others)                     Add here as they become available on the
#                                endpoint; keep the list current.
#
# Selection note: bge_m3's dense mode is a solid lexical+semantic hybrid
# baseline. If a domain-tuned embedding becomes available on the endpoint,
# prefer it for domain-specific corpora.
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_MODEL = "bge_m3"
DEFAULT_BASE_URL = "https://antchat.alipay.com/v1"
DEFAULT_BATCH_SIZE = 32
DEFAULT_MAX_RETRIES = 5

# Path to the local, git-ignored key file (one line: the bearer token for the
# /v1/* endpoints). The repo ships retrieval_test_api_key.example as a template;
# copy it to retrieval_test_api_key and paste your key. See _load_key_file below.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
KEY_FILE = _REPO_ROOT / "retrieval_test_api_key"
KEY_FILE_EXAMPLE = _REPO_ROOT / "retrieval_test_api_key.example"


def _load_key_file() -> str:
    """Read the bearer token from the local key file (one line), or '' if absent.

    The file is intentionally NOT committed (it's in .gitignore) so a real key
    never enters version control. Whitespace/newlines are stripped; blank/missing
    means "not configured, fall back to the next resolution source".
    """
    if not KEY_FILE.exists():
        return ""
    try:
        return KEY_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def resolve_api_key(api_key: str | None = None) -> str:
    """Resolve the API key for the /v1/* endpoints (embeddings + rerank).

    Resolution order (first non-empty wins):
      1. explicit ``api_key`` argument
      2. the local git-ignored file ``retrieval_test_api_key`` (see .example) —
         HIGHEST priority after an explicit arg, so a stale env var from the
         shell (e.g. an expired BAILING_API_KEY) cannot override a valid file key
      3. ``BAILING_API_KEY`` / ``OPENAI_API_KEY`` env vars
    Raises ValueError if none is configured. No key is ever compiled into code.
    """
    key = (
        api_key
        or _load_key_file()
        or os.environ.get("BAILING_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
    )
    if not key:
        raise ValueError(
            "API key required: pass api_key=, create "
            f"{KEY_FILE.relative_to(_REPO_ROOT)} (see "
            f"{KEY_FILE_EXAMPLE.relative_to(_REPO_ROOT)}), or set "
            "BAILING_API_KEY / OPENAI_API_KEY."
        )
    return key


async def embed_async(
    texts: Sequence[str],
    api_key: str | None = None,
    model: str = DEFAULT_MODEL,
    base_url: str = DEFAULT_BASE_URL,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_retries: int = DEFAULT_MAX_RETRIES,
) -> list[list[float]]:
    """Embed texts via POST /v1/embeddings, batched + retried, with disk cache.

    Cache hits are read straight from disk (no HTTP); only cache-miss texts are
    sent to the endpoint, in `batch_size` chunks. Newly fetched vectors are
    written to the cache so subsequent runs are free. Returns vectors in input
    order. Raises only if a miss batch exhausts retries (cached vectors already
    collected are not persisted in that case — the next run re-embeds the
    misses, which is safe).
    """
    texts = list(texts)

    # 1) Serve everything we already have on disk.
    hits, miss_idx = embedding_cache.get_many(model, texts)
    if not miss_idx:
        logger.info("embed(cache): %d/%d hit, 0 HTTP", len(hits), len(texts))
        return [hits[i] for i in range(len(texts))]

    miss_texts = [texts[i] for i in miss_idx]
    logger.info(
        "embed(cache): %d/%d hit, %d miss -> HTTP", len(hits), len(texts), len(miss_texts),
    )

    api_key = resolve_api_key(api_key)
    loop = asyncio.get_event_loop()
    url = f"{base_url}/embeddings"

    # 2) Fetch only the misses, batching as before. Preserve miss_text order so
    # we can re-key results back to their absolute input positions afterward.
    fetched: list[list[float]] = []
    for i in range(0, len(miss_texts), batch_size):
        batch = miss_texts[i : i + batch_size]
        payload = json.dumps({"model": model, "input": batch}).encode("utf-8")

        def _post() -> list[list[float]]:
            req = urllib.request.Request(
                url,
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                },
            )
            with urllib.request.urlopen(req, timeout=600) as resp:
                result = json.loads(resp.read().decode("utf-8"))
            return [
                item["embedding"]
                for item in sorted(result["data"], key=lambda x: x["index"])
            ]

        def _post_with_retry() -> list[list[float]]:
            # tenacity retry with exponential backoff; 401/403 fast-fail; on
            # budget exhaustion convert the last exception to RateLimitError.
            retrying = Retrying(
                stop=stop_after_attempt(max_retries),
                wait=wait_exponential(multiplier=1, min=1, max=60) + wait_random(0, 3),
                retry=retry_if_exception(_should_retry),
                before_sleep=lambda rs: logger.warning(
                    "embed failed (attempt %d/%d): %s",
                    rs.attempt_number, max_retries, rs.outcome.exception(),
                ) if rs.outcome and rs.outcome.failed else None,
                reraise=True,
            )
            try:
                for attempt in retrying:
                    with attempt:
                        return _post()
            except urllib.error.HTTPError as e:
                if e.code in (401, 403):
                    raise
                raise RateLimitError(
                    f"embed exhausted {max_retries} retries: HTTP {e.code} {e.reason}"
                ) from e
            except Exception as e:  # noqa: BLE001
                raise RateLimitError(
                    f"embed exhausted {max_retries} retries: {type(e).__name__}: {e}"
                ) from e
            return []

        fetched.extend(await loop.run_in_executor(None, _post_with_retry))

    # 3) Persist freshly fetched vectors to the cache (best-effort).
    embedding_cache.put_many(model, miss_texts, fetched)

    # 4) Merge cached + fetched in the original input order.
    out: list[list[float]] = [hits.get(i) for i in range(len(texts))]  # type: ignore[list-item]
    for j, abs_i in enumerate(miss_idx):
        out[abs_i] = fetched[j]
    return out


# Awaitable of T — bound by the coroutine's result. Using Awaitable (not
# Coroutine[..., R]) keeps the helper generic over whatever the caller passes
# (embed_async / rerank_async / chat_async all return coroutine objects).
_T = TypeVar("_T")


def _run_coro_sync(coro: "Awaitable[_T]") -> _T:
    """Run a coroutine to completion from sync code, loop-aware.

    retrieval-test's generate_predictions is async, so node init/run code calls
    embed_sync WHILE an event loop is already running — asyncio.run() raises
    "asyncio.run() cannot be called from a running event loop" in that case.
    We detect a running loop and, when there is one, drive the coroutine in a
    private loop on a worker thread (the only correct way to nest). With no
    running loop, asyncio.run() is used directly.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # No running loop — straightforward.
        return asyncio.run(coro)  # type: ignore[arg-type]

    # A loop IS running on this thread. Run the coro on a fresh loop in a
    # separate thread so we don't touch the caller's loop.
    import threading

    box: dict[str, object] = {}

    def _worker() -> None:
        try:
            box["result"] = asyncio.run(coro)  # type: ignore[arg-type]
        except BaseException as e:  # propagate back to the caller thread
            box["error"] = e

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join()
    if "error" in box:
        raise box["error"]  # type: ignore[misc]
    return box["result"]  # type: ignore[return-value]


# Public alias so sibling clients (rerank_client) reuse the same loop-aware
# runner instead of each rolling their own copy.
run_coro_sync = _run_coro_sync


def embed_sync(
    texts: Sequence[str],
    api_key: str | None = None,
    model: str = DEFAULT_MODEL,
    base_url: str = DEFAULT_BASE_URL,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> list[list[float]]:
    """Synchronous wrapper — for use in node ``init``/``run`` (which may run
    inside an already-running event loop, e.g. retrieval-test's async generate).

    Loop-aware: drives the coroutine safely whether or not a loop is running.
    """
    return _run_coro_sync(embed_async(texts, api_key, model, base_url, batch_size))

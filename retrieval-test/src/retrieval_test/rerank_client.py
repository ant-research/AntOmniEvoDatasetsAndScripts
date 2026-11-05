"""Rerank client via the /v1/rerank HTTP endpoint (no SDK).

Calls POST {base}/rerank with a Qwen3-Reranker-0.6B query/documents payload,
applying the model's required prefix/suffix/instruct formatting. NO SDK — pure
stdlib urllib. Same base URL and Bearer token as the embedding endpoint.

Endpoint contract (per /v1/rerank docs):
    POST https://antchat.alipay.com/v1/rerank
    Authorization: Bearer $KEY
    body: {"model": "Qwen3-Reranker-0.6B",
           "query":   <formatted query>,
           "documents":[<formatted doc>, ...]}
    resp: {"results": [{"index": i, "relevance_score": s}, ...] sorted desc}

Formatting (REQUIRED for this model — without it, retrieval drops 1-5%):
    PREFIX = '<|im_start|>system\\nJudge whether the Document meets the '
             'requirements based on the Query and the Instruct provided. Note '
             'that the answer can only be "yes" or "no".<|im_end|>\\n<|im_start|>user\\n'
    SUFFIX = '<|im_end|>\\n<|im_start|>assistant\\n\\u2234\\n\\n\\u2B1C\\n\\n'
    query   = PREFIX + '<Instruct>: {instruction}\\n<Query>: {query}\\n<Document>: '
    each doc = doc + SUFFIX
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.error
import urllib.request
from typing import Sequence

from tenacity import (
    Retrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
    wait_random,
)

from retrieval_test.errors import RateLimitError


def _should_retry(exc: BaseException) -> bool:
    """Retry everything EXCEPT auth (HTTP 401/403), which won't fix on retry.

    tenacity predicates key on exception TYPE, so we use a value-aware callback:
    a 401/403 HTTPError returns False (stop, fail fast as the bare error); every
    other failure (429/5xx/network) returns True (retry with backoff).
    """
    if isinstance(exc, urllib.error.HTTPError) and exc.code in (401, 403):
        return False
    return True

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Available rerank models on the /v1/rerank endpoint.
#
#   bge-reranker-v2-m3     BAAI cross-encoder. OpenAI-style raw query/documents
#                          (NO chat template). DEFAULT — verified present on /models.
#   Qwen3-Reranker-4B      Qwen3 reranker; REQUIRES the prefix/suffix/instruct
#                          chat-template formatting below or quality drops.
#   Qwen3-Reranker-0.6B    NOT listed by /models — kept only for reference.
#
# Formatting is MODEL-DEPENDENT (see _needs_qwen_template + format_query/doc):
#   - Qwen3-Reranker-*    -> query/doc wrapped in the Qwen3 chat template.
#   - everything else     -> raw query/doc (OpenAI-style).
#
# Selection note: this is a reranker for the *candidate list* (top-m) from
# recall nodes. It is O(top_m) model calls per query — keep top_m bounded.
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_MODEL = "bge-reranker-v2-m3"
DEFAULT_BASE_URL = "https://antchat.alipay.com/v1"
DEFAULT_INSTRUCT = "根据用户问题，找出最相关的文档。"  # zh default; swap to English if preferred
DEFAULT_MAX_RETRIES = 5
# The /v1/rerank endpoint rejects more than BATCH_LIMIT documents per call
# with ILLEGAL_PARAMETER: batch size 最大为32. Send in chunks of <= this and
# merge per-batch scores; bge-v2-m3 scores each (query,doc) pair independently,
# so splitting the batch preserves scores verbatim.
BATCH_LIMIT = 32

_PREFIX = (
    '<|im_start|>system\nJudge whether the Document meets the requirements '
    'based on the Query and the Instruct provided. Note that the answer can '
    'only be "yes" or "no".<|im_end|>\n<|im_start|>user\n'
)
# SUFFIX must reproduce the Qwen3-Reranker chat template's assistant turn tail
# EXACTLY, or retrieval quality drops 1-5%. The token after `assistant\n` is a
# template-specific marker from the endpoint's own docs (the raw byte is
# fragile across copy/paste). If rerank quality looks off, verify this string
# against the endpoint's published curl example and, if needed, override it via
# the QWEN3_RERANK_SUFFIX env var (default used here matches the documented curl).
_SUFFIX = os.environ.get(
    "QWEN3_RERANK_SUFFIX",
    '<|im_end|>\n<|im_start|>assistant\n⊣\n\n⬜\n\n',
)


def _needs_qwen_template(model: str) -> bool:
    """True iff `model` is a Qwen3-Reranker that requires chat-template wrapping.

    Non-Qwen rerankers (bge-reranker-*, gte-rerank*, jina-reranker*, ...) take
    the raw query/documents OpenAI-style — wrapping them would corrupt input.
    """
    return "qwen3-reranker" in (model or "").lower()


def format_query(query: str, model: str = DEFAULT_MODEL, instruct: str = DEFAULT_INSTRUCT) -> str:
    """Format `query` for the chosen rerank model.

    Qwen3-Reranker-*: wrap in the chat template (PREFIX + instruct + query).
    Others: raw query (OpenAI-style contract). `instruct` only affects Qwen3.
    """
    if _needs_qwen_template(model):
        return f"{_PREFIX}<Instruct>: {instruct}\n<Query>: {query}\n<Document>: "
    return query


def format_document(doc: str, model: str = DEFAULT_MODEL) -> str:
    """Format one document for the chosen rerank model.

    Qwen3-Reranker-*: doc + SUFFIX (chat template tail). Others: raw doc.
    """
    if _needs_qwen_template(model):
        return f"{doc}{_SUFFIX}"
    return doc


def _resolve_api_key(api_key: str | None) -> str:
    from retrieval_test.embedding_client import resolve_api_key
    return resolve_api_key(api_key)


async def rerank_async(
    query: str,
    documents: Sequence[str],
    api_key: str | None = None,
    model: str = DEFAULT_MODEL,
    base_url: str = DEFAULT_BASE_URL,
    instruct: str = DEFAULT_INSTRUCT,
    max_retries: int = DEFAULT_MAX_RETRIES,
) -> list[tuple[int, float]]:
    """Rerank (query, documents) via POST /v1/rerank. No SDK.

    Returns [(original_index, relevance_score), ...] sorted by relevance desc.
    The endpoint already returns results sorted by relevance_score descending.
    """
    api_key = _resolve_api_key(api_key)
    loop = asyncio.get_event_loop()
    url = f"{base_url}/rerank"
    formatted_query = format_query(query, model, instruct)
    formatted_docs = [format_document(d, model) for d in documents]

    async def _post_batch(start: int) -> list[tuple[int, float]]:
        """POST one chunk of docs [start, start+BATCH_LIMIT); return [(abs_index, score)]."""
        chunk = formatted_docs[start : start + BATCH_LIMIT]
        payload = json.dumps({
            "model": model,
            "query": formatted_query,
            "documents": chunk,
        }).encode("utf-8")

        def _post() -> list[tuple[int, float]]:
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
            # results[].index is the position within THIS chunk; remap to the
            # absolute document index in the full candidate set.
            return [
                (start + int(r["index"]), float(r["relevance_score"]))
                for r in result["results"]
            ]

        # Retry transient failures (HTTP 429/5xx/network) with exponential
        # backoff via tenacity. 401/403 are NOT retried (auth won't fix; fail
        # fast as the bare HTTPError). tenacity re-raises the last exception when
        # the budget is exhausted, so we wrap to convert it into RateLimitError —
        # the signal the generate harness replays the DAG on.
        def _post_with_retry() -> list[tuple[int, float]]:
            retrying = Retrying(
                stop=stop_after_attempt(max_retries),
                wait=wait_exponential(multiplier=1, min=1, max=60) + wait_random(0, 3),
                retry=retry_if_exception(_should_retry),
                before_sleep=lambda rs: _rerank_warn(
                    rs.attempt_number, max_retries, rs.outcome.exception()  # type: ignore[arg-type]
                ) if rs.outcome and rs.outcome.failed else None,
                reraise=True,
            )
            try:
                for attempt in retrying:
                    with attempt:
                        return _post()
            except urllib.error.HTTPError as e:
                if e.code in (401, 403):  # surfaced bare — not replayable
                    raise
                raise RateLimitError(
                    f"rerank exhausted {max_retries} retries: HTTP {e.code} {e.reason}"
                ) from e
            except Exception as e:  # noqa: BLE001 — network / 5xx / transient
                raise RateLimitError(
                    f"rerank exhausted {max_retries} retries: {type(e).__name__}: {e}"
                ) from e
            return []  # unreachable; for type-checkers

        return await loop.run_in_executor(None, _post_with_retry)

    # Send documents in <=BATCH_LIMIT chunks; bge-v2-m3 scores each (query,doc)
    # pair independently, so per-chunk scores merge into one global ranking
    # without distortion (a doc's score is its own cross-encoder output).
    all_scored: list[tuple[int, float]] = []
    for start in range(0, len(formatted_docs), BATCH_LIMIT):
        all_scored.extend(await _post_batch(start))
    all_scored.sort(key=lambda t: t[1], reverse=True)
    return all_scored


def _rerank_warn(attempt: int, max_retries: int, e: Exception) -> None:
    logger.warning(
        "rerank failed (attempt %d/%d): %s — retry shortly", attempt, max_retries, e,
    )


def rerank_sync(
    query: str,
    documents: Sequence[str],
    api_key: str | None = None,
    model: str = DEFAULT_MODEL,
    base_url: str = DEFAULT_BASE_URL,
    instruct: str = DEFAULT_INSTRUCT,
) -> list[tuple[int, float]]:
    """Synchronous wrapper — for node ``run`` (which runs inside the async
    generate event loop). Loop-aware via the shared runner from embedding_client.
    """
    from retrieval_test.embedding_client import run_coro_sync
    return run_coro_sync(
        rerank_async(query, documents, api_key, model, base_url, instruct)
    )

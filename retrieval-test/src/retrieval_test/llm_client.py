"""LLM (chat completion) client via /v1/chat/completions (no SDK).

Calls the OpenAI-shaped POST {base}/chat/completions with stdlib urllib, the
same Bearer-token key resolution (arg > file > env) as the embedding/rerank
clients, and an optional disk cache so re-running a spec with the same prompt
+ messages is free. Loop-aware (nodes can call call_sync from inside the async
generate event loop). Default model: GLM-5.2 (a reasoning model — see below).

Endpoint contract:
    POST https://antchat.alipay.com/v1/chat/completions
    Authorization: Bearer $KEY
    body: {"model": "GLM-5.2", "messages": [{"role","content"}...], "max_tokens": N}
    resp: {"choices": [{"message": {"content": "...", "reasoning_content": "..."},
                          "finish_reason": "stop"|"length"}], "usage": {...}}

Reasoning-model note: GLM-5.2 puts its chain-of-thought in `reasoning_content`
and the final answer in `content`. `max_tokens` must be large enough for BOTH
(reasoning is counted in completion_tokens), or `finish_reason` becomes "length"
and `content` comes back empty. call_sync validates this and raises a clear
error if content is empty with finish_reason=="length" (raise, not silent
return) so a mis-sized node params.max_tokens fails loudly instead of producing
empty rewrites.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import urllib.request
from pathlib import Path
from typing import Sequence

from retrieval_test.embedding_client import (
    DEFAULT_BASE_URL,
    resolve_api_key,
    run_coro_sync,
    _should_retry,
)
from retrieval_test.errors import RateLimitError

from tenacity import (
    Retrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
    wait_random,
)

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "GLM-5.2"
DEFAULT_MAX_TOKENS = 2048  # reasoning is OFF (chat_template_kwargs.enable_thinking=false),
                           # so this covers just the answer text.
DEFAULT_MAX_RETRIES = 5
DEFAULT_TEMPERATURE = 0.0  # retrieval rewrite wants determinism; a Proposer may raise it.

# Cache root is shared (see cache_root); we pick the "llm" subdirectory.
from retrieval_test.cache_root import CACHE_DIR
_CACHE_ROOT = (CACHE_DIR / "llm").resolve()


def _cache_key(model: str, messages: list[dict], max_tokens: int, temperature: float,
               enable_thinking: bool = False) -> Path:
    blob = json.dumps(
        {"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": temperature,
         "enable_thinking": enable_thinking},
        sort_keys=True, ensure_ascii=False,
    ).encode("utf-8")
    safe_model = "".join(c if c.isalnum() or c in "._-" else "_" for c in model) or "default"
    return _CACHE_ROOT / safe_model / f"{hashlib.sha1(blob).hexdigest()}.json"


def _cache_get(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        try:
            path.unlink()
        except OSError:
            pass
        return None


def _cache_put(path: Path, value: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass  # cache is best-effort


async def chat_async(
    messages: Sequence[dict],
    api_key: str | None = None,
    model: str = DEFAULT_MODEL,
    base_url: str = DEFAULT_BASE_URL,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    temperature: float = DEFAULT_TEMPERATURE,
    max_retries: int = DEFAULT_MAX_RETRIES,
    use_cache: bool = True,
    enable_thinking: bool = False,
) -> dict:
    """Chat completion via POST /v1/chat/completions. Returns the parsed response
    dict (with ``choices[0].message.content`` etc.). Disk-cached by (model,
    messages, max_tokens, temperature, enable_thinking) when ``use_cache``;
    a cache hit skips the HTTP call entirely.

    enable_thinking: default False (no reasoning). Nodes SHOULD NOT expose this
    as a pipeline.json param — reasoning is off by design for retrieval rewrite
    (cheap, direct answer). It's kept on the client so a power caller can opt in
    if ever needed, but the node layer doesn't pass it.
    """
    chat_template_kwargs = {"enable_thinking": bool(enable_thinking)}

    messages = [dict(m) for m in messages]
    ck = _cache_key(model, messages, max_tokens, temperature, enable_thinking) if use_cache else None
    if ck is not None:
        hit = _cache_get(ck)
        if hit is not None:
            logger.info("llm(cache): hit, 0 HTTP (model=%s)", model)
            return hit

    key = resolve_api_key(api_key)
    loop = asyncio.get_event_loop()
    url = f"{base_url}/chat/completions"
    body: dict = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    body["chat_template_kwargs"] = chat_template_kwargs
    payload = json.dumps(body).encode("utf-8")

    def _post() -> dict:
        req = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        )
        with urllib.request.urlopen(req, timeout=600) as resp:
            return json.loads(resp.read().decode("utf-8"))

    result: dict | None = None

    def _post_with_retry() -> dict:
        retrying = Retrying(
            stop=stop_after_attempt(max_retries),
            wait=wait_exponential(multiplier=1, min=1, max=60) + wait_random(0, 3),
            retry=retry_if_exception(_should_retry),
            before_sleep=lambda rs: logger.warning(
                "llm failed (attempt %d/%d): %s",
                rs.attempt_number, max_retries, rs.outcome.exception(),
            ) if rs.outcome and rs.outcome.failed else None,
            reraise=True,
        )
        try:
            for attempt in retrying:
                with attempt:
                    return _post()
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):  # auth — surface bare, not replayable
                raise
            raise RateLimitError(
                f"llm exhausted {max_retries} retries: HTTP {e.code} {e.reason}"
            ) from e
        except Exception as e:  # noqa: BLE001
            raise RateLimitError(
                f"llm exhausted {max_retries} retries: {type(e).__name__}: {e}"
            ) from e

    result = await loop.run_in_executor(None, _post_with_retry)

    assert result is not None
    if ck is not None:
        _cache_put(ck, result)
    return result


def call_sync(
    messages: Sequence[dict],
    api_key: str | None = None,
    model: str = DEFAULT_MODEL,
    base_url: str = DEFAULT_BASE_URL,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    temperature: float = DEFAULT_TEMPERATURE,
    use_cache: bool = True,
) -> dict:
    """Synchronous chat-completion wrapper, loop-aware (safe inside the async
    generate event loop via run_coro_sync). Returns the parsed response dict.
    """
    return run_coro_sync(chat_async(
        messages, api_key, model, base_url, max_tokens, temperature,
        use_cache=use_cache,
    ))


def extract_content(response: dict) -> str:
    """Pull the assistant's final answer text out of a chat-completion response.

    Validates the reasoning-model footgun: if ``content`` is empty and
    ``finish_reason == 'length'``, the model spent all max_tokens on
    reasoning_content and never emitted an answer — raise so the caller sees a
    clear "raise max_tokens" error instead of silently injecting '' into the
    pipeline.
    """
    choice = response["choices"][0]
    msg = choice.get("message", {})
    content = (msg.get("content") or "").strip()
    if not content:
        finish = choice.get("finish_reason")
        if finish == "length":
            raise ValueError(
                f"LLM returned empty content with finish_reason='length' — "
                f"max_tokens too small for reasoning model {response.get('model')}; "
                f"raise params.max_tokens"
            )
    return content
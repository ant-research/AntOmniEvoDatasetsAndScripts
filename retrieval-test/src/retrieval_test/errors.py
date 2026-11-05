"""Custom exception types for the retrieval pipeline.

Currently a single transient-failure marker: ``RateLimitError``. Raised by the
HTTP clients (embed / rerank / llm) when a request exhausts its own per-request
retry budget on a replayable failure (HTTP 429 / 5xx / network) — so a node can
let it propagate instead of silently degrading, and the generate harness can
replay the whole per-query DAG (see ``MAX_RETRIES`` in generate.py).

401/403 are NOT wrapped: the clients already fail-fast on auth/authz (a retry
won't fix a bad key), so they surface as the bare ``urllib.error.HTTPError``.
Only replayable transients become ``RateLimitError``.
"""

from __future__ import annotations


class RateLimitError(Exception):
    """Transient failure (HTTP 429 / 5xx / network) after exhausting client
    retries. Propagates through node ``run`` → ``run_query`` → the generate
    loop, which replays the whole DAG with backoff.
    """
"""On-disk cache for embedding vectors.

Keyed by (model, text) so that re-embedding the same corpus across spec edits
(or across generate/evaluate, which both re-load the pipeline) is free once the
cache is warm. Each vector is one JSON file under

    cache/embeddings/<model>/<sha1(text)>.json  ->  {"vector": [float, ...]}

One file per text (not a big blob) so a partial run that embeds only some docs
fills the cache incrementally, and a cache hit check is O(1) per text.

The cache is content-addressed on the text: changing the text or the model
naturally misses (different hash / different subdir), so stale vectors are
never silently reused. Deleting cache/embeddings/ clears everything.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

# Cache root is defined once in cache_root (shared by all disk caches); we only
# pick our subdirectory name here.
from retrieval_test.cache_root import CACHE_DIR
CACHE_ROOT = (CACHE_DIR / "embeddings").resolve()


def _hash(text: str) -> str:
    return hashlib.sha1(str(text).encode("utf-8")).hexdigest()


def _cache_file(model: str, text: str) -> Path:
    """Path for one vector: cache/embeddings/<model>/<sha1(text)>.json."""
    return CACHE_ROOT / _safe_model(model) / f"{_hash(text)}.json"


def _safe_model(model: str) -> str:
    """Make `model` a safe single path component (no / or escaping)."""
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in model)
    return safe or "default_model"


def get(model: str, text: str) -> list[float] | None:
    """Return cached vector for (model, text), or None if absent.

    A present-but-corrupt file is treated as a miss (deleted + None) rather than
    raising, so a half-written cache never breaks a run.
    """
    path = _cache_file(model, text)
    if not path.exists():
        return None
    try:
        return list(map(float, json.loads(path.read_text(encoding="utf-8"))["vector"]))
    except (json.JSONDecodeError, KeyError, ValueError, OSError):
        # Poisoned entry — remove it so the next run re-embeds.
        try:
            path.unlink()
        except OSError:
            pass
        return None


def put(model: str, text: str, vector: list[float]) -> None:
    """Persist a vector for (model, text). Silent on failure (cache is best-effort).

    Caching is an optimization: a write failure must never break retrieval, so
    we swallow OSError instead of propagating. The next run simply re-embeds.
    """
    path = _cache_file(model, text)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"vector": [float(x) for x in vector]}),
            encoding="utf-8",
        )
    except OSError:
        pass


def get_many(model: str, texts: list[str]) -> tuple[dict[int, list[float]], list[int]]:
    """Bulk lookup.

    Returns (hits, miss_indices):
      hits        : {input_index: vector} for cache hits
      miss_indices: [input_index, ...] for cache misses (in input order)
    Used by the embedding client to fetch only the misses from the endpoint.
    """
    hits: dict[int, list[float]] = {}
    miss_idx: list[int] = []
    for i, t in enumerate(texts):
        vec = get(model, t)
        if vec is not None:
            hits[i] = vec
        else:
            miss_idx.append(i)
    return hits, miss_idx


def put_many(model: str, texts: list[str], vectors: list[list[float]]) -> None:
    """Persist vectors aligned to texts (parallel lists). Silently best-effort."""
    if len(texts) != len(vectors):
        # Caller bug; do not corrupt the cache by misaligning.
        return
    for t, v in zip(texts, vectors):
        put(model, t, v)


def stats() -> dict[str, Any]:
    """Quick cache summary (count + bytes) for diagnostics / logging."""
    n = 0
    bytes_ = 0
    if CACHE_ROOT.exists():
        for p in CACHE_ROOT.rglob("*.json"):
            n += 1
            try:
                bytes_ += p.stat().st_size
            except OSError:
                pass
    return {"dir": str(CACHE_ROOT), "n_vectors": n, "bytes": bytes_}
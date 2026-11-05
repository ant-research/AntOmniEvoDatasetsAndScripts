"""Shared disk-cache root for the retrieval pipeline.

Nodes and clients cache build results on disk (doc embeddings, rerank scores,
LLM responses, reconstructed graphs). This module defines the ONE place the
repo root is resolved from (it sits in src/retrieval_test/, where
``parents[3]`` reliably hits the repo root), exposing ``CACHE_DIR`` so caches
live under ``<repo>/cache/<name>/`` regardless of which spec_dir a node file
was loaded from (node files move between specs, so computing the repo root
from their own path is unreliable).

Each cache picks a subdirectory name under ``CACHE_DIR`` (e.g. ``graphs``);
nothing else is shared — just the root.
"""

from pathlib import Path

# src/retrieval_test/cache_root.py -> src/retrieval_test -> src -> repo root.
REPO_ROOT = Path(__file__).resolve().parents[2]

# <repo>/cache/ — every disk cache is a subdir of this.
CACHE_DIR = (REPO_ROOT / "cache").resolve()

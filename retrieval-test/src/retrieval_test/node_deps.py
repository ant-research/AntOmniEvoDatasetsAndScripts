"""Scan node files for third-party imports and install missing packages.

Why this exists
---------------
Nodes under ``<spec_dir>/nodes/*.py`` are authored (by a Proposer or a human)
and may import arbitrary packages. The runner executes them via ``importlib``,
so a missing dependency becomes a ``ModuleNotFoundError`` only at run time —
crashes the whole pipeline with no warning. This module STATICALLY scans the
import statements of every node file BEFORE the runner executes any of them,
identifies third-party packages that are not yet installed, and installs them
into the project virtualenv via ``uv pip``.

How it identifies third-party packages
--------------------------------------
For each node file it walks the AST and collects top-level module names from
``import X`` and ``from X import ...`` statements, then drops:

  - stdlib modules (``sys.stdlib_module_names``, Py3.10+)
  - this package (``retrieval_test``) and ``__future__``
  - modules already importable in the running interpreter
    (``importlib.util.find_spec`` — checks the current env, i.e. the .venv)

What remains are third-party packages not present in the venv. Those are
installed with::

    uv pip install --python <venv> <pkgs>   [--index-url <url>]

Security / determinism
----------------------
The install uses ``uv`` (fast, lockfile-respecting) and targets the PROJECT
venv only (default ``<repo>/.venv``, overridable). An optional ``--index-url``
(see ``DEFAULT_INDEX_URL`` / env ``RETRIEVAL_TEST_PIP_INDEX_URL``) pins the
package source so installs come from a trusted mirror, not arbitrary PyPI.
Failures are collected and returned to the caller (logged + surfaced), never
silently swallowed — but a missing optional dep handled gracefully by a node's
own try/except import (e.g. recall_bm25 vs rank_bm25) will NOT appear here at
all, because the static scan sees the import regardless of the try/except.
"""

from __future__ import annotations

import ast
import logging
import os
import shutil
import subprocess
import sys
import importlib.util
from pathlib import Path

logger = logging.getLogger(__name__)

# The project virtualenv to install into. Default: repo-root/.venv. Override
# with RETRIEVAL_TEST_VENV env var (a path to the venv dir).
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_VENV = (_REPO_ROOT / ".venv").resolve()

# Optional package index URL (internal mirror). uv pulls from PyPI by default;
# set this to force a trusted mirror (deterministic + avoids public PyPI).
DEFAULT_INDEX_URL = os.environ.get("RETRIEVAL_TEST_PIP_INDEX_URL", "")

# Modules that look like third-party but are not. __future__ + this package.
_OWN_MODULES = {"retrieval_test", "__future__"}


def _stdlib_names() -> set[str]:
    """Names of stdlib modules (Py3.10+ exposes sys.stdlib_module_names)."""
    return set(getattr(sys, "stdlib_module_names", set()))


def _is_importable(name: str) -> bool:
    """True if `name` is currently importable in the running env (the venv)."""
    try:
        return importlib.util.find_spec(name) is not None
    except (ModuleNotFoundError, ValueError):
        return False


def scan_node_imports(nodes_dir: Path) -> set[str]:
    """Return the set of top-level third-party module names imported by any
    ``nodes/*.py`` file (before filtering installed ones).

    Only module ROOT names are collected (``a.b`` -> ``a``), matching how a
    package is named on the install side.
    """
    roots: set[str] = set()
    for pf in sorted(nodes_dir.glob("*.py")):
        try:
            tree = ast.parse(pf.read_text(encoding="utf-8"))
        except SyntaxError as e:
            logger.warning("could not parse %s for import scan: %s", pf, e)
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for n in node.names:
                    roots.add(n.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if node.module and node.level == 0:
                    roots.add(node.module.split(".")[0])
    return roots


def find_missing(nodes_dir: Path) -> list[str]:
    """Third-party packages imported by nodes but not installed in the venv.

    Drops stdlib, this package, and already-importable modules; returns the
    rest (sorted). These are candidates to install.
    """
    stdlib = _stdlib_names()
    missing: list[str] = []
    for name in sorted(scan_node_imports(nodes_dir)):
        if not name:
            continue
        if name in _OWN_MODULES or name in stdlib:
            continue
        # Name may map to a stdlib that stdlib_module_names lists under an alias;
        # the importability check is the authoritative test.
        if _is_importable(name):
            continue
        missing.append(name)
    return missing


def install_missing(
    nodes_dir: Path,
    venv: str | Path | None = None,
    index_url: str | None = None,
) -> dict:
    """Scan nodes and install any missing third-party packages into the venv.

    Args:
        nodes_dir: the spec's ``nodes/`` directory to scan.
        venv: path to the venv to install into (default repo .venv).
        index_url: optional ``--index-url`` for uv (default from env).

    Returns:
        ``{"installed": [...], "failed": [...], "missing": [...]}`` describing
        what was found and what happened. Failures are logged but not raised —
        the caller decides whether missing deps are fatal (some nodes degrade).
    """
    venv_dir = Path(venv) if venv else DEFAULT_VENV
    idx = index_url if index_url is not None else DEFAULT_INDEX_URL
    missing = find_missing(nodes_dir)
    result = {"installed": [], "failed": [], "missing": list(missing)}

    if not missing:
        logger.info("node deps: all node imports satisfied, nothing to install")
        return result

    uv = shutil.which("uv")
    if not uv:
        msg = "uv not found on PATH; cannot auto-install node dependencies"
        logger.warning("node deps: %s (missing: %s)", msg, ", ".join(missing))
        result["failed"] = [(p, msg) for p in missing]
        return result

    # Install the whole missing set in one uv call (atomic, fast). Target the
    # project venv explicitly so installs land in the right env regardless of
    # how this process was launched.
    cmd = [uv, "pip", "install", "--python", str(venv_dir)]
    if idx:
        cmd += ["--index-url", idx]
    cmd += missing
    logger.info("node deps: installing missing packages into %s: %s", venv_dir, ", ".join(missing))
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        result["failed"] = [(p, "uv install timed out") for p in missing]
        logger.error("node deps: uv install timed out")
        return result

    if proc.returncode != 0:
        err = (proc.stderr or "").strip().splitlines()
        logger.error("node deps: uv install failed (rc=%s): %s", proc.returncode, " | ".join(err[:3]))
        result["failed"] = [(p, f"uv rc={proc.returncode}") for p in missing]
        return result

    # Verify each is now importable; uv may install a dist whose import name
    # differs from the package name, so the truly missing-on-import check is
    # deferred to the node exec. Here we just report what we attempted + which
    # are now importable.
    still_missing = [p for p in missing if not _is_importable(p)]
    result["installed"] = [p for p in missing if p not in still_missing]
    result["failed"] = [(p, "installed but still not importable (maybe a dist-name vs import-name mismatch)") for p in still_missing]
    logger.info("node deps: installed %d, still missing importable: %d", len(result["installed"]), len(still_missing))
    return result
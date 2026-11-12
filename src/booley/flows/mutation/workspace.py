"""Scratch paths and scope hashes of one Mutation Flow run.

A run rebuilds everything: nothing here outlives its run as an input.  The
runtime directory keeps the last run's builds, variants and logs only so that
their paths can be shown to the user.

Layout under ``$BOOLEY_RUNTIME_DIR/mutation/campaign/``::

    campaign/
    ├── baseline.log
    ├── builds/
    │   ├── baseline/
    │   └── mutant_<N>/
    ├── mutant_logs/
    └── variants/
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

# The scratch directory earlier releases used, before the Mutation Flow renamed it.
LEGACY_RUNTIME_DIR = "mutation_tester"


def mutation_dir(logs_dir: Path | str | None = None) -> Path:
    """Return the directory that holds every Mutation Flow scratch tree.

    Resolution order: explicit ``logs_dir`` -> ``$BOOLEY_RUNTIME_DIR`` ->
    ``$BOOLEY_LOGS_DIR/.runtime`` -> the Project's runtime directory ->
    ``./mutation_runtime`` (direct callers in tests only).
    """
    if logs_dir is None:
        runtime_env = os.environ.get("BOOLEY_RUNTIME_DIR")
        logs_env = os.environ.get("BOOLEY_LOGS_DIR")
        if runtime_env:
            logs_dir = runtime_env
        elif logs_env:
            logs_dir = str(Path(logs_env) / ".runtime")
        else:
            try:
                from booley.runtime.booley_dir import runtime_dir

                logs_dir = str(runtime_dir())
            except (FileNotFoundError, ImportError):
                logs_dir = "mutation_runtime"
    return Path(logs_dir) / "mutation"


def legacy_runtime_dir(logs_dir: Path | str | None = None) -> Path:
    """Return the scratch directory of earlier releases; nothing reads it.

    A run removes it so an upgraded checkout keeps no stale tree of the retired Specialist.
    """
    return mutation_dir(logs_dir).parent / LEGACY_RUNTIME_DIR


def workspace_dir(logs_dir: Path | str | None = None) -> Path:
    """Return the scratch directory of the current campaign."""
    return mutation_dir(logs_dir) / "campaign"


def builds_dir(logs_dir: Path | str | None = None) -> Path:
    """Return the root containing independent baseline and mutant builds."""
    return workspace_dir(logs_dir) / "builds"


def baseline_build_dir(logs_dir: Path | str | None = None) -> Path:
    """Return the pristine source build directory."""
    return builds_dir(logs_dir) / "baseline"


def variant_build_dir(index: int, logs_dir: Path | str | None = None) -> Path:
    """Return the build directory for one isolated source replacement."""
    if index < 1:
        raise ValueError("mutation index must be positive")
    return builds_dir(logs_dir) / f"mutant_{index}"


def variants_dir(logs_dir: Path | str | None = None) -> Path:
    """Return the durable exact-source variant artifact directory."""
    return workspace_dir(logs_dir) / "variants"


def mutant_logs_dir(logs_dir: Path | str | None = None) -> Path:
    """Return the per-mutant simulator-log directory.

    Each mutant has its own compiled image, while this directory keeps the
    simulator transcript independent from build-tool layout.  A surviving
    (not-detected) mutant is the whole point of the run, and it is exactly the
    case with no failure text to read, so each run's full output is persisted
    here as ``mutant_<index>.log``.
    """
    return workspace_dir(logs_dir) / "mutant_logs"


def baseline_log_path(logs_dir: Path | str | None = None) -> Path:
    """Return the current campaign's pristine-baseline simulator log path."""
    return workspace_dir(logs_dir) / "baseline.log"


def _hash_file(path: Path) -> str:
    """Return ``sha256:<hex>`` digest of *path* contents."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return f"sha256:{h.hexdigest()}"


def compute_scope_hashes(scope_files: list[str], work_dir: Path) -> dict[str, str]:
    """Compute sha256 digests for every scope file.

    Missing files map to the literal string ``"sha256:MISSING"`` so callers
    can distinguish "file went away" from "file is empty" (empty hashes to
    a real digest).
    """
    hashes: dict[str, str] = {}
    for rel in scope_files:
        p = Path(rel) if Path(rel).is_absolute() else work_dir / rel
        hashes[rel] = _hash_file(p) if p.exists() else "sha256:MISSING"
    return hashes

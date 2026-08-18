"""Canonical booley.toml path resolution.

One home for the two path-resolution shapes that were previously copy-pasted
across ``project_config``, ``shared_infra``, ``harness.config`` and
``harness._backend_config`` (Issue #4/#7: ~4 loaders, two functions both named
``_resolve_booley_toml``). Stdlib-only, so it sits at the bottom of the import
graph with no cycle risk.
"""

from __future__ import annotations

from pathlib import Path


def resolve_toml(directory: Path) -> Path:
    """Resolve ``booley.toml`` in *directory*, falling back to ``pipeline.toml``.

    Returns the ``booley.toml`` path when it exists, otherwise the legacy
    ``pipeline.toml`` path (which may or may not exist — callers check).
    """
    new = directory / "booley.toml"
    if new.exists():
        return new
    return directory / "pipeline.toml"


def resolve_booley_toml(project_root: Path) -> Path:
    """Resolve ``booley.toml`` from a repository *project_root*.

    Handles the standard ``.booley_project/`` layout and the legacy
    ``.booley/project/`` layout, then delegates to :func:`resolve_toml` for the
    ``booley.toml`` → ``pipeline.toml`` filename fallback.
    """
    project_dir = project_root / ".booley_project"
    if not project_dir.is_dir():
        project_dir = project_root / ".booley" / "project"
    return resolve_toml(project_dir)

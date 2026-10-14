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
    """Resolve configuration from the current checkout's Booley Directory."""
    from booley.core.booley_layout import require_current_layout

    require_current_layout(project_root)
    return resolve_toml(project_root / ".booley")

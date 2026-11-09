"""Canonical booley.toml path resolution.

One home for the two path-resolution shapes that were previously copy-pasted
across ``project_config``, ``shared_infra``, ``harness.config`` and
``harness._backend_config`` (Issue #4/#7: ~4 loaders, two functions both named
``_resolve_booley_toml``). Stdlib-only, so it sits at the bottom of the import
graph with no cycle risk.
"""

from __future__ import annotations

from pathlib import Path

# Directory, inside the Booley Directory, that holds every Goalset.
GOALSETS_DIR = "goalsets"


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
    from booley.core.booley_dir import own_booley_dir
    from booley.core.booley_layout import require_current_layout

    require_current_layout(project_root)
    return resolve_toml(own_booley_dir(project_root))


def main_repository_root(work_dir: Path) -> Path | None:
    """Follow a linked worktree's ``.git`` file to its main repository root.

    In a linked worktree ``.git`` is a *file* holding ``gitdir: <path>`` that
    points into the main repository's ``.git/worktrees/`` directory. Returns the
    main repository root, or ``None`` when *work_dir* is the main repository
    itself or has no recognisable ``.git`` file.
    """
    marker = work_dir / ".git"
    if not marker.is_file():
        return None
    try:
        content = marker.read_text(encoding="utf-8").strip()
        if not content.startswith("gitdir:"):
            return None
        git_dir = Path(content.split(":", 1)[1].strip())
        if not git_dir.is_absolute():
            git_dir = (work_dir / git_dir).resolve()
        for parent in git_dir.parents:
            if parent.name == ".git":
                return parent.parent
    except (OSError, ValueError):
        pass
    return None

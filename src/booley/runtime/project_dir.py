"""Runtime directories and the stable Project-directory compatibility interface."""

from __future__ import annotations

from pathlib import Path
from stat import S_ISDIR

from booley.core.project_dir import (
    PROJECT_DIR_NAME,
    checkout_project_dir_relative_to,
    init_project_dir_scope,
    project_dir_for_init,
    reset_cache,
    resolve_authoritative_project_dir,
    resolve_checkout_project_dir,
    resolve_project_dir,
)

__all__ = [
    "PROJECT_DIR_NAME",
    "checkout_project_dir_relative_to",
    "contains",
    "init_project_dir_scope",
    "project_dir_for_init",
    "reset_cache",
    "resolve_authoritative_project_dir",
    "resolve_checkout_project_dir",
    "resolve_project_dir",
]


def checkout_runtime_dir(project_root: Path) -> Path:
    """Return the runtime tree anchored to one explicitly selected checkout."""
    directory = resolve_checkout_project_dir(project_root) / ".runtime"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def runtime_dir(start: Path | None = None) -> Path:
    """Return and create the active Project's transient ``.runtime`` tree."""
    directory = resolve_project_dir(start) / ".runtime"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _identity_suffix(candidate: Path, root: Path) -> Path | None:
    """Find a directory ancestor physically identical to the selected root."""
    for ancestor in (candidate, *candidate.parents):
        try:
            directory = S_ISDIR(ancestor.stat().st_mode)
        except FileNotFoundError:
            continue
        if directory and ancestor.samefile(root):
            return candidate.relative_to(ancestor)
    return None


def contains(path: str | Path, *, project_dir: Path | None = None) -> Path | None:
    """Return a selected-root Project-data path, or None if membership is unproven.

    Directory identity admits independent mount spellings, including missing
    descendants of an existing root. File hardlinks do not establish membership.
    Explicit ``project_dir`` supplies authority independently of active selection.
    Resolve internal symlinks while retaining the selected root spelling for aliases.
    Rebasing selects root authority; asymmetric mounts need not preserve leaf identity.
    Filesystem errors fail closed; selection errors and malformed paths propagate.
    This predicate does not authorize mutation or provide a race-free file open.
    """
    selected = resolve_project_dir() if project_dir is None else project_dir
    candidate = Path(path)
    try:
        root = selected.resolve()
        if not root.is_dir():
            return None
        suffix = _identity_suffix(candidate.resolve(), root)
        if suffix is None:
            return None
        canonical = (root / suffix).resolve()
        canonical_suffix = _identity_suffix(canonical, root)
        if canonical_suffix is None:
            return None
        authoritative = root / canonical_suffix
        resolved = authoritative.resolve()
        if _identity_suffix(resolved, root) is None:
            return None
        return resolved if resolved.is_relative_to(root) else authoritative
    except (OSError, RuntimeError):
        return None

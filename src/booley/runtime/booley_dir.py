"""Runtime directories and the stable Booley Directory compatibility interface."""

from __future__ import annotations

from pathlib import Path
from stat import S_ISDIR

from booley.core.booley_dir import (
    BOOLEY_DIR_NAME,
    booley_dir_for_init,
    checkout_booley_dir_relative_to,
    committed_project_scope,
    init_booley_dir_scope,
    reset_cache,
    resolve_authoritative_booley_dir,
    resolve_booley_dir,
    resolve_checkout_booley_dir,
)

__all__ = [
    "BOOLEY_DIR_NAME",
    "booley_dir_for_init",
    "checkout_booley_dir_relative_to",
    "committed_project_scope",
    "contains",
    "init_booley_dir_scope",
    "reset_cache",
    "resolve_authoritative_booley_dir",
    "resolve_booley_dir",
    "resolve_checkout_booley_dir",
]


def checkout_runtime_dir(project_root: Path) -> Path:
    """Return the runtime tree anchored to one explicitly selected checkout."""
    directory = resolve_checkout_booley_dir(project_root) / ".runtime"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def runtime_dir(start: Path | None = None) -> Path:
    """Return and create the active Project's transient ``.runtime`` tree."""
    directory = resolve_booley_dir(start) / ".runtime"
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


def contains(path: str | Path, *, booley_dir: Path | None = None) -> Path | None:
    """Return a selected-root Booley Directory path, or None if membership is unproven.

    Directory identity admits independent mount spellings, including missing
    descendants of an existing root. File hardlinks do not establish membership.
    Explicit ``booley_dir`` supplies authority independently of active selection.
    Resolve internal symlinks while retaining the selected root spelling for aliases.
    Rebasing selects root authority; asymmetric mounts need not preserve leaf identity.
    Filesystem errors fail closed; selection errors and malformed paths propagate.
    This predicate does not authorize mutation or provide a race-free file open.
    """
    selected = resolve_booley_dir() if booley_dir is None else booley_dir
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

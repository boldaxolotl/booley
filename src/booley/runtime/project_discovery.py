"""Infer a checkout without mistaking its Booley Directory for design sources."""

from __future__ import annotations

import os
from collections.abc import Callable
from functools import cache
from itertools import chain
from pathlib import Path

from booley.core.booley_dir import BOOLEY_DIR_NAME, resolve_authoritative_booley_dir
from booley.core.checkout_role import SourceCheckoutProjectError, require_project_checkout
from booley.runtime.booley_dir import contains
from booley.runtime.sandbox_layout import BOOLEY_DIR_TARGET, WORK_DIR


class ProjectRootDiscoveryError(RuntimeError):
    """A Booley Directory cannot prove its owning checkout."""


def has_git_worktree_marker(path: Path) -> bool:
    """Recognize a checkout, excluding empty mounted Git sentinels."""
    marker = path / ".git"
    try:
        return (
            (marker / "HEAD").is_file()
            if marker.is_dir()
            else marker.is_file()
            and marker.read_text(encoding="utf-8", errors="replace").startswith("gitdir:")
        )
    except OSError:
        return False


def _data_root(
    start: Path, checkouts: list[Path], checkout_data: Callable[[Path], Path | None]
) -> Path | None:
    for parent in (start, *start.parents):
        if parent.name == BOOLEY_DIR_NAME and (
            parent != Path.home() / BOOLEY_DIR_NAME or has_git_worktree_marker(parent.parent)
        ):
            return parent
        if parent in checkouts:
            break
    configured = [os.environ.get("BOOLEY_DIR")]
    if os.environ.get("BOOLEY_CONTAINER") == "1":
        configured.append(BOOLEY_DIR_TARGET)
    directories = chain(
        (Path(value).resolve() for value in configured if value),
        (
            data.resolve()
            for checkout in checkouts
            if (data := checkout_data(checkout)) is not None
        ),
    )
    for data in directories:
        if contains(start, booley_dir=data) is not None:
            if any(
                (canonical := contains(p, booley_dir=data)) is not None
                and canonical != data.resolve()
                for p in checkouts
            ):
                continue
            return data
    return None


def _checkout_candidates(start: Path | None) -> tuple[Path, list[Path]]:
    logical = Path(start or Path.cwd()).absolute()
    current = logical.resolve()
    if start is None and os.environ.get("PWD"):
        candidate = Path(os.environ["PWD"])
        try:
            if candidate.samefile(current):
                logical = candidate.absolute()
        except OSError:
            pass
    paths = (current, *current.parents, logical, *logical.parents)
    checkouts = list(
        dict.fromkeys(
            p.resolve()
            for p in paths
            if (p.name != BOOLEY_DIR_NAME or not has_git_worktree_marker(p.parent))
            and has_git_worktree_marker(p)
        )
    )
    return current, checkouts


def _owning_checkouts(
    candidates: list[Path], data: Path, checkout_data: Callable[[Path], Path | None]
) -> set[Path]:
    owners = set()
    for candidate in candidates:
        if contains(candidate, booley_dir=data) is None and has_git_worktree_marker(candidate):
            try:
                root = candidate.resolve()
                selected = checkout_data(root)
                if selected is not None and selected.samefile(data):
                    owners.add(root)
            except OSError:
                pass
    return owners


def discover_project_root(
    start: Path | None = None, *, required: bool = False, explicit_selection: bool = False
) -> Path | None:
    """Resolve explicit selection or an independently proven owning checkout."""

    @cache
    def checkout_data(root):
        try:
            return resolve_authoritative_booley_dir(require_project_checkout(root))
        except SourceCheckoutProjectError:
            return None

    current, checkouts = _checkout_candidates(start)
    data = _data_root(current, checkouts, checkout_data)
    env = None if explicit_selection else os.environ.get("RTL_PROJECT_ROOT")
    explicit = None
    if env:
        explicit, explicit_checkouts = _checkout_candidates(Path(env))
        explicit_data = _data_root(explicit, explicit_checkouts, checkout_data)
        if explicit_data is None:
            return explicit
        data = data or explicit_data
    if data is not None:
        candidates = checkouts[:]
        if os.environ.get("BOOLEY_CONTAINER") == "1":
            candidates.append(Path(WORK_DIR))
        owners = _owning_checkouts(candidates, data, checkout_data)
        if not owners and start is not None:
            # A data-path lookup can originate in its owning checkout.
            try:
                _current, cwd_checkouts = _checkout_candidates(None)
            except OSError:
                cwd_checkouts = []
            owners = _owning_checkouts(cwd_checkouts, data, checkout_data)
        if len(owners) == 1 and explicit is None:
            return owners.pop()
        raise ProjectRootDiscoveryError(
            "Cannot determine Project Root from Booley Directory; "
            "run from the Project Root or set RTL_PROJECT_ROOT to it."
        )
    if checkouts:
        return checkouts[0]
    return None if required else current

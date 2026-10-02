"""Infer a checkout without mistaking its Project data for design sources."""

from __future__ import annotations

import os
from pathlib import Path

from booley.core.checkout_role import SourceCheckoutProjectError, require_project_checkout
from booley.core.project_dir import PROJECT_DIR_NAME, resolve_authoritative_project_dir
from booley.runtime.sandbox_layout import PROJECT_DIR_TARGET, WORK_DIR


class ProjectRootDiscoveryError(RuntimeError):
    """A Project data directory cannot prove its owning checkout."""


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


def _checkout_data(root: Path) -> Path:
    """Restrict ownership evidence to this checkout, excluding ancestor config."""
    root = require_project_checkout(root)
    if (root / "booley.toml").is_file():
        return resolve_authoritative_project_dir(root)
    local = root / PROJECT_DIR_NAME
    return local if local.is_dir() else root / ".booley" / "project"


def _owns(root: Path, data: Path) -> bool:
    try:
        return _checkout_data(root).samefile(data)
    except (OSError, SourceCheckoutProjectError):
        return False


def _data_root(start: Path, checkouts: list[Path]) -> Path | None:
    for parent in (start, *start.parents):
        if parent.name == PROJECT_DIR_NAME or (
            parent.name == "project" and parent.parent.name == ".booley"
        ):
            return parent
        if parent in checkouts:
            break
    configured = [os.environ.get("BOOLEY_PROJECT_DIR")]
    if os.environ.get("BOOLEY_CONTAINER") == "1":
        configured.append(PROJECT_DIR_TARGET)
    for value in configured:
        if value:
            data = Path(value).resolve()
            if start.is_relative_to(data) and not any(
                checkout != data and checkout.is_relative_to(data) for checkout in checkouts
            ):
                return data
    for checkout in checkouts:
        try:
            data = _checkout_data(checkout).resolve()
        except SourceCheckoutProjectError:
            continue
        if start.is_relative_to(data):
            if any(p != data and p.is_relative_to(data) for p in checkouts):
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
            p.resolve() for p in paths if p.name != ".booley" and has_git_worktree_marker(p)
        )
    )
    return current, checkouts


def _owning_checkouts(candidates: list[Path], data: Path) -> set[Path]:
    """Require independent checkout-local ownership for every candidate."""
    return {
        p.resolve()
        for p in candidates
        if not p.resolve().is_relative_to(data) and has_git_worktree_marker(p) and _owns(p, data)
    }


def discover_project_root(start: Path | None = None, *, required: bool = False) -> Path | None:
    """Resolve explicit selection or an independently proven owning checkout."""
    current, checkouts = _checkout_candidates(start)
    data = _data_root(current, checkouts)
    env = os.environ.get("RTL_PROJECT_ROOT")
    explicit = Path(env).resolve() if env else None
    if explicit is not None:
        _selected, explicit_checkouts = _checkout_candidates(Path(env))
        explicit_data = _data_root(explicit, explicit_checkouts)
        if explicit_data is None:
            return explicit
        data = data or explicit_data
    if data is not None:
        candidates = checkouts[:]
        if os.environ.get("BOOLEY_CONTAINER") == "1":
            candidates.append(Path(WORK_DIR))
        owners = _owning_checkouts(candidates, data)
        if not owners and start is not None:
            # A data-path lookup can originate in its owning checkout.
            try:
                _current, cwd_checkouts = _checkout_candidates(None)
            except OSError:
                cwd_checkouts = []
            owners = _owning_checkouts(cwd_checkouts, data)
        if len(owners) == 1 and explicit is None:
            return owners.pop()
        raise ProjectRootDiscoveryError(
            "Cannot determine Project checkout from Project data directory; "
            "run from the Project checkout or set RTL_PROJECT_ROOT to it."
        )
    if checkouts:
        return checkouts[0]
    return None if required else current

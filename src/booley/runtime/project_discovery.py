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
    configured = [os.environ.get("BOOLEY_PROJECT_DIR")]
    if os.environ.get("BOOLEY_CONTAINER") == "1":
        configured.append(PROJECT_DIR_TARGET)
    for value in configured:
        if value and start.is_relative_to(Path(value).resolve()):
            return Path(value).resolve()
    for checkout in checkouts:
        try:
            data = _checkout_data(checkout).resolve()
        except SourceCheckoutProjectError:
            continue
        if start.is_relative_to(data):
            return data
    return None


def discover_project_root(start: Path | None = None, *, required: bool = False) -> Path | None:
    """Resolve explicit selection or an independently proven owning checkout."""
    current = Path(start or Path.cwd()).resolve()
    checkouts = [
        p
        for p in (current, *current.parents)
        if p.name != ".booley" and has_git_worktree_marker(p)
    ]
    data = _data_root(current, checkouts)
    env = os.environ.get("RTL_PROJECT_ROOT")
    explicit = Path(env).resolve() if env else None
    if explicit is not None:
        explicit_checkouts = [
            p for p in (explicit, *explicit.parents) if has_git_worktree_marker(p)
        ]
        explicit_data = _data_root(explicit, explicit_checkouts)
        if explicit_data is None and (data is None or not explicit.is_relative_to(data)):
            return explicit
        data = data or explicit_data
    if data is not None:
        candidates = checkouts[:]
        if os.environ.get("BOOLEY_CONTAINER") == "1":
            candidates.append(Path(WORK_DIR))
        owners = {
            p.resolve()
            for p in candidates
            if not p.resolve().is_relative_to(data)
            and has_git_worktree_marker(p)
            and _owns(p, data)
        }
        if len(owners) == 1 and explicit is None:
            return owners.pop()
        raise ProjectRootDiscoveryError(
            "Cannot determine Project checkout from Project data directory; "
            "run from the Project checkout or set RTL_PROJECT_ROOT to it."
        )
    if checkouts:
        return checkouts[0]
    return None if required else current

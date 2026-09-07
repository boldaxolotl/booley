"""Crash-recoverable relocation for linked Git worktrees."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from booley.runtime.submodule_materialization import (
    SubmoduleMaterializationError,
    submodule_paths,
)

_TIMEOUT_S = 30


class WorktreeRelocationError(RuntimeError):
    """A linked worktree cannot be safely relocated or recovered."""


@dataclass(frozen=True)
class WorktreeMove:
    """One source and destination pair in a larger relocation."""

    repository: Path
    ref: str
    source: Path
    destination: Path


def preflight_worktree_moves(
    moves: tuple[WorktreeMove, ...],
    *,
    occupied_destinations: frozenset[Path] = frozenset(),
) -> None:
    """Validate a complete relocation plan before its first mutation."""
    normalized = tuple(
        WorktreeMove(
            move.repository.resolve(),
            move.ref,
            move.source.resolve(),
            move.destination.resolve(),
        )
        for move in moves
    )
    allowed = {path.resolve() for path in occupied_destinations}
    sources = {(move.repository, move.source) for move in normalized}
    registrations: dict[Path, dict[Path, str]] = {}
    for move in normalized:
        registered = registrations.get(move.repository)
        if registered is None:
            registered = _registered_worktrees(move.repository)
            registrations[move.repository] = registered
        _validate_registration(move, registered)
        if move.source == move.destination:
            continue
        interrupted = not move.source.exists() and move.destination.is_dir()
        inspected = move.destination if interrupted else move.source
        _validate_source(inspected)
        _validate_same_filesystem(inspected, move.destination)
        if (move.destination.exists() or move.destination.is_symlink()) and (
            (move.repository, move.destination) not in sources
            and move.destination not in allowed
            and not interrupted
        ):
            raise WorktreeRelocationError(
                f"worktree destination already exists: {move.destination}"
            )


def relocate_worktree(repository: Path, ref: str, source: Path, destination: Path) -> None:
    """Atomically move one linked worktree and repair its registration.

    Standalone submodule repositories move with the worktree. Native Git
    submodules use path-coupled administrative state and are rejected before
    mutation. Repeating the call repairs an interruption after the rename.
    """
    repository = repository.resolve()
    source = source.resolve()
    destination = destination.resolve()
    registered = _registered_worktrees(repository)
    if registered.get(destination) == ref:
        _validate_destination(destination)
        return
    if registered.get(source) != ref:
        raise WorktreeRelocationError(f"worktree for {ref} is not registered at {source}")
    if source.exists():
        preflight_worktree_moves((WorktreeMove(repository, ref, source, destination),))
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            source.rename(destination)
        except OSError as exc:
            raise WorktreeRelocationError(
                f"could not relocate worktree {source} to {destination}: {exc}"
            ) from exc
    elif not destination.exists():
        raise WorktreeRelocationError(
            f"registered worktree and relocation destination are both unavailable: {source}"
        )
    _repair_registration(repository, ref, destination)
    _validate_destination(destination)


def _validate_registration(move: WorktreeMove, registered: dict[Path, str]) -> None:
    source_ref = registered.get(move.source)
    destination_ref = registered.get(move.destination)
    if move.ref in {source_ref, destination_ref}:
        return
    raise WorktreeRelocationError(
        f"worktree for {move.ref} is not registered at {move.source} or {move.destination}"
    )


def _validate_source(source: Path) -> None:
    if not source.is_dir() or source.is_symlink():
        raise WorktreeRelocationError(f"worktree source is unavailable: {source}")
    _validate_submodule_layout(source, source)


def _validate_same_filesystem(source: Path, destination: Path) -> None:
    existing_parent = _nearest_existing_parent(destination.parent)
    if source.stat().st_dev != existing_parent.stat().st_dev:
        raise WorktreeRelocationError(
            f"worktree relocation must stay on one filesystem: {source} -> {destination}"
        )


def _nearest_existing_parent(path: Path) -> Path:
    current = path
    while not current.exists():
        parent = current.parent
        if parent == current:
            raise WorktreeRelocationError(f"worktree destination has no existing parent: {path}")
        current = parent
    if not current.is_dir() or current.is_symlink():
        raise WorktreeRelocationError(f"worktree destination parent is unsafe: {current}")
    return current


def _validate_submodule_layout(repository: Path, worktree_root: Path) -> None:
    try:
        paths = submodule_paths(repository)
    except SubmoduleMaterializationError as exc:
        raise WorktreeRelocationError(str(exc)) from exc
    for relative in paths:
        submodule = repository / relative
        if not submodule.exists():
            continue
        if submodule.is_symlink() or not submodule.is_dir():
            raise WorktreeRelocationError(f"submodule path is unsafe: {submodule}")
        marker = submodule / ".git"
        if marker.is_file() or marker.is_symlink():
            shown = submodule.relative_to(worktree_root).as_posix()
            raise WorktreeRelocationError(
                f"worktree contains native Git submodule {shown!r}; "
                f"deinitialize native submodules in {repository} with "
                "`git submodule deinit -f --all`, then retry; no worktrees were moved "
                "because native submodule metadata is path-coupled"
            )
        if not marker.is_dir():
            if any(submodule.iterdir()):
                raise WorktreeRelocationError(
                    f"initialized submodule has no standalone repository: {submodule}"
                )
            continue
        top = Path(_git(submodule, "rev-parse", "--show-toplevel")).resolve()
        if top != submodule.resolve():
            raise WorktreeRelocationError(
                f"standalone submodule identity changed: expected {submodule}, got {top}"
            )
        _validate_submodule_layout(submodule, worktree_root)


def _repair_registration(repository: Path, ref: str, destination: Path) -> None:
    _git(repository, "worktree", "repair", str(destination))
    if _registered_worktrees(repository).get(destination) != ref:
        raise WorktreeRelocationError(
            f"Git did not register relocated worktree for {ref} at {destination}"
        )


def _validate_destination(destination: Path) -> None:
    top = Path(_git(destination, "rev-parse", "--show-toplevel")).resolve()
    if top != destination:
        raise WorktreeRelocationError(
            f"relocated worktree identity changed: expected {destination}, got {top}"
        )
    _validate_submodule_layout(destination, destination)


def _registered_worktrees(repository: Path) -> dict[Path, str]:
    paths: dict[Path, str] = {}
    current: Path | None = None
    for line in [*_git(repository, "worktree", "list", "--porcelain").splitlines(), ""]:
        if line.startswith("worktree "):
            current = Path(line.removeprefix("worktree ")).resolve()
        elif line.startswith("branch ") and current is not None:
            paths[current] = line.removeprefix("branch ")
        elif not line:
            current = None
    return paths


def _git(repository: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repository,
        capture_output=True,
        text=True,
        timeout=_TIMEOUT_S,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise WorktreeRelocationError(f"git {' '.join(args)} failed in {repository}: {detail}")
    return result.stdout.strip()

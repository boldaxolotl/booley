"""Native-submodule checks for a linked worktree that a directory migration moves."""

from __future__ import annotations

from pathlib import Path

from booley.runtime import platform_paths
from booley.runtime.submodule_materialization import (
    SubmoduleMaterializationError,
    submodule_paths,
)

_TIMEOUT_S = 30


class WorktreeRelocationError(RuntimeError):
    """A linked worktree fails a native-submodule check for the Booley Directory migration."""


def _validate_source(source: Path) -> None:
    _validate_submodule_layout(source, source)


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


def _git(repository: Path, *args: str) -> str:
    result = platform_paths.run_captured_bounded(
        ["git", *args], timeout=_TIMEOUT_S, cwd=repository, errors="strict"
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise WorktreeRelocationError(f"git {' '.join(args)} failed in {repository}: {detail}")
    return result.stdout.strip()

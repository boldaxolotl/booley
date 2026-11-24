"""Give a linked worktree made by Git or T3 Code its own Booley Directory.

``booley worktree prepare`` adopts a worktree that ``booley worktree new`` did not
create. These are its steps without printing: which worktree of which Project a
path names, and the Booley Directory it gets (or loses again when the run fails).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from booley.runtime.booley_dir import own_booley_dir, own_booley_dir_relative
from booley.runtime.filesystem_utils import safe_rmtree
from booley.runtime.project_repositories import (
    GitDirectoryInspectionError,
    git_directories,
    run_git,
)
from booley.runtime.project_worktree_pairing import pair_project_worktree, rollback_project_pairing
from booley.runtime.worktree_snapshot import copy_snapshot


class WorktreePrepareError(RuntimeError):
    """A worktree cannot be prepared; the message names the cause."""


@dataclass(frozen=True)
class PrepareTarget:
    """A linked worktree and the main checkout of the Project it belongs to."""

    main: Path
    worktree: Path

    @property
    def name(self) -> str:
        """The worktree's folder name, which names its Paired Checkout branch."""
        return self.worktree.name


@dataclass(frozen=True)
class WorktreeBooleyDir:
    """The worktree's own Booley Directory and what this run did to get it."""

    kind: Literal["tracked", "paired", "snapshot"]
    path: Path
    created: bool
    left_out: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()


def prepare_target(path: Path) -> PrepareTarget | None:
    """Return the linked worktree *path* is in, or ``None`` outside a Booley Project.

    Raises:
        WorktreePrepareError: *path* is not a directory, or it is in the main checkout.
    """
    if not path.is_dir():
        raise WorktreePrepareError(f"{path} is not a directory")
    try:
        directories = git_directories(path)
    except GitDirectoryInspectionError:
        return None
    common_dir = directories.common_dir
    if common_dir.name != ".git":
        return None
    main = common_dir.parent
    if not ((main / "booley.toml").is_file() or own_booley_dir(main).is_dir()):
        return None
    if directories.git_dir == common_dir:
        raise WorktreePrepareError(
            f"{main} is the main checkout of a Booley Project; "
            "run booley worktree prepare in a linked worktree"
        )
    top = run_git(path, "rev-parse", "--show-toplevel")
    if top.returncode != 0:
        raise WorktreePrepareError(
            f"cannot find the worktree root of {path}: {top.stderr.strip()}"
        )
    return PrepareTarget(main, Path(top.stdout.strip()))


def give_booley_dir(target: PrepareTarget, source: Path | None) -> WorktreeBooleyDir:
    """Give *target.worktree* its Booley Directory unless it already has one.

    *source* is the main Versioned Booley Directory (``None`` when it is not
    versioned). Git-tracked contents stay as they are; a Versioned directory
    becomes a Paired Checkout; any other becomes a Snapshot, published
    atomically so an existing directory is always complete.

    Raises:
        WorktreePrepareError: the directory links out of the worktree, or an
            existing one is not a Paired Checkout of a Versioned Project.
    """
    relative = own_booley_dir_relative(target.worktree)
    nested = target.worktree / relative
    if nested.is_symlink() or not nested.resolve().is_relative_to(target.worktree.resolve()):
        raise WorktreePrepareError(
            f"{nested} is a link out of the worktree; it would share another checkout's configuration"
        )
    tracked = run_git(target.worktree, "ls-files", "-z", "--", str(relative))
    if tracked.stdout:
        return WorktreeBooleyDir("tracked", nested, created=False)
    if source is None:
        return _give_snapshot(target, nested)
    if (nested / ".git").is_file():
        return WorktreeBooleyDir("paired", nested, created=False)
    if nested.exists():
        raise WorktreePrepareError(
            f"{nested} exists but is not a Paired Checkout; move it away and run prepare again"
        )
    pairing = pair_project_worktree(target.main, target.worktree, target.name, source=source)
    assert pairing is not None, "a Versioned Booley Directory pairs"
    return WorktreeBooleyDir("paired", nested, created=True, left_out=pairing.left_out)


def _give_snapshot(target: PrepareTarget, nested: Path) -> WorktreeBooleyDir:
    if nested.exists():
        return WorktreeBooleyDir("snapshot", nested, created=False)
    staging = nested.with_name(f"{nested.name}.prepare-{os.getpid()}")
    try:
        skipped = copy_snapshot(own_booley_dir(target.main), staging)
        staging.rename(nested)
    finally:
        safe_rmtree(staging)
    return WorktreeBooleyDir("snapshot", nested, created=True, skipped=tuple(skipped))


def remove_booley_dir(target: PrepareTarget, made: WorktreeBooleyDir, source: Path | None) -> None:
    """Remove the Booley Directory this run created; keep one it found.

    Raises:
        WorktreePrepareError: the post-setup hook moved the worktree's Booley
            Directory, so removing what was made would name another path.
    """
    if not made.created:
        return
    if own_booley_dir(target.worktree) != made.path:
        raise WorktreePrepareError(
            f"the post-setup hook changed the worktree's Booley Directory; remove {made.path} by hand"
        )
    if source is not None:
        rollback_project_pairing(source, target.worktree, target.name)
    else:
        safe_rmtree(made.path)

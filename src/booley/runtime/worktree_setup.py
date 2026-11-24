"""Set up a linked worktree after its outer checkout exists: markers, Git config, Snapshot.

``booley worktree new`` creates the outer worktree with the packaged shell script, then
calls :func:`set_up_linked_worktree`; each step also applies to any linked worktree on
its own.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path

from booley.core.booley_dir import own_booley_dir, own_booley_dir_relative
from booley.core.booley_layout import BooleyLayoutError
from booley.core.file_lock import release_file_lock, wait_for_file_lock
from booley.runtime.git_environment import inherited_git_environment
from booley.runtime.incontainer_git_identity import (
    GitIdentityError,
    apply_git_identity,
    load_git_identity,
)
from booley.runtime.platform_paths import run_captured_bounded
from booley.runtime.project_repositories import (
    GitDirectoryInspectionError,
    git_directories,
    run_git,
)
from booley.runtime.worktree_snapshot import SnapshotError, copy_snapshot

_MARKER_LINE = (
    b"# Booley Directory: keep transient worktree .core copies out of FuseSoC/Booley "
    b"core discovery.\n"
)
_GIT_TIMEOUT_S = 30
_CHECKOUT_TIMEOUT_S = 300
# Bounds the wait for the parent repository's Git config; the sibling creations it excludes are short.
_PARENT_LOCK_TIMEOUT_S = 300.0
_ABSOLUTE_POINTER = re.compile(r"^(/|[A-Za-z]:)")


class WorktreeSetupError(RuntimeError):
    """A setup step failed; the message names the cause."""


def mark_fusesoc_ignore(booley_dir: Path, *, paired: bool) -> None:
    """Keep the worktrees' ``.core`` copies out of FuseSoC's recursive core scan.

    Each worktree is a full checkout carrying the project's ``.core`` files, so a
    stale copy could shadow the source at the Project Root. The marker suppresses
    ``.core`` discovery only. A *paired* (Versioned) Booley Directory may have no
    committed root marker: it is quarantined from ``worktrees/`` instead, without
    adding an untracked input to the clean source. An existing marker is kept and
    a write that fails is ignored.
    """
    booley_dir.mkdir(parents=True, exist_ok=True)
    marker = booley_dir / "FUSESOC_IGNORE"
    if paired and not marker.is_file():
        (booley_dir / "worktrees").mkdir(exist_ok=True)
        marker = booley_dir / "worktrees" / "FUSESOC_IGNORE"
    if not marker.is_file():
        with suppress(OSError):
            marker.write_bytes(_MARKER_LINE)


@contextmanager
def _parent_git_lock(booley_dir: Path) -> Iterator[None]:
    """Serialize writes to the parent repository's Git config with concurrent creations."""
    folder = booley_dir / "worktrees" / ".locks"
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "_parent_git.lock").open("a+", encoding="utf-8") as handle:
        wait_for_file_lock(handle, timeout_s=_PARENT_LOCK_TIMEOUT_S)
        try:
            yield
        finally:
            release_file_lock(handle)


def _git(cwd: Path, *args: str, timeout: float = _GIT_TIMEOUT_S) -> str:
    try:
        result = run_captured_bounded(
            ["git", *args], timeout=timeout, cwd=cwd, env=inherited_git_environment()
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        detail = str(exc)
    else:
        if result.returncode == 0:
            return result.stdout.strip()
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
    raise WorktreeSetupError(f"git {' '.join(args)} failed in {cwd}: {detail}")


def _relative_paths_policy(project_root: Path) -> bool:
    """Whether the repository asks Git for relative worktree pointers (unset is no)."""
    result = run_git(project_root, "config", "--bool", "--get", "worktree.useRelativePaths")
    return result.stdout.strip() == "true"


def _pointers_are_relative(git_dir: Path, worktree: Path, configured: str) -> bool:
    pointers = (
        (worktree / ".git").read_text(encoding="utf-8").strip().removeprefix("gitdir: "),
        (git_dir / "gitdir").read_text(encoding="utf-8").strip(),
        configured,
    )
    return not any(_ABSOLUTE_POINTER.match(pointer) for pointer in pointers)


def _renormalize_needed(worktree: Path, explicit: list[str]) -> bool:
    """Whether ``core.autocrlf`` is set to something other than false before we force it off."""
    result = run_git(worktree, *explicit, "config", "--type=bool-or-str", "--get", "core.autocrlf")
    return result.returncode == 0 and result.stdout.strip() != "false"


def configure_worktree_git(project_root: Path, worktree: Path, *, booley_dir: Path) -> None:
    """Apply the per-worktree Git settings and the ``[agent.git]`` identity to *worktree*.

    *project_root* is the checkout the worktree was added from and *booley_dir* its
    Booley Directory, which holds the lock and the identity. The settings keep
    submodule recursion off (Python fills gitlinks after final branch selection) and
    force LF line endings, so host and Sandbox agree; ``git checkout -f`` then
    renormalizes the files, but only when ``core.autocrlf`` was on, and never over
    uncommitted changes. The private Git directory
    is addressed explicitly, because a shared config polluted with
    ``core.worktree=/work`` makes plain ``git -C <worktree>`` refuse to run.

    Raises:
        WorktreeSetupError: a Git command or the identity fails, a relative-path
            policy left an absolute pointer, or the files need renormalizing and
            have uncommitted changes.
    """
    try:
        git_dir = git_directories(worktree).git_dir
    except GitDirectoryInspectionError as exc:
        raise WorktreeSetupError(str(exc)) from exc
    explicit = [f"--git-dir={git_dir}", f"--work-tree={worktree}"]
    relative = os.path.relpath(os.path.realpath(worktree), os.path.realpath(git_dir))
    with _parent_git_lock(booley_dir):
        renormalize = _renormalize_needed(worktree, explicit)
        if renormalize and _git(
            worktree, *explicit, "status", "--porcelain", "--untracked-files=no"
        ):
            raise WorktreeSetupError(
                f"{worktree} has uncommitted changes and its line endings need "
                "renormalizing; commit them and run the command again"
            )
        _git(project_root, "config", "extensions.worktreeConfig", "true")
        for key, value in (
            ("core.worktree", relative.replace(os.sep, "/")),
            ("submodule.recurse", "false"),
            ("diff.ignoreSubmodules", "all"),
            ("status.submoduleSummary", "false"),
            ("core.autocrlf", "false"),
        ):
            _git(worktree, *explicit, "config", "--worktree", key, value)
        if renormalize:
            _git(worktree, *explicit, "checkout", "-f", timeout=_CHECKOUT_TIMEOUT_S)
        if _relative_paths_policy(project_root) and not _pointers_are_relative(
            git_dir,
            worktree,
            _git(worktree, *explicit, "config", "--worktree", "--get", "core.worktree"),
        ):
            raise WorktreeSetupError("relative worktree policy produced non-portable metadata")
        try:
            apply_git_identity(worktree, load_git_identity(booley_dir))
        except GitIdentityError as exc:
            raise WorktreeSetupError(str(exc)) from exc


def copy_root_fusesoc_ignore(project_root: Path, worktree: Path) -> None:
    """Preserve an untracked root FuseSoC quarantine marker in a linked worktree."""
    marker = project_root / "FUSESOC_IGNORE"
    if marker.is_file():
        shutil.copy2(marker, worktree / marker.name)


def set_up_linked_worktree(project_root: Path, worktree: Path, *, paired: bool) -> list[str]:
    """Set up the outer worktree *worktree* added from *project_root*; return the paths left out.

    Applies the Git settings, then copies the Snapshot of the Main Booley Directory
    into the worktree's own Booley Directory unless a Paired Checkout takes its
    place (*paired*), then the root FuseSoC marker. The returned paths are the
    unclassified ones the Snapshot named and did not copy; none when *paired*.

    Raises:
        WorktreeSetupError: a step failed; the caller rolls the worktree back.
    """
    booley_dir = own_booley_dir(project_root)
    configure_worktree_git(project_root, worktree, booley_dir=booley_dir)
    # The new worktree's own root booley.toml (it may come from another branch) says
    # where it expects its Booley Directory.
    try:
        relative = own_booley_dir_relative(worktree)
    except (ValueError, BooleyLayoutError) as exc:
        raise WorktreeSetupError(str(exc)) from exc
    skipped: list[str] = []
    if not paired:
        try:
            skipped = copy_snapshot(booley_dir, worktree / relative)
        except SnapshotError as exc:
            raise WorktreeSetupError(str(exc)) from exc
    copy_root_fusesoc_ignore(project_root, worktree)
    return skipped

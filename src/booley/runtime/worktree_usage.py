"""Interactive worktree naming and the last-used stamp.

``booley worktree new <name>`` creates ``<Main Booley Directory>/worktrees/<name>``.
Every Booley tool call routed to such a worktree touches
``<Main Booley Directory>/worktrees/.last-used/<name>``; the stamp is that file's
mtime, so one atomic ``utime`` records it and nothing needs parsing. Names cannot
start with ``.``, so the stamp directory never collides with a worktree.

A Session handle ``S<N>`` is tied to the worktree name ``s<N>`` (ADR 0068): the
handle outlives its worktree's removal by at most one prune, and a number is
never handed out again while that worktree or one of its branches exists.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from collections.abc import Callable
from pathlib import Path

from booley.core.booley_dir import owning_checkout
from booley.runtime.git_environment import inherited_git_environment
from booley.runtime.safe_storage import refuse_symlinks

logger = logging.getLogger(__name__)

# One path component: letters, digits, '.', '_' or '-', starting with a letter or digit.
# The packaged worktree_create.sh enforces the same rule.
WORKTREE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")

_STAMP_DIR = ".last-used"
# Branch namespaces of an interactive worktree: the RTL repository holds
# ``booley/<name>``, the Versioned Booley Directory ``booley-worktree/<name>``.
_BRANCH_PREFIXES = ("booley/", "booley-worktree/")
# Allocation runs under the Session writer lock that every tool call's registry
# upsert waits on, so Git gets far less than the usual 30 s.
_GIT_TIMEOUT_SECONDS = 5
_SESSION_LABEL = re.compile(r"S([0-9]+)")


def interactive_worktree_name(path: Path, common: Path) -> str | None:
    """Return the worktree name when *path* is inside ``<common>/worktrees/<name>``.

    Only a linked worktree (its ``.git`` is a file) directly under ``worktrees/``
    counts. Both sides are resolved so the Sandbox spelling of the Main Booley
    Directory compares equal to the host one.
    """
    try:
        relative = path.resolve().relative_to((common / "worktrees").resolve())
    except ValueError:
        return None
    if not relative.parts:
        return None
    name = relative.parts[0]
    if not WORKTREE_NAME.fullmatch(name) or not (common / "worktrees" / name / ".git").is_file():
        return None
    return name


def stamp_worktree(common: Path, name: str) -> None:
    """Record now as worktree *name*'s last use."""
    stamp = common / "worktrees" / _STAMP_DIR / name
    refuse_symlinks(stamp, booley_dir=common)
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.touch()


def record_worktree_use(work_dir: Path, common: Path) -> None:
    """Stamp the interactive worktree containing *work_dir*, if it is one."""
    name = interactive_worktree_name(work_dir, common)
    if name is not None:
        stamp_worktree(common, name)


def last_used(common: Path, name: str) -> float | None:
    """Return worktree *name*'s last-used time (epoch seconds), or ``None`` if never."""
    try:
        return (common / "worktrees" / _STAMP_DIR / name).stat().st_mtime
    except FileNotFoundError:
        return None


def handle_worktree(common: Path, label: str) -> Path | None:
    """Return the worktree directory a Session handle *label* is tied to, else ``None``.

    ``S<N>`` maps to ``<common>/worktrees/s<N>``; any other label (a Job handle) has none.
    """
    match = _SESSION_LABEL.fullmatch(label)
    return None if match is None else common / "worktrees" / f"s{match[1]}"


def _branch_names(repository: Path) -> set[str]:
    """Interactive-worktree branch names in *repository*; empty when Git cannot say.

    A failure (not a repository, Git missing, timeout, unsafe repository) is
    logged and reads as "no branches": allocating a handle must never fail
    because Git is unavailable.
    """
    command = ["git", "for-each-ref", "--format=%(refname)"]
    command += [f"refs/heads/{prefix}" for prefix in _BRANCH_PREFIXES]
    try:
        result = subprocess.run(
            command,
            cwd=repository,
            env={**inherited_git_environment(), "GIT_OPTIONAL_LOCKS": "0"},
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("Git branch list unavailable in %s: %s", repository, exc)
        return set()
    if result.returncode != 0:
        logger.warning("Git branch list unavailable in %s: %s", repository, result.stderr.strip())
        return set()
    return {line.removeprefix("refs/heads/") for line in result.stdout.split()}


def _taken_by_branch(names: set[str], number: int) -> bool:
    """Whether a branch is, or lies below, ``booley/s<N>`` or ``booley-worktree/s<N>``.

    Git refuses ``worktree add -b booley/s1`` while ``booley/s1/x`` exists, so a
    nested branch occupies the name as well.
    """
    stems = tuple(f"{prefix}s{number}" for prefix in _BRANCH_PREFIXES)
    return any(name == stem or name.startswith(stem + "/") for name in names for stem in stems)


def session_number_taken(common: Path) -> Callable[[int], bool]:
    """Return a predicate: whether handle number *N* is occupied by worktree ``s<N>``.

    Occupied means ``<common>/worktrees/s<N>`` exists in any form (a half-created
    or dangling entry still holds the name), or a ``booley/s<N>`` or
    ``booley-worktree/s<N>`` branch exists (or one below either name) in the RTL
    checkout owning *common* or in *common* itself when it is a Versioned
    Booley Directory. Branches are read on first use and cached, so one
    allocation costs at most two Git calls.
    """
    branches: set[str] | None = None

    def taken(number: int) -> bool:
        nonlocal branches
        if os.path.lexists(common / "worktrees" / f"s{number}"):
            return True
        if branches is None:
            repositories = [owning_checkout(common)]
            repositories.append(common if (common / ".git").is_dir() else None)
            branches = set()
            for repository in repositories:
                if repository is not None:
                    branches |= _branch_names(repository)
        return _taken_by_branch(branches, number)

    return taken

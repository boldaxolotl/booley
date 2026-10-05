"""Commit one file's content onto the checked-out branch without disturbing other work.

:func:`commit_file` runs this sequence:

1. hash the source file into the object database;
2. on each attempt, read the checked-out branch and its head; a head that
   already holds the same blob at the path needs nothing, and a head holding
   other content there is refused;
3. stage that one path in the user's index, so a commit the user makes
   meanwhile carries the file too;
4. build a commit of the head's tree plus that blob, using a private index so
   the user's other staged work is never committed;
5. compare-and-swap the branch from the head the commit was built on; a branch
   that moved is rebuilt on its new head, at most :data:`COMMIT_ATTEMPTS` times.

It commits onto whatever head is checked out at each attempt and gives no
"validated head" guarantee; a caller that needs one must build it on top.

Idempotent in the sense that repeating the call after a success (or after a
crash at any step) commits nothing new while the checked-out head still holds
the same blob at the path, wherever the branch has moved since.

No lock held by Booley can exclude manual Git use. In particular, a user who
checks out a sibling branch at the same commit between the branch read and the
head read makes the commit land on the branch read first. Callers that need
more bind the branch with ``check_branch`` and re-verify after the call returns.

Callers own policy: which repository and path, the commit message, and any
per-attempt branch check. This module knows nothing about the files it commits.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path

# A branch that moves under us this many times in one call is left for the
# caller to retry later rather than retried forever.
COMMIT_ATTEMPTS = 3
_GIT_TIMEOUT_S = 60
_DEFAULT_REFLOG_MESSAGE = "booley: commit file"

BuildCommit = Callable[[Path, "str | None", str, str, str], str]
"""``(repository, parent, path, blob, message) -> commit``; see :func:`build_commit`."""

SwapBranch = Callable[[Path, str, str, "str | None"], bool]
"""``(repository, ref, commit, expected) -> moved``; see :func:`compare_and_swap`."""


class FileCommitError(RuntimeError):
    """The file could not be committed now; repeating the call is safe."""


class DetachedHeadError(FileCommitError):
    """The repository has no checked-out branch to commit onto."""

    def __init__(self, repository: Path) -> None:
        super().__init__(f"{repository} has a detached HEAD; check out a branch to commit onto")
        self.repository = repository


class BranchKeptMovingError(FileCommitError):
    """The branch moved under every attempt."""

    def __init__(self, repository: Path) -> None:
        super().__init__(
            f"branch in {repository} kept moving for {COMMIT_ATTEMPTS} attempts; "
            "nothing was published"
        )
        self.repository = repository


# Git -----------------------------------------------------------------------------


def git(
    repository: Path, *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run ``git`` in *repository*; raise :class:`FileCommitError` if it cannot run."""
    try:
        return subprocess.run(
            ["git", *args],
            cwd=repository,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
            check=False,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise FileCommitError(f"git {' '.join(args)} failed in {repository}: {exc}") from exc


def require_git(repository: Path, *args: str, env: dict[str, str] | None = None) -> str:
    """Return stripped stdout, or raise :class:`FileCommitError` when git fails."""
    result = git(repository, *args, env=env)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip() or "no diagnostic"
        raise FileCommitError(f"git {' '.join(args)} failed in {repository}: {detail}")
    return result.stdout.strip()


def optional_git(repository: Path, *args: str) -> str | None:
    """Return stdout, or ``None`` when git reports the object or ref is absent."""
    result = git(repository, *args)
    return result.stdout.strip() if result.returncode == 0 else None


# Inspection ----------------------------------------------------------------------


def checked_out_commit(repository: Path) -> str | None:
    """Return the checked-out commit, or ``None`` on an unborn branch."""
    return optional_git(repository, "rev-parse", "-q", "--verify", "HEAD^{commit}")


def _checked_out_branch(repository: Path) -> str:
    """Return the checked-out branch ref; committing needs one."""
    ref = optional_git(repository, "symbolic-ref", "-q", "HEAD")
    if not ref:
        raise DetachedHeadError(repository)
    return ref


def tree_blob(repository: Path, commit: str | None, path: str) -> str | None:
    """Return the blob *commit* holds at *path*, or ``None`` when it holds none."""
    if commit is None:
        return None
    return optional_git(repository, "rev-parse", "-q", "--verify", f"{commit}:{path}")


def _index_blob(repository: Path, path: str) -> str | None:
    line = require_git(repository, "ls-files", "-s", "--", path)
    # "<mode> <object> <stage>\t<path>"
    return line.split()[1] if line else None


# Commit steps ---------------------------------------------------------------------


def build_commit(repository: Path, parent: str | None, path: str, blob: str, message: str) -> str:
    """Return a commit of *parent*'s tree plus *blob* at *path*, via a private index."""
    with tempfile.TemporaryDirectory(prefix="booley-history-") as scratch:
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(scratch) / "index")}
        if parent is None:
            require_git(repository, "read-tree", "--empty", env=env)
        else:
            require_git(repository, "read-tree", parent, env=env)
        require_git(
            repository, "update-index", "--add", "--cacheinfo", f"100644,{blob},{path}", env=env
        )
        tree = require_git(repository, "write-tree", env=env)
    parents = ["-p", parent] if parent is not None else []
    return require_git(repository, "commit-tree", tree, *parents, "-m", message)


def compare_and_swap(
    repository: Path,
    ref: str,
    commit: str,
    expected: str | None,
    *,
    reflog_message: str = _DEFAULT_REFLOG_MESSAGE,
) -> bool:
    """Move *ref* from *expected* to *commit*; return ``False`` if it had moved."""
    old = expected if expected is not None else "0" * len(commit)
    result = git(repository, "update-ref", "-m", reflog_message, ref, commit, old)
    if result.returncode == 0:
        return True
    current = optional_git(repository, "rev-parse", "-q", "--verify", ref)
    if current == commit:
        return True
    if current != expected:
        return False
    detail = (result.stderr or result.stdout).strip() or "no diagnostic"
    raise FileCommitError(f"git update-ref {ref} failed in {repository}: {detail}")


def _stage(repository: Path, path: str, blob: str) -> None:
    """Stage *blob* at *path* in the user's index (only this one path)."""
    if _index_blob(repository, path) == blob:
        return
    require_git(repository, "update-index", "--add", "--cacheinfo", f"100644,{blob},{path}")


# Entry point ----------------------------------------------------------------------


def commit_file(
    repository: Path,
    relative_path: str,
    source: Path,
    message: str,
    *,
    check_branch: Callable[[str], None] | None = None,
    build: BuildCommit = build_commit,
    swap: SwapBranch = compare_and_swap,
) -> str:
    """Commit *source*'s content at *relative_path*; return the commit holding it.

    *relative_path* is the repository-relative POSIX path. See the module
    docstring for the sequence and its guarantees.
    *check_branch* runs with the checked-out branch ref at the start of every
    attempt, before anything is staged or built; whatever it raises propagates.

    *build* and *swap* are injection seams replacing :func:`build_commit` and
    :func:`compare_and_swap`: for fault injection in tests, and for a caller
    that needs its own reflog message. They must keep those functions'
    contracts.

    Raises :class:`FileCommitError` (or a subclass) when the file cannot be
    committed now.
    """
    blob = require_git(repository, "hash-object", "-w", "--", str(source))
    for _attempt in range(COMMIT_ATTEMPTS):
        ref = _checked_out_branch(repository)
        if check_branch is not None:
            check_branch(ref)
        head = checked_out_commit(repository)
        committed = tree_blob(repository, head, relative_path)
        if committed == blob:
            assert head is not None  # a blob was found in it
            return head
        if committed is not None:
            raise FileCommitError(
                f"{relative_path} is already committed with other content; inspect it by hand"
            )
        _stage(repository, relative_path, blob)
        commit = build(repository, head, relative_path, blob, message)
        if swap(repository, ref, commit, head):
            return commit
    raise BranchKeptMovingError(repository)

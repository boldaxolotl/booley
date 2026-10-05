"""Commit one file's content onto the checked-out branch without disturbing other work.

:func:`commit_file` has two modes.

**Unpinned** (``expected_head=None``) commits onto whatever the branch points at:

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

Idempotent in the sense that repeating the call after a success (or after a
crash at any step) commits nothing new while the checked-out head still holds
the same blob at the path, wherever the branch has moved since.

**Pinned** (``expected_head=<commit>``) commits only directly onto a head the
caller validated. ``expected_head`` must be the full lowercase hex ID of an
existing commit, in the repository's object format; anything else (an
abbreviation, a revision expression such as ``HEAD``, a missing object, a
non-commit) raises :class:`InvalidExpectedHeadError` before Git writes
anything. An unborn branch is refused with :class:`HeadMismatchError`. After
the branch read and ``check_branch``:

* head == ``expected_head``: return it when it already holds the blob at the
  path; otherwise commit onto it (replacing any other content at the path),
  with no retry: a lost compare-and-swap raises :class:`HeadMismatchError`;
* head != ``expected_head``: return the head only when it is this call's own
  earlier result: a commit whose single parent is ``expected_head`` and whose
  tree differs from it at exactly the path, which holds the blob with mode
  100644. Anything else raises :class:`HeadMismatchError`.

Idempotent in the sense that repeating the call after a success (or after a
crash at any step) returns the same commit while the branch still points at
``expected_head`` or at that one-path child; once anything else lands, the
repeat is refused. A refusal never changes the user's index or any ref (a
lost compare-and-swap can leave an unreferenced commit object behind). Pinned
mode stages the path in the user's index only after its commit is on the
branch (or recognised there, so a crash between the two heals on repeat), and
only when the user's index entry for the path still equals ``expected_head``'s
(both absent counts); a user who staged something else there keeps it.

Neither mode can exclude manual Git use: a lock held by Booley does not stop a
user. In particular, a user who checks out a sibling branch at the same commit
between the branch read and the head read makes the commit land on the branch
read first (its parent is still the validated commit). Callers that need more
bind the branch with ``check_branch`` and re-verify after the call returns.

Callers own policy: which repository and path, the commit message, and any
per-attempt branch check. This module knows nothing about the files it commits.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path

# A branch that moves under us this many times in one call is left for the
# caller to retry later rather than retried forever.
COMMIT_ATTEMPTS = 3
_GIT_TIMEOUT_S = 60
_DEFAULT_REFLOG_MESSAGE = "booley: commit file"
_FILE_MODE = "100644"
_OID_LENGTHS = {"sha1": 40, "sha256": 64}
_LOWER_HEX = re.compile(r"[0-9a-f]+")

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
    """The branch moved under every attempt (unpinned mode)."""

    def __init__(self, repository: Path) -> None:
        super().__init__(
            f"branch in {repository} kept moving for {COMMIT_ATTEMPTS} attempts; "
            "nothing was published"
        )
        self.repository = repository


class InvalidExpectedHeadError(FileCommitError):
    """``expected_head`` is not the full ID of an existing commit (pinned mode)."""


class HeadMismatchError(FileCommitError):
    """The branch is not on, or moved away from, the head the caller validated."""

    def __init__(self, message: str, *, expected: str, actual: str | None) -> None:
        super().__init__(message)
        self.expected = expected
        self.actual = actual


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


def _literal_git(repository: Path, *args: str) -> str:
    """Return raw stdout of a command whose paths must match literally (no pathspec magic)."""
    result = git(repository, *args, env={**os.environ, "GIT_LITERAL_PATHSPECS": "1"})
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip() or "no diagnostic"
        raise FileCommitError(f"git {' '.join(args)} failed in {repository}: {detail}")
    return result.stdout


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
    expected_head: str | None = None,
    check_branch: Callable[[str], None] | None = None,
    build: BuildCommit = build_commit,
    swap: SwapBranch = compare_and_swap,
) -> str:
    """Commit *source*'s content at *relative_path*; return the commit holding it.

    *relative_path* is the repository-relative POSIX path. See the module
    docstring for the unpinned and pinned (*expected_head*) contracts.
    *check_branch* runs with the checked-out branch ref at the start of every
    attempt, before anything is staged or built; whatever it raises propagates.

    *build* and *swap* are injection seams replacing :func:`build_commit` and
    :func:`compare_and_swap`: for fault injection in tests, and for a caller
    that needs its own reflog message. They must keep those functions'
    contracts.

    Raises :class:`FileCommitError` (or a subclass) when the file cannot be
    committed now.
    """
    if expected_head is None:
        return _commit_unpinned(
            repository, relative_path, source, message, check_branch, build, swap
        )
    _validate_expected_head(repository, expected_head)
    blob = require_git(repository, "hash-object", "-w", "--", str(source))
    pinned = _Pinned(repository, relative_path, blob, expected_head)
    return pinned.commit(message, check_branch, build, swap)


def _commit_unpinned(
    repository: Path,
    path: str,
    source: Path,
    message: str,
    check_branch: Callable[[str], None] | None,
    build: BuildCommit,
    swap: SwapBranch,
) -> str:
    blob = require_git(repository, "hash-object", "-w", "--", str(source))
    for _attempt in range(COMMIT_ATTEMPTS):
        ref = _checked_out_branch(repository)
        if check_branch is not None:
            check_branch(ref)
        head = checked_out_commit(repository)
        committed = tree_blob(repository, head, path)
        if committed == blob:
            assert head is not None  # a blob was found in it
            return head
        if committed is not None:
            raise FileCommitError(
                f"{path} is already committed with other content; inspect it by hand"
            )
        _stage(repository, path, blob)
        commit = build(repository, head, path, blob, message)
        if swap(repository, ref, commit, head):
            return commit
    raise BranchKeptMovingError(repository)


# Pinned mode -----------------------------------------------------------------------


def _validate_expected_head(repository: Path, expected_head: str) -> None:
    """Refuse anything but the full canonical ID of an existing commit; reads only."""
    object_format = require_git(repository, "rev-parse", "--show-object-format")
    length = _OID_LENGTHS.get(object_format)
    if length is None:
        raise InvalidExpectedHeadError(f"unsupported object format {object_format!r}")
    if len(expected_head) != length or not _LOWER_HEX.fullmatch(expected_head):
        raise InvalidExpectedHeadError(
            f"expected_head {expected_head!r} is not a full lowercase {object_format} commit ID"
        )
    kind = optional_git(repository, "cat-file", "-t", expected_head)
    if kind != "commit":
        raise InvalidExpectedHeadError(
            f"expected_head {expected_head} names {kind or 'no object'} in {repository}, "
            "not a commit"
        )


class _Pinned:
    """One pinned commit of *blob* at *path* directly onto *expected*."""

    def __init__(self, repository: Path, path: str, blob: str, expected: str) -> None:
        self.repository = repository
        self.path = path
        self.blob = blob
        self.expected = expected

    def commit(
        self,
        message: str,
        check_branch: Callable[[str], None] | None,
        build: BuildCommit,
        swap: SwapBranch,
    ) -> str:
        ref = _checked_out_branch(self.repository)
        if check_branch is not None:
            check_branch(ref)
        head = checked_out_commit(self.repository)
        if head != self.expected:
            if head is None or not self._is_own_child(head):
                raise self._mismatch(
                    f"{self.repository} has {ref} at {head or 'no commit (unborn)'}, "
                    f"not at the validated head {self.expected} or the commit of "
                    f"{self.path} onto it; no ref was moved",
                    head,
                )
            self._stage_published()
            return head
        head = self.expected  # equal; narrows the type for the steps below
        if tree_blob(self.repository, head, self.path) == self.blob:
            return head
        commit = build(self.repository, head, self.path, self.blob, message)
        if not swap(self.repository, ref, commit, head):
            current = optional_git(self.repository, "rev-parse", "-q", "--verify", ref)
            raise self._mismatch(
                f"{ref} in {self.repository} moved from the validated head {self.expected} "
                f"to {current or 'nothing'} during the commit; no ref was moved",
                current,
            )
        self._stage_published()
        return commit

    def _mismatch(self, message: str, actual: str | None) -> HeadMismatchError:
        return HeadMismatchError(message, expected=self.expected, actual=actual)

    def _is_own_child(self, head: str) -> bool:
        """Whether *head* is exactly *expected* plus the blob at the path (mode 100644)."""
        parents = require_git(self.repository, "rev-list", "--parents", "-n", "1", head).split()
        if parents[1:] != [self.expected]:
            return False
        changed = _literal_git(
            self.repository, "diff-tree", "-r", "--no-renames", "--name-only", "-z",
            self.expected, head,
        )  # fmt: skip
        if changed != f"{self.path}\0":
            return False
        return self._tree_entry(head) == f"{_FILE_MODE} blob {self.blob}\t{self.path}\0"

    def _tree_entry(self, commit: str) -> str:
        """Return the raw ``ls-tree -z`` entry for the path in *commit* (``""`` if absent)."""
        return _literal_git(self.repository, "ls-tree", "-z", commit, "--", self.path)

    def _stage_published(self) -> None:
        """Stage the published blob unless the user staged something else at the path."""
        raw = _literal_git(self.repository, "ls-files", "-s", "-z", "--", self.path)
        published = f"{_FILE_MODE} {self.blob} 0\t{self.path}\0"
        if raw == published:
            return
        if raw != _as_index_entry(self._tree_entry(self.expected)):
            return  # the user's own staging at the path wins
        require_git(
            self.repository,
            "update-index",
            "--add",
            "--cacheinfo",
            f"{_FILE_MODE},{self.blob},{self.path}",
        )


def _as_index_entry(tree_entry: str) -> str:
    """Turn ``<mode> <type> <oid>\\t<path>\\0`` into ``<mode> <oid> 0\\t<path>\\0``."""
    if not tree_entry:
        return ""
    header, _tab, rest = tree_entry.partition("\t")
    mode, _kind, oid = header.split(" ")
    return f"{mode} {oid} 0\t{rest}"

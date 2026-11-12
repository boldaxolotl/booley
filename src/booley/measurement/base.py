"""The Base of a measured commit: how it is chosen, what changed since, which edits are protected.

Contract for the caller (``booley measure``, #3507; rendering is #3505): resolve the
Base with :func:`resolve_base`, list what the commit changed since it with
:func:`changed_files`, then report which of those changes touch the measured tree's
protected set (:func:`protected_edits`, built from ``protected_set``). Everything
reads Git objects of the live RTL repository, never its working tree, and only
reports: nothing here blocks a measurement.
"""

from __future__ import annotations

import os
from collections.abc import Collection
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from booley.runtime.commit_export import has_commit
from booley.runtime.pinned_history import FileCommitError, raw_git

_GITLINK_MODE = "160000"
_CONSTRAINT_SUFFIXES = (".sdc", ".xdc")


def is_constraint(path: str) -> bool:
    """Whether *path* is a timing constraint file (.sdc or .xdc, any case)."""
    return path.lower().endswith(_CONSTRAINT_SUFFIXES)


class BaseError(ValueError):
    """A ``--base`` that is not a commit or shares no history with the measured commit."""


class BaseChoice(StrEnum):
    """How the Base was chosen; the value is the human wording."""

    REMOTE_DEFAULT = "merge-base with origin/HEAD"
    LOCAL_MAIN = "merge-base with local main"
    REQUESTED = "--base, used as given"
    NORMALIZED = "--base, replaced by its merge-base with the measured commit"


# Tried in order; the first that resolves is the reference the default Base is cut from.
_DEFAULT_REFERENCES = (
    ("refs/remotes/origin/HEAD", "origin/HEAD", BaseChoice.REMOTE_DEFAULT),
    ("refs/heads/main", "local main", BaseChoice.LOCAL_MAIN),
)


@dataclass(frozen=True)
class Base:
    """The commit a measured commit is compared with, or why there is none."""

    commit: str | None  # full sha; None means "no base"
    choice: BaseChoice | None  # None only when no default reference exists
    requested: str | None  # the full sha ``--base`` named, when given
    no_base: str | None  # why there is no base; None when ``commit`` is set


def _rev(repository: Path, spec: str) -> str | None:
    """The commit *spec* names, or None when it does not resolve to one."""
    try:
        out = raw_git(
            repository,
            "rev-parse",
            "--verify",
            "--quiet",
            "--end-of-options",
            f"{spec}^{{commit}}",
        )
    except FileCommitError:
        return None
    return out.decode("ascii").strip()


def _merge_base(repository: Path, first: str, second: str) -> str | None:
    """The merge-base of two commits, or None when they share no history."""
    try:
        return raw_git(repository, "merge-base", first, second).decode("ascii").strip()
    except FileCommitError:
        return None


def _requested_base(repository: Path, commit: str, requested: str) -> Base:
    """Normalize a user-given ``--base`` to its merge-base with *commit*."""
    named = _rev(repository, requested)
    if named is None:
        raise BaseError(f"--base {requested}: not a commit in this repository")
    base = _merge_base(repository, named, commit)
    if base is None:
        raise BaseError(f"--base {requested}: shares no history with {commit[:12]}")
    choice = BaseChoice.REQUESTED if base == named else BaseChoice.NORMALIZED
    return _with_own_base_check(base, commit, choice, named)


def _with_own_base_check(
    base: str, commit: str, choice: BaseChoice | None, requested: str | None
) -> Base:
    """A commit that is its own base has no base."""
    if base == commit:
        return Base(
            None,
            choice,
            requested,
            f"the measured commit {commit[:12]} is its own base (it is on the base's history)",
        )
    return Base(base, choice, requested, None)


def resolve_base(repository: Path, commit: str, requested: str | None = None) -> Base:
    """Choose the Base of *commit* (a full commit id) in *repository*.

    Without *requested* the Base is the merge-base with ``origin/HEAD``, else with
    local ``main``; a commit on that branch itself has no Base, and so does a
    repository with neither reference. A *requested* revision is normalized to its
    merge-base with *commit*, and ``requested`` on the result names the revision.

    Raises:
        BaseError: *requested* is not a commit, or shares no history with *commit*.
    """
    if requested is not None:
        return _requested_base(repository, commit, requested)
    for ref, label, choice in _DEFAULT_REFERENCES:
        reference = _rev(repository, ref)
        if reference is None:
            continue
        base = _merge_base(repository, reference, commit)
        if base is None:
            return Base(None, choice, None, f"{label} shares no history with {commit[:12]}")
        return _with_own_base_check(base, commit, choice, None)
    return Base(None, None, None, "neither origin/HEAD nor local main exists")


@dataclass(frozen=True)
class ChangedFile:
    """One non-submodule path the commit changed since the Base."""

    path: str  # "/"-separated, relative to the checkout root
    status: str  # A, M, D or T (the ``git diff`` letter; renames are not detected)
    constraint: bool  # an .sdc or .xdc file: it constrains timing, so an edit matters


@dataclass(frozen=True)
class SubmoduleChange:
    """One submodule whose pinned commit moved: a single entry, never its files."""

    path: str
    old: str | None  # gitlink at the Base; None when added
    new: str | None  # gitlink at the measured commit; None when removed
    files: tuple[str, ...] | None  # changed paths inside, prefixed with ``path``; None = unknown


@dataclass(frozen=True)
class ChangedFiles:
    """What the measured commit changed since the Base, in Git's path order."""

    files: tuple[ChangedFile, ...]
    submodules: tuple[SubmoduleChange, ...]


def _split_nul(output: bytes) -> list[str]:
    return [os.fsdecode(part) for part in output.split(b"\0") if part]


def _submodule_files(
    repository: Path, path: str, old: str | None, new: str | None
) -> tuple[str, ...] | None:
    """Changed paths inside a submodule, or None when its objects are not available.

    Objects are looked for only in the initialized checkout at *path*, the same rule
    the commit export uses, so "unknown" means ``measure`` could not export it either.
    """
    inside = repository / path
    present = [oid for oid in (old, new) if oid is not None]
    if not (inside / ".git").exists() or not all(has_commit(inside, oid) for oid in present):
        return None
    if old is not None and new is not None:
        out = raw_git(inside, "diff-tree", "-r", "-z", "--name-only", "--no-renames", old, new)
    else:
        out = raw_git(inside, "ls-tree", "-r", "-z", "--name-only", present[0])
    return tuple(f"{path}/{name}" for name in _split_nul(out))


def changed_files(repository: Path, base: str, commit: str) -> ChangedFiles:
    """List what *commit* changed since *base* (already the merge-base: a three-dot diff).

    Plumbing ``diff-tree`` with renames off, so user diff configuration does not
    change the answer and ``.gitmodules`` ``ignore`` cannot hide a submodule bump.
    """
    out = raw_git(
        repository,
        "diff-tree",
        "-r",
        "-z",
        "--no-renames",
        "--no-abbrev",
        "--ignore-submodules=none",
        base,
        commit,
    )
    fields = _split_nul(out)
    files: list[ChangedFile] = []
    submodules: list[SubmoduleChange] = []
    for meta, path in zip(fields[0::2], fields[1::2], strict=True):
        old_mode, new_mode, old_oid, new_oid, status = meta.removeprefix(":").split()
        if _GITLINK_MODE in (old_mode, new_mode):
            old = old_oid if old_mode == _GITLINK_MODE else None
            new = new_oid if new_mode == _GITLINK_MODE else None
            submodules.append(
                SubmoduleChange(path, old, new, _submodule_files(repository, path, old, new))
            )
        else:
            files.append(ChangedFile(path, status[0], is_constraint(path)))
    return ChangedFiles(tuple(files), tuple(submodules))


def is_protected(path: str, protected: Collection[str]) -> bool:
    """Whether *path* is a protected file, or lies under a protected ``dir/`` prefix."""
    return any(
        path == entry or (entry.endswith("/") and path.startswith(entry)) for entry in protected
    )


def _submodule_is_protected(change: SubmoduleChange, protected: Collection[str]) -> bool:
    if is_protected(change.path, protected):
        return True
    if change.files is None:
        # The changed files are unknown, so any protected entry inside could have changed.
        return any(entry.startswith(f"{change.path}/") for entry in protected)
    return any(is_protected(name, protected) for name in change.files)


def protected_edits(changed: ChangedFiles, protected: Collection[str]) -> tuple[str, ...]:
    """Changed paths that are protected: files in diff order, then submodule paths."""
    edits = [file.path for file in changed.files if is_protected(file.path, protected)]
    edits.extend(
        change.path for change in changed.submodules if _submodule_is_protected(change, protected)
    )
    return tuple(edits)

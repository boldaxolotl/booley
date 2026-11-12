"""The Measurement Key: what a stored Measurement Run is filed under (ADR 0092, #3045).

A run belongs to the Git tree of the measured commit and, because its rules live
in the Booley Directory, to the tree of the Versioned Booley Directory commit the
rules were read from. Commit id, message and parents are recorded, never keyed, so
an amended commit with the same tree finds the same runs, and changing only a rule
changes the key while the project tree (the base lookup) stays the same.

Whether the rules come from a Versioned Booley Directory is decided by the
invoking checkout's structure, as in the commit export; only the in-tree
placement is read from the measured commit.

Contract for the caller (``booley measure``, #3507): resolve the key without
``booley_commit`` for the lookup before the export, then again with
``booley_commit=CommitExport.paired_commit`` for the run it stores, so a stored
key names the Booley Directory tree the rules were actually read from even if
the Paired Checkout's ``HEAD`` moved in between.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

from booley.core.booley_dir import BooleyDirOutsideCheckoutError, booley_dir_relative
from booley.core.boundary import as_dict, as_str
from booley.runtime.commit_export import versioned_booley_dir
from booley.runtime.pinned_history import FileCommitError, raw_git, validate_pin


class RulesNotVersionedError(ValueError):
    """The rules are not under version control; the message starts ``rules must be versioned``."""


@dataclass(frozen=True)
class MeasurementKey:
    project_tree: str  # ``<commit>^{tree}`` of the RTL repository
    booley_tree: str | None  # tree of the Versioned Booley Directory commit, else None


@dataclass(frozen=True)
class KeyedCommit:
    key: MeasurementKey
    commit: str  # the full sha that was measured
    message: str
    parents: tuple[str, ...]
    booley_commit: str | None  # the Versioned Booley Directory commit the rules come from


def _text(repository: Path, *args: str) -> str:
    return raw_git(repository, *args).decode("utf-8", "replace").strip()


def _in_tree(repository: Path, commit: str, path: str) -> bool:
    try:
        raw_git(repository, "cat-file", "-e", f"{commit}:{path}")
    except FileCommitError:
        return False
    return True


def _configured_dir_at(repository: Path, commit: str) -> str:
    """``[project].dir`` from the commit's root ``booley.toml``; ``""`` when absent or unreadable."""
    try:
        raw = raw_git(repository, "cat-file", "blob", f"{commit}:booley.toml")
        config = tomllib.loads(raw.decode("utf-8"))
    except (FileCommitError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return ""
    return as_str((as_dict(config.get("project")) or {}).get("dir")) or ""


def _require_rules_in_commit(repository: Path, commit: str) -> None:
    """Fail unless the commit's own tree holds the Project's ``booley.toml``."""
    try:
        place = booley_dir_relative(_configured_dir_at(repository, commit))
    except BooleyDirOutsideCheckoutError as exc:
        raise RulesNotVersionedError(
            "rules must be versioned: the Booley Directory is neither in the commit's "
            f"tree nor its own Git repository ({exc})"
        ) from exc
    names = (f"{place.as_posix()}/booley.toml", f"{place.as_posix()}/pipeline.toml")
    if not any(_in_tree(repository, commit, name) for name in names):
        raise RulesNotVersionedError(
            f"rules must be versioned: {names[0]} is not in commit {commit[:12]}"
        )


def resolve_measurement_key(
    repository: Path, commit: str, *, booley_commit: str | None = None
) -> KeyedCommit:
    """Return the key and recorded identity of *commit* (a full commit id) in *repository*.

    Raises:
        RulesNotVersionedError: the rules are not in the measured tree and there is
            no Versioned Booley Directory (``rules must be versioned``).
        ValueError: ``booley_commit`` was given without a Versioned Booley Directory.
        FileCommitError: Git cannot resolve the commit.
    """
    validate_pin(repository, commit)
    project_tree = _text(repository, "rev-parse", "--verify", f"{commit}^{{tree}}")
    versioned = versioned_booley_dir(repository)
    booley_tree: str | None = None
    if versioned is None:
        if booley_commit is not None:
            raise ValueError("booley_commit needs a Versioned Booley Directory")
        _require_rules_in_commit(repository, commit)
    else:
        if booley_commit is not None:
            validate_pin(versioned, booley_commit)
        else:
            try:
                booley_commit = _text(versioned, "rev-parse", "--verify", "HEAD^{commit}")
            except FileCommitError as exc:
                raise RulesNotVersionedError(
                    f"rules must be versioned: the Versioned Booley Directory has no commit ({exc})"
                ) from exc
        booley_tree = _text(versioned, "rev-parse", "--verify", f"{booley_commit}^{{tree}}")
    message = raw_git(repository, "log", "-1", "--format=%B", commit).decode("utf-8", "replace")
    parents = tuple(_text(repository, "rev-parse", f"{commit}^@").split())
    return KeyedCommit(
        MeasurementKey(project_tree, booley_tree), commit, message, parents, booley_commit
    )

"""The rules diff of a measured commit and the header state it sets (ADR 0092, #3045, #3697).

Contract for the caller (``booley measure``, #3507; rendering is #3505): resolve the
Base with ``base.resolve_base``, compute :func:`rules_diff` of the measured
:class:`~booley.measurement.run_key.KeyedCommit`, and read the amber reasons with
:func:`header_amber`. With a Versioned Booley Directory, also list the protected-path
edits committed there with :func:`paired_protected_edits`; edits in the RTL repository
are listed by ``base.protected_edits`` as before.

Everything reads Git objects, never a working tree, and only reports: nothing here
blocks a measurement. ``metrics.toml`` rules are compared as written (tables by Target
key and Flow, entries by metric, limit and prefix, after defaults, ``off`` and notes),
so respelling a key shows as a removal and an addition and errs towards amber.
``tests.toml`` is not classified: any change is reported with its diff.
"""

from __future__ import annotations

import difflib
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path, PurePosixPath

from booley.core.booley_dir import BooleyDirOutsideCheckoutError
from booley.measurement.base import (
    Base,
    ChangedFiles,
    changed_files,
    protected_edits,
    resolve_base,
)
from booley.measurement.metrics_file import (
    METRICS_FILE,
    Bound,
    MetricsFileError,
    TableRules,
    WrittenRules,
    parse_metrics_as_written,
)
from booley.measurement.run_key import KeyedCommit, booley_dir_at
from booley.runtime.commit_export import versioned_booley_dir
from booley.runtime.pinned_history import FileCommitError, raw_git

TESTS_FILE = "tests.toml"
NO_NOTE = "(no note)"


class Direction(StrEnum):
    """Whether a change makes the rules stricter, weaker, or neither."""

    TIGHTENED = "tightened"
    LOOSENED = "loosened"
    NEUTRAL = "neutral"


class ChangeKind(StrEnum):
    """What changed in ``metrics.toml``; the value is the human wording."""

    BOUND_ADDED = "bound added"
    TABLE_ADDED = "table added"
    MAX_LOWERED = "maximum lowered"
    MIN_RAISED = "minimum raised"
    MAX_RAISED = "maximum raised"
    MIN_LOWERED = "minimum lowered"
    BOUND_REMOVED = "bound removed"
    TURNED_OFF = "bound turned off"
    MOVED_TO_TRACK = "bound moved to track"
    TABLE_REMOVED = "table removed"
    TRACK_EDIT = "track edited"
    NOTES_EDIT = "note edited"


_DIRECTION: dict[ChangeKind, Direction] = {
    ChangeKind.BOUND_ADDED: Direction.TIGHTENED,
    ChangeKind.TABLE_ADDED: Direction.TIGHTENED,
    ChangeKind.MAX_LOWERED: Direction.TIGHTENED,
    ChangeKind.MIN_RAISED: Direction.TIGHTENED,
    ChangeKind.MAX_RAISED: Direction.LOOSENED,
    ChangeKind.MIN_LOWERED: Direction.LOOSENED,
    ChangeKind.BOUND_REMOVED: Direction.LOOSENED,
    ChangeKind.TURNED_OFF: Direction.LOOSENED,
    ChangeKind.MOVED_TO_TRACK: Direction.LOOSENED,
    ChangeKind.TABLE_REMOVED: Direction.LOOSENED,
    ChangeKind.TRACK_EDIT: Direction.NEUTRAL,
    ChangeKind.NOTES_EDIT: Direction.NEUTRAL,
}


@dataclass(frozen=True)
class RuleChange:
    """One change to the rules of one table; keys are as written in the file."""

    target: str
    flow: str
    key: str | None  # the bound or tracked key; None for a whole table
    kind: ChangeKind
    old: int | float | None
    new: int | float | None
    note: str | None  # the note of the bound in the measured commit, if any

    @property
    def direction(self) -> Direction:
        """Tightened, loosened or neutral."""
        return _DIRECTION[self.kind]

    @property
    def note_text(self) -> str | None:
        """The note a loosened change shows: its note, or ``"(no note)"``."""
        if self.direction is Direction.LOOSENED:
            return self.note or NO_NOTE
        return self.note


class AmberReason(StrEnum):
    """The only two things that turn the header amber; the value is the wording."""

    RULES_LOOSENED = "rules loosened"
    TESTS_CHANGED = "tests changed"


@dataclass(frozen=True)
class RulesDiff:
    """What changed in the rules between the Base and the measured commit."""

    changes: tuple[RuleChange, ...]  # empty when the rules could not be compared
    tests_diff: str | None  # unified diff of ``tests.toml``; None when unchanged
    unreadable: str | None  # why a side could not be read; None when both were
    paired_base: Base | None  # Versioned Booley Directory only: how its Base was chosen


# --- classification ----------------------------------------------------------------------


def _row(table: TableRules, kind: ChangeKind) -> RuleChange:
    return RuleChange(table.target, table.flow, None, kind, None, None, None)


def _bound_row(
    table: TableRules,
    bound: Bound,
    kind: ChangeKind,
    old: int | float | None,
    new: int | float | None,
    note: str | None,
) -> RuleChange:
    return RuleChange(table.target, table.flow, bound.key, kind, old, new, note)


def _changed_value(table: TableRules, before: Bound, after: Bound) -> RuleChange | None:
    """The change from bound *before* to *after* at one address, or None when none."""
    if before.value != after.value:
        lowered = after.value < before.value
        if after.limit == "max":
            kind = ChangeKind.MAX_LOWERED if lowered else ChangeKind.MAX_RAISED
        else:
            kind = ChangeKind.MIN_LOWERED if lowered else ChangeKind.MIN_RAISED
        return _bound_row(table, after, kind, before.value, after.value, after.note)
    if before.note != after.note:
        return _bound_row(
            table, after, ChangeKind.NOTES_EDIT, after.value, after.value, after.note
        )
    return None


def _bound_changes(
    before: TableRules,
    after: TableRules,
    switched_off: frozenset[tuple[str, str | None, str | None]],
) -> list[RuleChange]:
    """Bounds added, removed, or changed; bounds that moved to ``track`` are told apart."""
    old = {(bound.metric, bound.limit, bound.prefix): bound for bound in before.bounds}
    new = {(bound.metric, bound.limit, bound.prefix): bound for bound in after.bounds}
    tracked = {(item.metric, item.prefix) for item in after.tracked}
    changes: list[RuleChange] = []
    for address, bound in new.items():
        previous = old.get(address)
        if previous is None:
            changes.append(
                _bound_row(after, bound, ChangeKind.BOUND_ADDED, None, bound.value, bound.note)
            )
        elif change := _changed_value(after, previous, bound):
            changes.append(change)
    for address, bound in old.items():
        if address in new:
            continue
        if address in switched_off:
            kind = ChangeKind.TURNED_OFF
        elif (bound.metric, bound.prefix) in tracked:
            kind = ChangeKind.MOVED_TO_TRACK
        else:
            kind = ChangeKind.BOUND_REMOVED
        changes.append(_bound_row(before, bound, kind, bound.value, None, None))
    return changes


def _track_changes(before: TableRules, after: TableRules) -> list[RuleChange]:
    """``track`` entries added or removed, except those that traded places with a bound."""
    old = {(item.metric, item.prefix): item for item in before.tracked}
    new = {(item.metric, item.prefix): item for item in after.tracked}
    bounded_before = {(bound.metric, bound.prefix) for bound in before.bounds}
    bounded_after = {(bound.metric, bound.prefix) for bound in after.bounds}
    changes: list[RuleChange] = []
    for slot, item in new.items():
        if slot not in old and slot not in bounded_before:
            changes.append(
                RuleChange(
                    after.target, after.flow, item.key, ChangeKind.TRACK_EDIT, None, None, None
                )
            )
    for slot, item in old.items():
        if slot not in new and slot not in bounded_after:
            changes.append(
                RuleChange(
                    before.target, before.flow, item.key, ChangeKind.TRACK_EDIT, None, None, None
                )
            )
    return changes


def _classify(before: WrittenRules, after: WrittenRules) -> tuple[RuleChange, ...]:
    """Every change from *before* to *after*: head table order, then removed tables."""
    old = {(table.target, table.flow): table for table in before.tables}
    present = {(table.target, table.flow) for table in after.tables}
    changes: list[RuleChange] = []
    for table in after.tables:
        key = (table.target, table.flow)
        previous = old.get(key)
        if previous is None:
            changes.append(_row(table, ChangeKind.TABLE_ADDED))
            continue
        off = after.switched_off.get(key, frozenset())
        changes.extend(_bound_changes(previous, table, off))
        changes.extend(_track_changes(previous, table))
    changes.extend(
        _row(table, ChangeKind.TABLE_REMOVED)
        for table in before.tables
        if (table.target, table.flow) not in present
    )
    return tuple(changes)


# --- reading the two commits ------------------------------------------------------------


@dataclass(frozen=True)
class _Side:
    """The two files of one commit: ``None`` content means the file is absent."""

    label: str  # "base" or "measured"
    metrics: bytes | None
    tests: bytes | None
    tests_path: str
    problem: str | None  # why the files could not be located


def _blob(repository: Path, commit: str, path: str) -> bytes | None:
    """The content of *path* at *commit*, or None when the commit has no such file."""
    try:
        raw_git(repository, "cat-file", "-e", f"{commit}:{path}")
    except FileCommitError:
        return None
    return raw_git(repository, "cat-file", "blob", f"{commit}:{path}")


def _read_side(
    repository: Path,
    label: str,
    commit: str,
    placement: Callable[[Path, str], PurePosixPath],
) -> _Side:
    """Read ``metrics.toml`` and ``tests.toml`` of *commit* from the place it keeps them."""
    try:
        place = placement(repository, commit)
    except BooleyDirOutsideCheckoutError as exc:
        return _Side(label, None, None, TESTS_FILE, f"{label} commit: {exc}")
    tests_path = (place / TESTS_FILE).as_posix()
    return _Side(
        label,
        _blob(repository, commit, (place / METRICS_FILE).as_posix()),
        _blob(repository, commit, tests_path),
        tests_path,
        None,
    )


def _parse(side: _Side) -> tuple[WrittenRules | None, str | None]:
    """The rules of *side*, or the rendered load errors."""
    try:
        return parse_metrics_as_written(side.metrics or b""), None
    except MetricsFileError as exc:
        errors = "; ".join(error.render() for error in exc.errors)
        return None, f"{side.label} {METRICS_FILE}: {errors}"


def _tests_diff(before: _Side, after: _Side) -> str | None:
    """A unified diff of the two ``tests.toml`` (Python's, so Git configuration cannot change it)."""
    if before.tests == after.tests:
        return None

    def lines(content: bytes | None) -> list[str]:
        return (content or b"").decode("utf-8", "replace").splitlines(keepends=True)

    return "".join(
        difflib.unified_diff(
            lines(before.tests),
            lines(after.tests),
            fromfile=f"a/{before.tests_path}",
            tofile=f"b/{after.tests_path}",
        )
    )


def _compare(
    repository: Path,
    before_commit: str,
    after_commit: str,
    placement: Callable[[Path, str], PurePosixPath],
) -> RulesDiff:
    """The rules diff from *before_commit* to *after_commit*, both in *repository*."""
    before = _read_side(repository, "base", before_commit, placement)
    after = _read_side(repository, "measured", after_commit, placement)
    problems = [side.problem for side in (before, after) if side.problem]
    if problems:
        return RulesDiff((), None, "\n".join(problems), None)
    old_rules, old_problem = _parse(before)
    new_rules, new_problem = _parse(after)
    tests = _tests_diff(before, after)
    if old_rules is None or new_rules is None:
        unreadable = "\n".join(text for text in (old_problem, new_problem) if text)
        return RulesDiff((), tests, unreadable, None)
    return RulesDiff(_classify(old_rules, new_rules), tests, None, None)


def _paired_repository(repository: Path) -> Path:
    """The checkout's Versioned Booley Directory repository."""
    paired = versioned_booley_dir(repository)
    if paired is None:
        raise ValueError(
            "the measured commit's rules come from a Versioned Booley Directory, but "
            f"{repository} has none"
        )
    return paired


def _at_root(_repository: Path, _commit: str) -> PurePosixPath:
    """The place of the files in a Versioned Booley Directory: its repository root."""
    return PurePosixPath()


def rules_diff(repository: Path, keyed: KeyedCommit, base: str | None) -> RulesDiff:
    """Compare the rules of the measured commit with those of its Base, from Git objects.

    *base* is the RTL Base commit (``None``: no base). When ``keyed.booley_commit`` is
    ``None`` the rules are in the measured tree and the two commits of *repository* are
    compared, each at the Booley Directory placement it configures. Otherwise they come
    from the Versioned Booley Directory: its own Base (merge-base with its default
    branch, kept on the result) is compared with ``keyed.booley_commit`` and *base* is
    not used. A missing file declares nothing; an unreadable one is reported in
    ``unreadable`` and nothing else is compared. No Base means no changes.

    Raises:
        ValueError: ``keyed.booley_commit`` is set but *repository* has no Versioned
            Booley Directory.
    """
    if keyed.booley_commit is None:
        if base is None:
            return RulesDiff((), None, None, None)
        return _compare(repository, base, keyed.commit, booley_dir_at)
    paired = _paired_repository(repository)
    paired_base = resolve_base(paired, keyed.booley_commit)
    if paired_base.commit is None:
        return RulesDiff((), None, None, paired_base)
    diff = _compare(paired, paired_base.commit, keyed.booley_commit, _at_root)
    return replace(diff, paired_base=paired_base)


def _under(place: PurePosixPath, changed: ChangedFiles) -> ChangedFiles:
    """*changed* (relative to the Versioned Booley Directory) as paths of the RTL checkout."""
    prefix = f"{place.as_posix()}/"
    submodules = tuple(
        replace(
            change,
            path=prefix + change.path,
            files=None if change.files is None else tuple(prefix + name for name in change.files),
        )
        for change in changed.submodules
    )
    files = tuple(replace(file, path=prefix + file.path) for file in changed.files)
    return ChangedFiles(files, submodules)


def paired_protected_edits(
    repository: Path, keyed: KeyedCommit, diff: RulesDiff, protected: tuple[str, ...]
) -> tuple[str, ...]:
    """Protected paths changed inside the Versioned Booley Directory, as checkout paths.

    *diff* is the result of :func:`rules_diff` for *keyed*, which carries the paired
    Base; *protected* is the protected set of the measured export. Paths are prefixed
    with the measured commit's Booley Directory placement, the one the export uses.
    Empty when the rules are in the measured tree or the paired commit has no Base.

    Raises:
        BooleyDirOutsideCheckoutError: the measured commit's placement escapes the checkout.
    """
    if keyed.booley_commit is None or diff.paired_base is None or diff.paired_base.commit is None:
        return ()
    paired = _paired_repository(repository)
    changed = changed_files(paired, diff.paired_base.commit, keyed.booley_commit)
    place = booley_dir_at(repository, keyed.commit)
    return protected_edits(_under(place, changed), protected)


def header_amber(diff: RulesDiff) -> tuple[AmberReason, ...]:
    """The reasons the header is amber; empty means the verdicts decide its colour.

    A rules file that could not be read counts as loosened: a loosening cannot be
    ruled out. Tightened and neutral changes and edits to ``booley.toml`` or
    ``reviews.toml`` contribute nothing.
    """
    reasons: list[AmberReason] = []
    if diff.unreadable is not None or any(
        change.direction is Direction.LOOSENED for change in diff.changes
    ):
        reasons.append(AmberReason.RULES_LOOSENED)
    if diff.tests_diff is not None:
        reasons.append(AmberReason.TESTS_CHANGED)
    return tuple(reasons)

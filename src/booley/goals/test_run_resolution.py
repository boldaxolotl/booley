"""The Test Runs a Goal is frozen to (ADR 0072).

A Goal that is judged by Test Runs (:mod:`booley.goals.test_run_goals`) is
resolved to concrete Test Runs once (:mod:`booley.goals.test_run_freezing`),
and that resolution is stored in the Goal Record (``test_run_resolutions``).
Later ``tests.toml`` edits therefore cannot change what the Goal requires: a
default Test Variant or Default Seed that moves leaves the frozen run where
it was.

A resolution has **slots**. A Goal that names selectors has one slot per
written selector; a Goal over every Test of its Target has the one slot
:data:`SUITE_SLOT`. A slot that cannot be resolved yet (the Target or the
Test does not exist) is ``pending`` and is filled later, once. The suite slot
additionally grows: a Test added to the Target later is frozen when it first
exists; and the unnamed run it froze while the Target registered no Test at
all is superseded by the Target's first registered Tests. Nothing else about
a resolution may change while the Goal keeps its specification revision
(:func:`resolution_update_violation`).

This module holds the stored values only and imports nothing of the Goal
Record, which holds them.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any, Literal, cast

from booley.core.boundary import BoundaryError, require_dict, require_list, require_positive_int
from booley.flows.test_runs import TestRun, test_run_document, test_run_from_document
from booley.runtime.timefmt import parse_timestamp

#: The slot of a Goal over every Test of its Target.
SUITE_SLOT = "*"

ResolvedBy = Literal["entry", "deferred", "change"]
#: Frozen after Goal Mode entry, when the Target or Test first existed.
DEFERRED: ResolvedBy = "deferred"
_RESOLVED_BY: tuple[ResolvedBy, ...] = ("entry", DEFERRED, "change")


class ResolutionFormatError(ValueError):
    """A stored Test Run resolution does not have the resolution format."""


@dataclass(frozen=True)
class FrozenRun:
    """One Test Run a Goal requires, with the slot it fills and when it was frozen."""

    run: TestRun
    slot: str
    resolved_by: ResolvedBy
    resolved_at: str

    @property
    def deferred(self) -> bool:
        """Whether the run was frozen after entry (the review package lists those)."""
        return self.resolved_by == DEFERRED

    def to_json(self) -> dict[str, Any]:
        """The stored form: the Test Run record plus the resolution fields."""
        return {
            **test_run_document(self.run),
            "slot": self.slot,
            "resolved_by": self.resolved_by,
            "resolved_at": self.resolved_at,
        }


@dataclass(frozen=True)
class TestRunResolution:
    """The frozen Test Runs of one Goal at one specification revision."""

    __test__ = False

    spec_revision: int
    runs: tuple[FrozenRun, ...] = ()
    pending: tuple[str, ...] = ()

    @property
    def test_runs(self) -> tuple[TestRun, ...]:
        """The required Test Runs, in frozen order."""
        return tuple(item.run for item in self.runs)

    @property
    def deferred(self) -> tuple[FrozenRun, ...]:
        """Runs frozen after entry, which the review package lists."""
        return tuple(item for item in self.runs if item.deferred)

    def to_json(self) -> dict[str, Any]:
        """The form stored under the Goal's key in ``record.json``."""
        return {
            "spec_revision": self.spec_revision,
            "runs": [item.to_json() for item in self.runs],
            "pending": list(self.pending),
        }

    @classmethod
    def from_json(cls, raw: object, *, where: str) -> TestRunResolution:
        """Parse a stored resolution, raising :class:`ResolutionFormatError`."""
        try:
            mapping = require_dict(raw, field=where)
            if set(mapping) != {"spec_revision", "runs", "pending"}:
                raise ResolutionFormatError(f"{where} must hold spec_revision, runs and pending")
            revision = require_positive_int(
                mapping["spec_revision"], field=f"{where}.spec_revision"
            )
            rows = require_list(mapping["runs"], field=f"{where}.runs")
            pending = require_list(mapping["pending"], field=f"{where}.pending")
        except BoundaryError as exc:
            raise ResolutionFormatError(str(exc)) from None
        if not all(isinstance(slot, str) and slot for slot in pending):
            raise ResolutionFormatError(f"{where}.pending must name slots")
        runs = tuple(_frozen_run(row, f"{where}.runs[{index}]") for index, row in enumerate(rows))
        names = [item.run.name for item in runs]
        if len(set(names)) != len(names):
            raise ResolutionFormatError(f"{where}.runs repeats a Test Run")
        return cls(revision, runs, tuple(cast("list[str]", pending)))


def _frozen_run(raw: object, where: str) -> FrozenRun:
    try:
        row = dict(require_dict(raw, field=where))
    except BoundaryError as exc:
        raise ResolutionFormatError(str(exc)) from None
    slot = row.pop("slot", None)
    by = row.pop("resolved_by", None)
    at = row.pop("resolved_at", None)
    if not isinstance(slot, str) or not slot or not isinstance(at, str) or by not in _RESOLVED_BY:
        raise ResolutionFormatError(f"{where} needs slot, resolved_by and resolved_at")
    try:
        parse_timestamp(at)
    except ValueError:
        raise ResolutionFormatError(f"{where}.resolved_at is not a timestamp") from None
    try:
        run = test_run_from_document(row)
    except ValueError as exc:
        raise ResolutionFormatError(f"{where}: {exc}") from None
    return FrozenRun(run, slot, by, at)


# ---------------------------------------------------------------------------
# What a saved record may change
# ---------------------------------------------------------------------------


def resolution_update_violation(
    key: str, before: TestRunResolution, after: TestRunResolution
) -> str:
    """Why *after* may not replace *before* at one specification revision; empty when it may.

    An update only fills pending slots or freezes Tests added to the suite
    slot: every frozen run stays, in place, and no slot becomes pending. The
    one run that may go is the suite slot's unnamed run, and only when the
    update freezes a named Test in that slot: a Target that registers Tests
    no longer has an unnamed invocation.
    """
    named = any(item.slot == SUITE_SLOT and item.run.test is not None for item in after.runs)
    if named and not any(_unnamed_suite_run(item) for item in after.runs):
        before = replace(
            before, runs=tuple(item for item in before.runs if not _unnamed_suite_run(item))
        )
    if after.runs[: len(before.runs)] != before.runs:
        return f"the frozen Test Runs of Goal {key} cannot change"
    if not set(after.pending) <= set(before.pending):
        return f"Goal {key} cannot gain pending Test Run slots"
    known = {item.run.test for item in before.runs if item.slot == SUITE_SLOT}
    for item in after.runs[len(before.runs) :]:
        filled = item.slot in before.pending and item.slot not in after.pending
        suite = item.slot == SUITE_SLOT and item.run.test not in known
        if not (filled or (suite and SUITE_SLOT not in before.pending)):
            return f"Test Run {item.run.name} of Goal {key} fills no pending slot"
    return ""


def _unnamed_suite_run(item: FrozenRun) -> bool:
    """Whether *item* is the unnamed invocation a whole-Target Goal froze."""
    return item.slot == SUITE_SLOT and item.run.test is None


def resolutions_to_json(resolutions: Mapping[str, TestRunResolution]) -> dict[str, Any]:
    """The ``test_run_resolutions`` field of ``record.json``."""
    return {key: resolutions[key].to_json() for key in sorted(resolutions)}


def resolutions_from_json(raw: object) -> dict[str, TestRunResolution]:
    """Parse the ``test_run_resolutions`` field, raising :class:`ResolutionFormatError`."""
    try:
        mapping = require_dict(raw, field="test_run_resolutions")
    except BoundaryError as exc:
        raise ResolutionFormatError(str(exc)) from None
    return {
        str(key): TestRunResolution.from_json(value, where=f"test_run_resolutions[{key}]")
        for key, value in mapping.items()
    }

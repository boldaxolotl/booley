"""Typed, read-only reads of one decoded Simulation Record manifest.

The view wraps a manifest the codec already validated.  It never touches
storage: no path, hash, or re-read.  Members the codec validates are read
directly; the three it does not fully validate raise
:class:`SimulationRecordIntegrityError` instead of defaulting silently.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from booley.flows.test_runs import TestRun, test_run_from_document

from .codec import SimulationRecordIntegrityError
from .model import SimulationRecordManifest

if TYPE_CHECKING:
    from .coordinator import SimulationRecordRecoveryStatus

_COVERAGE_KIND = "coverage_aggregate"


@dataclass(frozen=True, slots=True)
class ManifestTarget:
    """The Target a manifest froze."""

    selector: str
    vlnv: str
    name: str
    role: str
    revision: str

    @property
    def identity(self) -> str:
        """The ``vlnv#name`` form of the Target's identity."""
        return f"{self.vlnv}#{self.name}"


def _mapping(value: object) -> Mapping[str, object]:
    return cast(Mapping[str, object], value)


@dataclass(frozen=True, slots=True)
class SimulationRecordManifestView:
    """Typed reads of one decoded manifest; never touches storage."""

    manifest: SimulationRecordManifest

    @property
    def _workload(self) -> Mapping[str, object]:
        return _mapping(self.manifest.document["workload"])

    @property
    def _items(self) -> tuple[Mapping[str, object], ...]:
        return cast(tuple[Mapping[str, object], ...], self.manifest.document["work_items"])

    @property
    def simulation_record_id(self) -> str:
        value = self.manifest.document["simulation_record_id"]
        if not isinstance(value, str):
            raise SimulationRecordIntegrityError("simulation_record_id must be a UUID")
        return value

    @property
    def origin_invocation_id(self) -> int:
        return cast(int, _mapping(self.manifest.document["origin"])["invocation_id"])

    @property
    def target(self) -> ManifestTarget:
        target = cast(Mapping[str, str], self.manifest.document["target"])
        return ManifestTarget(
            selector=target["selector"],
            vlnv=target["vlnv"],
            name=target["name"],
            role=target["role"],
            revision=target["revision"],
        )

    @property
    def work_item_ids(self) -> tuple[str, ...]:
        """The work-item ids in manifest order."""
        return tuple(cast(str, item["work_item_id"]) for item in self._items)

    @property
    def work_item_kind(self) -> str | None:
        """The first work item's kind; ``None`` when the manifest has no work items."""
        items = self._items
        return cast(str, items[0]["kind"]) if items else None

    @property
    def is_coverage(self) -> bool:
        """The one coverage rule: the first work item is a coverage aggregate."""
        return self.work_item_kind == _COVERAGE_KIND

    @property
    def selected_name_groups(self) -> tuple[tuple[str, ...], ...]:
        return tuple(
            cast(tuple[str, ...], _mapping(item["selection"])["names"]) for item in self._items
        )

    @property
    def test_run_groups(self) -> tuple[tuple[TestRun, ...], ...]:
        """Each work item's Test Runs exactly as the manifest froze them."""
        try:
            return tuple(
                tuple(
                    test_run_from_document(entry)
                    for entry in cast(tuple[object, ...], item["test_runs"])
                )
                for item in self._items
            )
        except ValueError as exc:
            raise SimulationRecordIntegrityError(str(exc)) from exc

    @property
    def required_suite_names(self) -> tuple[str, ...]:
        suite = _mapping(self.manifest.document["required_suite"])
        return cast(tuple[str, ...], suite["names"])

    @property
    def required_suite_default_invocation(self) -> bool:
        suite = _mapping(self.manifest.document["required_suite"])
        return cast(bool, suite["default_invocation"])

    @property
    def trace(self) -> bool:
        return cast(bool, self._workload["trace"])

    @property
    def no_waivers(self) -> bool:
        """Whether the workload asked for raw coverage; absent means waivers apply."""
        value = self._workload.get("no_waivers", False)
        if not isinstance(value, bool):
            raise SimulationRecordIntegrityError("workload.no_waivers must be a boolean")
        return value

    @property
    def eda_tool_kind(self) -> str:
        return cast(str, _mapping(self._workload["eda"])["kind"])

    @property
    def prerequisites(self) -> tuple[Mapping[str, object], ...]:
        """The raw prerequisite entries, handed back to the planner."""
        return cast(tuple[Mapping[str, object], ...], self.manifest.document["prerequisites"])

    def unfinished_work_item_ids(self, status: SimulationRecordRecoveryStatus) -> tuple[str, ...]:
        """Pending and interrupted ids in manifest order; ``()`` unless a coverage record."""
        if not self.is_coverage:
            return ()
        unfinished = set(status.pending) | set(status.interrupted)
        return tuple(item_id for item_id in self.work_item_ids if item_id in unfinished)

"""Read-only authenticated observations of durable Simulation Record storage."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from booley.core.file_lock import LockContentionError
from booley.flows.sim.record_layout import (
    RECORD_DIRECTORY,
    refuse_older_manifest_path,
)
from booley.flows.simulation_record_results import (
    DurableSimulationRecordResults,
    SimulationRecordBusyError,
    StartedSimulationRecord,
)

from .codec import (
    SimulationRecordIntegrityError,
)
from .coordinator import collected_result_facts
from .model import SimulationRecordManifest, SimulationResult
from .planning import manifest_digest
from .store import SimulationRecordStore, collect_pre_sim_firings
from .test_run_report import observation_test_run_rows


class SimulationRecordWorkItemError(SimulationRecordIntegrityError):
    """One exact terminal Simulation Record work item could not be selected."""


@dataclass(frozen=True, slots=True)
class SimulationRecordWorkItemEvidence:
    """Immutable evidence read from one terminal Simulation Record work item."""

    manifest_path: Path
    manifest_sha256: str
    manifest: SimulationRecordManifest
    work_item_id: str
    work_item: Mapping[str, object]
    result: SimulationResult


def authenticate_work_item(
    manifest_path: Path, work_item_id: str, *, booley_dir: Path | None = None
) -> SimulationRecordWorkItemEvidence:
    """Return one exact terminal result from an authenticated complete Simulation Record scan."""
    store = _store_for_manifest(manifest_path, booley_dir)
    try:
        manifest = store.load_manifest()
        digest = manifest_digest(manifest)
        recovery = store.scan(manifest, digest)
    except SimulationRecordIntegrityError:
        raise
    except OSError as exc:
        raise SimulationRecordIntegrityError(
            f"cannot inspect Simulation Record storage: {exc}"
        ) from exc
    recovered = tuple(item for item in recovery.items if item.work_item_id == work_item_id)
    if len(recovered) != 1 or recovered[0].result is None:
        raise SimulationRecordWorkItemError(
            f"Simulation Record has no exact terminal work item {work_item_id!r}"
        )
    manifest_items = cast(tuple[Mapping[str, object], ...], manifest.document["work_items"])
    selected = tuple(item for item in manifest_items if item["work_item_id"] == work_item_id)
    if len(selected) != 1:
        raise SimulationRecordWorkItemError(
            f"Simulation Record has no exact manifest work item {work_item_id!r}"
        )
    result = recovered[0].result
    assert result is not None
    return SimulationRecordWorkItemEvidence(
        store.manifest_path,
        digest,
        manifest,
        work_item_id,
        selected[0],
        result,
    )


def started_simulation_record(manifest_path: Path) -> StartedSimulationRecord:
    """The identity of the record whose manifest was just published at *manifest_path*.

    Raises :class:`SimulationRecordIntegrityError` when the manifest cannot
    be read or is not a record manifest.
    """
    try:
        manifest = _store_for_manifest(manifest_path).load_manifest()
    except OSError as exc:
        raise SimulationRecordIntegrityError(
            f"cannot read Simulation Record manifest: {exc}"
        ) from exc
    target = cast(Mapping[str, object], manifest.document["target"])
    return StartedSimulationRecord(
        manifest_path,
        str(manifest.document["simulation_record_id"]),
        manifest_digest(manifest),
        (str(target["selector"]), f"{target['vlnv']}#{target['name']}"),
        target["role"] == "candidate",
    )


def read_durable_results(
    manifest_path: Path, manifest_sha256: str
) -> DurableSimulationRecordResults:
    """Authenticate and return the terminal results of the record at *manifest_path*.

    For a record that never returned an outcome (it was cancelled or
    killed): the same Test Run rows a completed record's acceptance
    records, for the work items that finished. *manifest_sha256* is the
    digest recorded when the record started; another manifest is refused.
    Every result is decoded and bound to its attempt by the store's validated
    scan (evidence is trusted on read), and each row carries the Test Run the
    manifest recorded with the fingerprint its observation bound. The read holds the record's
    mutation lock, so the results returned are all there are until the
    record is resumed.

    Raises :class:`~booley.flows.simulation_record_results.SimulationRecordBusyError` while a
    scheduler owns the record,
    :class:`~booley.flows.simulation_record_results.OlderBooleyRecordError` when an older
    Booley wrote it, and :class:`SimulationRecordIntegrityError`
    when the record is gone, is not the expected one, or any of its results
    cannot be authenticated.
    """
    store = _store_for_manifest(manifest_path)
    try:
        with store.mutation_lock():
            return _durable_results(store, manifest_sha256)
    except LockContentionError as exc:
        raise SimulationRecordBusyError(
            f"Simulation Record {manifest_path} is running: its results are not final"
        ) from exc
    except OSError as exc:
        raise SimulationRecordIntegrityError(
            f"cannot inspect Simulation Record storage: {exc}"
        ) from exc


def _durable_results(
    store: SimulationRecordStore, manifest_sha256: str
) -> DurableSimulationRecordResults:
    manifest = store.load_manifest()
    digest = manifest_digest(manifest)
    if digest != manifest_sha256:
        raise SimulationRecordIntegrityError(
            "Simulation Record manifest is not the one that was registered"
        )
    recovery = store.scan(manifest, digest)
    items = cast(tuple[Mapping[str, object], ...], manifest.document["work_items"])
    by_id = {item["work_item_id"]: item for item in items}
    consumed, observations = collected_result_facts(store, recovery, by_id)
    results = tuple(
        (str(fact["work_item_id"]), str(cast(Mapping[str, object], fact["result"])["sha256"]))
        for fact in consumed
    )
    rows = tuple(observation_test_run_rows(manifest, observations))
    return DurableSimulationRecordResults(
        digest,
        str(manifest.document["simulation_record_id"]),
        results,
        rows,
        complete=not recovery.pending and not recovery.interrupted,
    )


def _store_for_manifest(
    manifest_path: Path, booley_dir: Path | None = None
) -> SimulationRecordStore:
    canonical = manifest_path.absolute()
    refuse_older_manifest_path(canonical)
    if canonical.name != "manifest.json" or canonical.parent.name != RECORD_DIRECTORY:
        raise SimulationRecordIntegrityError("expected exact record/manifest.json path")
    return SimulationRecordStore(canonical.parent, booley_dir=booley_dir)


__all__ = [
    "SimulationRecordWorkItemError",
    "SimulationRecordWorkItemEvidence",
    "authenticate_work_item",
    "collect_pre_sim_firings",
    "read_durable_results",
    "started_simulation_record",
]

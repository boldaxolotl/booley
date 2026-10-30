"""Read-only authenticated observations of durable Simulation Campaign storage."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from booley.core.file_lock import LockContentionError
from booley.flows.campaign_results import (
    CampaignBusyError,
    DurableCampaignResults,
    StartedCampaign,
)
from booley.runtime.execution_records import ExecutionId

from .codec import SimulationCampaignIntegrityError
from .coordinator import collected_result_facts
from .model import SimulationCampaignManifest, SimulationResult
from .planning import manifest_digest
from .pre_sim_evidence import PreSimFiring
from .store import CampaignRecovery, CampaignStore, collect_pre_sim_firings, reread_pre_sim_firings
from .test_run_report import observation_test_run_rows


class SimulationCampaignWorkItemError(SimulationCampaignIntegrityError):
    """One exact terminal Campaign work item could not be selected."""


@dataclass(frozen=True, slots=True)
class CampaignWorkItemEvidence:
    """Immutable evidence read from one terminal Campaign work item."""

    manifest_path: Path
    manifest_sha256: str
    manifest: SimulationCampaignManifest
    work_item_id: str
    work_item: Mapping[str, object]
    result: SimulationResult


@dataclass(frozen=True, slots=True)
class RetainedCampaignStatus:
    """Authenticated recovery and summary status used by retention policy."""

    manifest_path: Path
    summary_path: Path
    manifest_sha256: str
    manifest: SimulationCampaignManifest
    campaign_id: str
    target_selector: str
    target_identity: str
    completed: tuple[str, ...]
    pending: tuple[str, ...]
    interrupted: tuple[str, ...]
    summary_present: bool
    summary_complete: bool
    summary_completed_matches: bool
    retention_files: tuple[Path, ...]
    coverage_directories: tuple[Path, ...]


def read_pre_sim_firings(
    manifest_path: Path, *, booley_dir: Path | None = None
) -> tuple[PreSimFiring, ...]:
    """Read authenticated firing evidence without exposing a mutable store to Flow."""
    store = _store_for_manifest(manifest_path, booley_dir)
    return store.inspect_pre_sim_firings()


def read_invocation_pre_sim_firings(
    invocation: Path, resumed_origin: Path | None = None, *, booley_dir: Path | None = None
) -> tuple[PreSimFiring, ...]:
    """Inspect Campaigns published in this invocation, including unfinished work.

    A resumed origin Campaign is one more root of the same bounded collection.
    """
    paths = sorted((invocation / "targets").glob("*/campaign/manifest.json"))
    if len(paths) > 4000:
        raise SimulationCampaignIntegrityError("invocation Campaign count exceeds bound")
    if resumed_origin is not None:
        paths.append(resumed_origin)
    return collect_pre_sim_firings(*paths, booley_dir=booley_dir)


def authenticate_work_item(
    manifest_path: Path, work_item_id: str, *, booley_dir: Path | None = None
) -> CampaignWorkItemEvidence:
    """Return one exact terminal result from an authenticated complete Campaign scan."""
    store = _store_for_manifest(manifest_path, booley_dir)
    try:
        manifest = store.load_manifest()
        digest = manifest_digest(manifest)
        recovery = store.scan_validated(manifest, digest)
    except SimulationCampaignIntegrityError:
        raise
    except OSError as exc:
        raise SimulationCampaignIntegrityError(
            f"cannot inspect Simulation Campaign storage: {exc}"
        ) from exc
    recovered = tuple(item for item in recovery.items if item.work_item_id == work_item_id)
    if len(recovered) != 1 or recovered[0].result is None:
        raise SimulationCampaignWorkItemError(
            f"Campaign has no exact terminal work item {work_item_id!r}"
        )
    manifest_items = cast(tuple[Mapping[str, object], ...], manifest.document["work_items"])
    selected = tuple(item for item in manifest_items if item["work_item_id"] == work_item_id)
    if len(selected) != 1:
        raise SimulationCampaignWorkItemError(
            f"Campaign has no exact manifest work item {work_item_id!r}"
        )
    result = recovered[0].result
    assert result is not None
    return CampaignWorkItemEvidence(
        store.manifest_path,
        digest,
        manifest,
        work_item_id,
        selected[0],
        result,
    )


def inspect_retained_campaign(
    manifest_path: Path, *, booley_dir: Path | None = None
) -> RetainedCampaignStatus:
    """Return authenticated recovery plus existing summary consistency, without writes."""
    store = _store_for_manifest(manifest_path, booley_dir)
    try:
        manifest = store.load_manifest()
        digest = manifest_digest(manifest)
        recovery = store.scan_validated(manifest, digest)
        if not store.summary_path.exists() and not store.summary_path.is_symlink():
            summary = None
        else:
            summary = store.load_summary()
    except SimulationCampaignIntegrityError:
        raise
    except OSError as exc:
        raise SimulationCampaignIntegrityError(
            f"cannot inspect Simulation Campaign storage: {exc}"
        ) from exc
    completed = cast(list[str], summary["completed"]) if summary is not None else []
    target = cast(Mapping[str, str], manifest.document["target"])
    retention_files, coverage_directories = _retention_inventory(store, recovery)
    return RetainedCampaignStatus(
        store.manifest_path,
        store.summary_path,
        digest,
        manifest,
        cast(str, manifest.document["campaign_id"]),
        target["selector"],
        f"{target['vlnv']}#{target['name']}",
        recovery.complete,
        recovery.pending,
        recovery.interrupted,
        summary is not None,
        summary is not None and summary["complete"] is True,
        summary is None or tuple(completed) == recovery.complete,
        retention_files,
        coverage_directories,
    )


def _retention_inventory(
    store: CampaignStore, recovery: CampaignRecovery
) -> tuple[tuple[Path, ...], tuple[Path, ...]]:
    retention_files = set(store.retention_files)
    attempts = {
        path.parent
        for path in retention_files
        if path.name == "attempt.json" and path.parent.parent.name == "attempts"
    }
    completed_attempts = {
        cast(str, item.result.document["attempt_id"]): item.result.document
        for item in recovery.items
        if item.result is not None
    }
    coverage_directories = []
    for attempt in attempts:
        attempt_id = attempt.name.split("-", 1)[1]
        if attempt_id in completed_attempts:
            coverage_directories.append(attempt / "coverage-campaign")
            retention_files.update(
                _result_named_files(store.root, attempt, completed_attempts[attempt_id])
            )
            continue
        for name in (
            "private-build",
            "snapshot",
            "runtime-inputs",
            "evidence",
            "pre-sim",
            "coverage-campaign",
        ):
            retention_files.update(
                path.absolute() for path in (attempt / name).rglob("*") if path.is_file()
            )
    return tuple(sorted(retention_files)), tuple(sorted(coverage_directories))


def _result_named_files(root: Path, attempt: Path, result: Mapping[str, object]) -> set[Path]:
    """Files a terminal result names, plus the Booley-written build trees it used, unhashed."""
    references = list(cast(tuple[Mapping[str, object], ...], result["evidence"]))
    snapshot = result["executable_snapshot"]
    if isinstance(snapshot, Mapping):
        references.append(cast(Mapping[str, object], snapshot["manifest"]))
    for binding in cast(tuple[Mapping[str, object], ...], result["runtime_inputs"]):
        references.append(cast(Mapping[str, object], binding["authoritative_copy"]))
    named = {(attempt / cast(str, reference["path"])).absolute() for reference in references}
    trees = [attempt / "private-build", attempt / "snapshot"]
    build = cast(Mapping[str, object], result["build_result"])
    if build["sharing"] == "shared_variant":
        trees.append((root / cast(str, build["path"])).parent)
    for tree in trees:
        named.update(path.absolute() for path in tree.rglob("*") if path.is_file())
    return named


@contextmanager
def retained_campaign_lock(
    manifest_path: Path, *, booley_dir: Path | None = None
) -> Iterator[None]:
    """Exclude mutation of one existing authenticated Campaign without recreating it."""
    store = _store_for_manifest(manifest_path, booley_dir)
    with store.mutation_lock():
        yield


def authenticate_retained_campaign_inventory(
    status: RetainedCampaignStatus, *, booley_dir: Path | None = None
) -> tuple[Path, ...]:
    """Authenticate shared-build files owned by one retained Campaign."""
    store = _store_for_manifest(status.manifest_path, booley_dir)
    origin = cast(Mapping[str, object], status.manifest.document["origin"])
    variants = cast(tuple[Mapping[str, object], ...], status.manifest.document["build_variants"])
    for variant in variants:
        store.recover_shared_build(
            cast(str, variant["build_variant_id"]),
            cast(int, origin["invocation_id"]),
        )
    return store.retention_files


def recover_retained_campaign_resources(
    status: RetainedCampaignStatus, project_data: Path | None, *, booley_dir: Path | None = None
) -> None:
    """Recover interrupted lanes and interrupted owned run directories."""
    from .child_protocol import ChildExecutionRegistry
    from .run_directory import (
        cleanup_interrupted_run_directory,
        restore_run_directory_from_project_data,
    )

    store = _store_for_manifest(status.manifest_path, booley_dir)
    surviving_runs = _surviving_owned_run_directories(store, status.interrupted)
    interrupted_lanes = _interrupted_lane_ids(store, status.interrupted)
    if (surviving_runs or interrupted_lanes) and project_data is None:
        raise SimulationCampaignIntegrityError(
            "Booley Directory is required to recover retained Campaign resources"
        )
    if project_data is None:
        return
    registry = ChildExecutionRegistry.from_project_data(
        status.manifest, project_data, booley_dir=booley_dir
    )
    for lane_id in interrupted_lanes:
        # Fail closed before any interrupted run directory is removed.
        registry.recover_lane(lane_id)
    for work_item_id in status.interrupted:
        attempt = store.latest_attempt(work_item_id)
        if attempt is None:
            continue
        document = attempt.document
        run = restore_run_directory_from_project_data(
            cast(Mapping[str, object], document["run_directory"]),
            project_data=project_data,
            booley_dir=booley_dir,
        )
        cleanup_interrupted_run_directory(
            run,
            identity={
                "campaign_id": cast(str, document["campaign_id"]),
                "work_item_id": work_item_id,
                "attempt_id": cast(str, document["attempt_id"]),
            },
        )


def _interrupted_lane_ids(
    store: CampaignStore, interrupted: tuple[str, ...]
) -> tuple[ExecutionId, ...]:
    """Name the lane executions of interrupted attempts that were scheduled on a lane."""
    lanes: list[ExecutionId] = []
    for work_item_id in interrupted:
        attempt = store.latest_attempt(work_item_id)
        if attempt is not None and attempt.document["child_execution_id"] is not None:
            lanes.append(ExecutionId(cast(str, attempt.document["child_execution_id"])))
    return tuple(lanes)


def _surviving_owned_run_directories(
    store: CampaignStore, interrupted: tuple[str, ...]
) -> tuple[Path, ...]:
    surviving: list[Path] = []
    for work_item_id in interrupted:
        attempt = store.latest_attempt(work_item_id)
        if attempt is None:
            continue
        run = cast(Mapping[str, object], attempt.document["run_directory"])
        path = Path(cast(str, run["resolved"]))
        if run["owned"] is True and path.exists():
            surviving.append(path)
    return tuple(surviving)


def started_campaign(manifest_path: Path) -> StartedCampaign:
    """The identity of the campaign whose manifest was just published at *manifest_path*.

    Raises :class:`SimulationCampaignIntegrityError` when the manifest cannot
    be read or is not a campaign manifest.
    """
    try:
        manifest = _store_for_manifest(manifest_path).load_manifest()
    except OSError as exc:
        raise SimulationCampaignIntegrityError(
            f"cannot read Simulation Campaign manifest: {exc}"
        ) from exc
    target = cast(Mapping[str, object], manifest.document["target"])
    return StartedCampaign(
        manifest_path,
        str(manifest.document["campaign_id"]),
        manifest_digest(manifest),
        (str(target["selector"]), f"{target['vlnv']}#{target['name']}"),
        target["role"] == "candidate",
    )


def read_durable_results(manifest_path: Path, manifest_sha256: str) -> DurableCampaignResults:
    """Authenticate and return the terminal results of the campaign at *manifest_path*.

    For a campaign that never returned an outcome (it was cancelled or
    killed): the same Test Run rows a completed campaign's acceptance
    records, for the work items that finished. *manifest_sha256* is the
    digest recorded when the campaign started; another manifest is refused.
    Every result is decoded and bound to its attempt by the store's validated
    scan (evidence is trusted on read), and each row carries the Test Run the
    manifest recorded with the fingerprint its observation bound. The read holds the campaign's
    mutation lock, so the results returned are all there are until the
    campaign is resumed.

    Raises :class:`~booley.flows.campaign_results.CampaignBusyError` while a
    scheduler owns the campaign, and :class:`SimulationCampaignIntegrityError`
    when the campaign is gone, is not the expected one, or any of its results
    cannot be authenticated (one whose writer was killed mid-write included).
    """
    store = _store_for_manifest(manifest_path)
    try:
        with store.mutation_lock():
            return _durable_results(store, manifest_sha256)
    except LockContentionError as exc:
        raise CampaignBusyError(
            f"Simulation Campaign {manifest_path} is running: its results are not final"
        ) from exc
    except OSError as exc:
        raise SimulationCampaignIntegrityError(
            f"cannot inspect Simulation Campaign storage: {exc}"
        ) from exc


def _durable_results(store: CampaignStore, manifest_sha256: str) -> DurableCampaignResults:
    manifest = store.load_manifest()
    digest = manifest_digest(manifest)
    if digest != manifest_sha256:
        raise SimulationCampaignIntegrityError(
            "Simulation Campaign manifest is not the one that was registered"
        )
    recovery = store.scan_validated(manifest, digest)
    items = cast(tuple[Mapping[str, object], ...], manifest.document["work_items"])
    by_id = {item["work_item_id"]: item for item in items}
    consumed, observations = collected_result_facts(store, recovery, by_id)
    results = tuple(
        (str(fact["work_item_id"]), str(cast(Mapping[str, object], fact["result"])["sha256"]))
        for fact in consumed
    )
    rows = tuple(observation_test_run_rows(manifest, observations))
    return DurableCampaignResults(
        digest,
        str(manifest.document["campaign_id"]),
        results,
        rows,
        complete=not recovery.pending and not recovery.interrupted,
    )


def _store_for_manifest(manifest_path: Path, booley_dir: Path | None = None) -> CampaignStore:
    canonical = manifest_path.absolute()
    if canonical.name != "manifest.json" or canonical.parent.name != "campaign":
        raise SimulationCampaignIntegrityError(
            "expected exact Simulation Campaign campaign/manifest.json path"
        )
    return CampaignStore(canonical.parent, booley_dir=booley_dir)


__all__ = [
    "CampaignWorkItemEvidence",
    "RetainedCampaignStatus",
    "SimulationCampaignWorkItemError",
    "authenticate_retained_campaign_inventory",
    "authenticate_work_item",
    "collect_pre_sim_firings",
    "inspect_retained_campaign",
    "read_durable_results",
    "recover_retained_campaign_resources",
    "reread_pre_sim_firings",
    "retained_campaign_lock",
    "started_campaign",
]

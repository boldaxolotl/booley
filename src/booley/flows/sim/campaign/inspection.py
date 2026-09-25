"""Read-only authenticated observations of durable Simulation Campaign storage."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from .codec import SimulationCampaignIntegrityError
from .model import SimulationCampaignManifest, SimulationResult
from .planning import manifest_digest
from .store import CampaignRecovery, CampaignStore


class SimulationCampaignWorkItemError(SimulationCampaignIntegrityError):
    """One exact terminal Campaign work item could not be selected."""


@dataclass(frozen=True, slots=True)
class CampaignWorkItemEvidence:
    """Authenticated immutable evidence for one terminal Campaign work item."""

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
    completed: tuple[str, ...]
    pending: tuple[str, ...]
    interrupted: tuple[str, ...]
    summary_present: bool
    summary_complete: bool
    summary_completed_matches: bool
    retention_files: tuple[Path, ...]
    coverage_directories: tuple[Path, ...]


def authenticate_work_item(manifest_path: Path, work_item_id: str) -> CampaignWorkItemEvidence:
    """Return one exact terminal result from an authenticated complete Campaign scan."""
    store = _store_for_manifest(manifest_path)
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


def inspect_retained_campaign(manifest_path: Path) -> RetainedCampaignStatus:
    """Return authenticated recovery plus existing summary consistency, without writes."""
    store = _store_for_manifest(manifest_path)
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
    retention_files, coverage_directories = _retention_inventory(store, recovery)
    return RetainedCampaignStatus(
        store.manifest_path,
        store.summary_path,
        digest,
        manifest,
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
        cast(str, item.result.document["attempt_id"])
        for item in recovery.items
        if item.result is not None
    }
    coverage_directories = []
    for attempt in attempts:
        attempt_id = attempt.name.split("-", 1)[1]
        if attempt_id in completed_attempts:
            coverage_directories.append(attempt / "coverage-campaign")
            continue
        for name in (
            "private-build",
            "snapshot",
            "runtime-inputs",
            "evidence",
            "coverage-campaign",
        ):
            retention_files.update(
                path.absolute() for path in (attempt / name).rglob("*") if path.is_file()
            )
    return tuple(sorted(retention_files)), tuple(sorted(coverage_directories))


@contextmanager
def retained_campaign_lock(manifest_path: Path) -> Iterator[None]:
    """Exclude mutation of one existing authenticated Campaign without recreating it."""
    store = _store_for_manifest(manifest_path)
    with store.mutation_lock():
        yield


def recover_retained_campaign_resources(
    status: RetainedCampaignStatus, project_data: Path | None
) -> None:
    """Recover exact child records and interrupted owned run directories."""
    from booley.runtime.job_slots import SlotStore

    from .child_protocol import ChildExecutionRegistry
    from .run_directory import cleanup_interrupted_run_directory, restore_run_directory

    store = _store_for_manifest(status.manifest_path)
    children = store.root / "child-executions" / "entries"
    unretired = (
        tuple(
            path
            for path in children.glob("*.json")
            if not (store.root / "child-executions" / "retired" / path.name).is_file()
        )
        if children.is_dir()
        else ()
    )
    if (unretired or status.interrupted) and project_data is None:
        raise SimulationCampaignIntegrityError(
            "Project data is required to recover retained Campaign resources"
        )
    if project_data is None:
        return
    if unretired:
        registry = ChildExecutionRegistry(store, status.manifest, project_data=project_data)
        registry.recover_selected_unretired(SlotStore(project_data / "runtime" / "jobs" / "slots"))
    for work_item_id in status.interrupted:
        attempt = store.latest_attempt(work_item_id)
        if attempt is None:
            continue
        document = attempt.document
        run = restore_run_directory(
            cast(Mapping[str, object], document["run_directory"]),
            project_data=project_data,
        )
        cleanup_interrupted_run_directory(
            run,
            identity={
                "campaign_id": cast(str, document["campaign_id"]),
                "work_item_id": work_item_id,
                "attempt_id": cast(str, document["attempt_id"]),
            },
        )


def _store_for_manifest(manifest_path: Path) -> CampaignStore:
    canonical = manifest_path.absolute()
    if canonical.name != "manifest.json" or canonical.parent.name != "campaign":
        raise SimulationCampaignIntegrityError(
            "expected exact Simulation Campaign campaign/manifest.json path"
        )
    return CampaignStore(canonical.parent)


__all__ = [
    "CampaignWorkItemEvidence",
    "RetainedCampaignStatus",
    "SimulationCampaignWorkItemError",
    "authenticate_work_item",
    "inspect_retained_campaign",
    "recover_retained_campaign_resources",
    "retained_campaign_lock",
]

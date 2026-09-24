"""Read-only authenticated observations of durable Simulation Campaign storage."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from .codec import SimulationCampaignIntegrityError
from .model import SimulationCampaignManifest, SimulationResult
from .planning import manifest_digest
from .store import CampaignStore


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
    completed: tuple[str, ...]
    pending: tuple[str, ...]
    interrupted: tuple[str, ...]
    summary_complete: bool
    summary_completed_matches: bool


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
        summary = store.load_summary()
    except SimulationCampaignIntegrityError:
        raise
    except OSError as exc:
        raise SimulationCampaignIntegrityError(
            f"cannot inspect Simulation Campaign storage: {exc}"
        ) from exc
    completed = cast(list[str], summary["completed"])
    return RetainedCampaignStatus(
        store.manifest_path,
        store.summary_path,
        digest,
        recovery.complete,
        recovery.pending,
        recovery.interrupted,
        summary["complete"] is True,
        tuple(completed) == recovery.complete,
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
]

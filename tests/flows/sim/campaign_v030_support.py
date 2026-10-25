"""Fabricate the child records Booley 0.3.0 wrote, for the readers that must still accept them."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from booley.flows.sim.campaign.child_protocol import ChildExecutionRegistry
from booley.flows.sim.campaign.codec import canonical_json_bytes
from booley.flows.sim.campaign.planning import manifest_digest
from booley.flows.sim.campaign_durability import durable_create
from booley.runtime.execution_records import ExecutionId

_ENTRY_SCHEMA = "booley.simulation-campaign-child-entry/v1"


@dataclass(frozen=True, slots=True)
class V030Child:
    """The paths and digest of one fabricated 0.3.0 child entry."""

    execution_id: ExecutionId
    entry_sha256: str
    project_entry: Path
    campaign_entry: Path
    context_path: Path


def publish_v030_child(
    registry: ChildExecutionRegistry,
    *,
    execution_id: ExecutionId,
    work_item_id: str,
    attempt_id: str,
    attempt_ordinal: int,
    attempt_directory: Path,
    parent_execution_id: ExecutionId,
) -> V030Child:
    """Prepare a lane, then add the Project and Campaign entry records 0.3.0 published."""
    prepared = registry.prepare(
        execution_id,
        work_item_id=work_item_id,
        attempt_id=attempt_id,
        attempt_ordinal=attempt_ordinal,
        attempt_directory=attempt_directory,
        parent_execution_id=parent_execution_id,
    )
    store = registry._store
    entry = {
        "$schema": _ENTRY_SCHEMA,
        "child_execution_id": str(execution_id),
        "parent_execution_id": str(parent_execution_id),
        "campaign_id": registry._manifest.document["campaign_id"],
        "manifest_path": str(store.manifest_path.resolve(strict=True)),
        "manifest_sha256": manifest_digest(registry._manifest),
        "work_item_id": work_item_id,
        "attempt_id": attempt_id,
        "attempt_ordinal": attempt_ordinal,
        "attempt_relative_path": attempt_directory.relative_to(store.root).as_posix(),
        "runtime_context_sha256": _sha(prepared.context_path.read_bytes()),
    }
    raw = canonical_json_bytes(entry)
    project_entry = registry._project_root / "entries" / f"{execution_id}.json"
    campaign_entry = registry._campaign_root / "entries" / f"{execution_id}.json"
    for path in (project_entry, campaign_entry):
        durable_create(path, raw)
    return V030Child(execution_id, _sha(raw), project_entry, campaign_entry, prepared.context_path)


def _sha(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()

"""Origin-owned reverse dependency receipts for Simulation Campaign resumes."""

from __future__ import annotations

import hashlib
import json
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path

from booley.flows.sim.campaign_reports import is_report_link, write_campaign_json

DEPENDENCY_SCHEMA = "booley.simulation-campaign-dependency/v1"
DEPENDENCY_MAX_BYTES = 16 * 1024
_FIELDS = {
    "$schema",
    "campaign_id",
    "manifest_sha256",
    "origin_invocation",
    "origin_target",
    "dependent_invocation",
    "dependent_invocation_id",
}


@dataclass(frozen=True, slots=True)
class CampaignDependencyReceipt:
    """One validated reverse edge from an origin Campaign to a resume invocation."""

    campaign_id: str
    manifest_sha256: str
    origin_invocation: Path
    origin_target: Path
    dependent_invocation: Path
    dependent_invocation_id: int
    path: Path


def register_campaign_dependency(
    origin_manifest: Path,
    *,
    campaign_id: str,
    manifest_sha256: str,
    dependent_invocation: Path,
) -> Path:
    """Atomically register a reserved resume invocation under its locked origin."""
    manifest = _regular_manifest(origin_manifest)
    origin_target = manifest.parents[1].absolute()
    origin_invocation = manifest.parents[3].absolute()
    dependent = dependent_invocation.absolute()
    dependent_id = _positive_invocation(dependent)
    _uuid(campaign_id)
    _digest(manifest_sha256)
    key = hashlib.sha256(str(dependent).encode()).hexdigest()
    path = manifest.parent / "dependency-receipts" / f"{key}.json"
    document: dict[str, object] = {
        "$schema": DEPENDENCY_SCHEMA,
        "campaign_id": campaign_id,
        "manifest_sha256": manifest_sha256,
        "origin_invocation": str(origin_invocation),
        "origin_target": str(origin_target),
        "dependent_invocation": str(dependent),
        "dependent_invocation_id": dependent_id,
    }
    if path.exists() and _read_document(path) != document:
        raise ValueError("Simulation Campaign dependency receipt disagrees")
    write_campaign_json(path, document)
    return path


def read_campaign_dependencies(origin_manifest: Path) -> tuple[CampaignDependencyReceipt, ...]:
    """Read the bounded, exact-schema receipts owned by one canonical origin."""
    manifest = _regular_manifest(origin_manifest)
    directory = manifest.parent / "dependency-receipts"
    if not directory.exists():
        return ()
    if is_report_link(directory) or not directory.is_dir():
        raise ValueError("Simulation Campaign dependency registry is unsafe")
    paths = tuple(sorted(directory.iterdir()))
    if len(paths) > 1_000:
        raise ValueError("Simulation Campaign dependency registry is too large")
    receipts = tuple(_decode_receipt(path, manifest) for path in paths)
    return tuple(sorted(receipts, key=lambda item: str(item.dependent_invocation)))


def dependency_receipt_files(origin_manifest: Path) -> set[Path]:
    """Return authenticated receipt files for invocation inventory validation."""
    return {item.path.absolute() for item in read_campaign_dependencies(origin_manifest)}


def _decode_receipt(path: Path, manifest: Path) -> CampaignDependencyReceipt:
    document = _read_document(path)
    if set(document) != _FIELDS or document.get("$schema") != DEPENDENCY_SCHEMA:
        raise ValueError(f"Malformed Simulation Campaign dependency receipt: {path}")
    campaign_id = document["campaign_id"]
    manifest_sha256 = document["manifest_sha256"]
    if not isinstance(campaign_id, str) or not isinstance(manifest_sha256, str):
        raise ValueError(f"Malformed Simulation Campaign dependency receipt: {path}")
    _uuid(campaign_id)
    _digest(manifest_sha256)
    origin_invocation = _absolute_path(document["origin_invocation"], "origin invocation")
    origin_target = _absolute_path(document["origin_target"], "origin Target")
    dependent = _absolute_path(document["dependent_invocation"], "dependent invocation")
    dependent_id = document["dependent_invocation_id"]
    if type(dependent_id) is not int or dependent_id < 1 or dependent.name != str(dependent_id):
        raise ValueError(f"Malformed Simulation Campaign dependency receipt: {path}")
    current_target = manifest.parents[1].absolute()
    current_invocation = manifest.parents[3].absolute()
    if origin_target != current_target or origin_invocation != current_invocation:
        raise ValueError("Detached Campaign copy cannot authorize dependency deletion")
    return CampaignDependencyReceipt(
        campaign_id,
        manifest_sha256,
        origin_invocation,
        origin_target,
        dependent,
        dependent_id,
        path,
    )


def _read_document(path: Path) -> dict[str, object]:
    info = path.lstat()
    if (
        is_report_link(path)
        or not stat.S_ISREG(info.st_mode)
        or info.st_size > DEPENDENCY_MAX_BYTES
    ):
        raise ValueError(f"Unsafe Simulation Campaign dependency receipt: {path}")
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError(f"Malformed Simulation Campaign dependency receipt: {path}")
    return document


def _regular_manifest(path: Path) -> Path:
    manifest = path.absolute()
    if manifest.name != "manifest.json" or manifest.parent.name != "campaign":
        raise ValueError("Dependency origin must be an exact Campaign manifest")
    info = manifest.lstat()
    if is_report_link(manifest) or not stat.S_ISREG(info.st_mode):
        raise ValueError("Dependency origin manifest must be a regular file")
    return manifest


def _positive_invocation(path: Path) -> int:
    if not path.name.isdecimal() or int(path.name) < 1:
        raise ValueError("Dependent invocation must have a positive numeric identity")
    return int(path.name)


def _absolute_path(value: object, label: str) -> Path:
    if not isinstance(value, str):
        raise ValueError(f"Dependency {label} is invalid")
    path = Path(value)
    if not path.is_absolute() or path != path.absolute() or ".." in path.parts:
        raise ValueError(f"Dependency {label} is not canonical")
    return path


def _uuid(value: str) -> None:
    if str(uuid.UUID(value)) != value:
        raise ValueError("Dependency Campaign id is invalid")


def _digest(value: str) -> None:
    if len(value) != 71 or not value.startswith("sha256:"):
        raise ValueError("Dependency manifest digest is invalid")
    try:
        int(value[7:], 16)
    except ValueError as exc:
        raise ValueError("Dependency manifest digest is invalid") from exc

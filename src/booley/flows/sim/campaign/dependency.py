"""Origin-owned reverse dependency receipts for Simulation Campaign resumes."""

from __future__ import annotations

import hashlib
import stat
from dataclasses import dataclass
from pathlib import Path

from booley.core.boundary import (
    BoundaryError,
    require_int,
    require_sha256_digest,
    require_str_value,
    require_uuid4,
)
from booley.flows.sim.campaign_files import read_json_object
from booley.flows.sim.campaign_reports import is_report_link, write_campaign_json
from booley.flows.sim.record_layout import RECORD_DIRECTORY
from booley.runtime.booley_dir import crossed_booley_dir_entry

from .codec import MANIFEST_MAX_BYTES, raise_if_retired_schema

DEPENDENCY_SCHEMA = "booley.simulation-record-dependency/v1"
DEPENDENCY_MAX_BYTES = 16 * 1024
_FIELDS = {
    "$schema",
    "simulation_record_id",
    "manifest_sha256",
    "origin_invocation",
    "origin_target",
    "dependent_invocation",
    "dependent_invocation_id",
}


@dataclass(frozen=True, slots=True)
class CampaignDependencyReceipt:
    """One validated reverse edge from an origin Simulation Campaign to a resume."""

    campaign_id: str
    manifest_sha256: str
    origin_invocation: Path
    origin_target: Path
    dependent_invocation: Path
    dependent_invocation_id: int
    path: Path
    authoritative: bool


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
    _campaign_id(campaign_id)
    _manifest_digest(manifest_sha256)
    key = hashlib.sha256(str(dependent).encode()).hexdigest()
    path = manifest.parent / "dependency-receipts" / f"{key}.json"
    document: dict[str, object] = {
        "$schema": DEPENDENCY_SCHEMA,
        "simulation_record_id": campaign_id,
        "manifest_sha256": manifest_sha256,
        "origin_invocation": str(origin_invocation),
        "origin_target": str(origin_target),
        "dependent_invocation": str(dependent),
        "dependent_invocation_id": dependent_id,
    }
    if (
        path.exists()
        and read_json_object(
            path,
            limit=DEPENDENCY_MAX_BYTES,
            error=ValueError,
            booley_dir=crossed_booley_dir_entry(path),
        )
        != document
    ):
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
    booley_dir = crossed_booley_dir_entry(directory)
    receipts = tuple(_decode_receipt(path, manifest, booley_dir) for path in paths)
    return tuple(sorted(receipts, key=lambda item: str(item.dependent_invocation)))


def dependency_receipt_files(origin_manifest: Path) -> set[Path]:
    """Return authenticated receipt files for invocation inventory validation."""
    return {item.path.absolute() for item in read_campaign_dependencies(origin_manifest)}


def _decode_receipt(
    path: Path, manifest: Path, booley_dir: Path | None
) -> CampaignDependencyReceipt:
    document = read_json_object(
        path, limit=DEPENDENCY_MAX_BYTES, error=ValueError, booley_dir=booley_dir
    )
    raise_if_retired_schema(document.get("$schema"))
    if set(document) != _FIELDS or document.get("$schema") != DEPENDENCY_SCHEMA:
        raise ValueError(f"Malformed Simulation Campaign dependency receipt: {path}")
    try:
        campaign_id = require_str_value(
            document["simulation_record_id"], field="simulation_record_id"
        )
        manifest_sha256 = require_str_value(document["manifest_sha256"], field="manifest_sha256")
        dependent_id = require_int(
            document["dependent_invocation_id"], field="dependent_invocation_id"
        )
    except BoundaryError as exc:
        raise ValueError(f"Malformed Simulation Campaign dependency receipt: {path}") from exc
    _campaign_id(campaign_id)
    _manifest_digest(manifest_sha256)
    origin_invocation = _absolute_path(document["origin_invocation"], "origin invocation")
    origin_target = _absolute_path(document["origin_target"], "origin Target")
    dependent = _absolute_path(document["dependent_invocation"], "dependent invocation")
    if dependent_id < 1 or dependent.name != str(dependent_id):
        raise ValueError(f"Malformed Simulation Campaign dependency receipt: {path}")
    current_target = manifest.parents[1].absolute()
    current_invocation = manifest.parents[3].absolute()
    authoritative = origin_target == current_target and origin_invocation == current_invocation
    if authoritative:
        manifest_document = read_json_object(
            manifest, limit=MANIFEST_MAX_BYTES, error=ValueError, booley_dir=booley_dir
        )
        if manifest_document.get("simulation_record_id") != campaign_id:
            raise ValueError("Simulation Campaign dependency origin identity disagrees")
    return CampaignDependencyReceipt(
        campaign_id,
        manifest_sha256,
        origin_invocation,
        origin_target,
        dependent,
        dependent_id,
        path,
        authoritative,
    )


def _regular_manifest(path: Path) -> Path:
    manifest = path.absolute()
    if manifest.name != "manifest.json" or manifest.parent.name != RECORD_DIRECTORY:
        raise ValueError("Dependency origin must be an exact Simulation Campaign manifest")
    info = manifest.lstat()
    if is_report_link(manifest) or not stat.S_ISREG(info.st_mode):
        raise ValueError("Dependency origin manifest must be a regular file")
    return manifest


def _positive_invocation(path: Path) -> int:
    if not path.name.isdecimal() or int(path.name) < 1:
        raise ValueError("Dependent invocation must have a positive numeric identity")
    return int(path.name)


def _absolute_path(value: object, label: str) -> Path:
    try:
        parsed = require_str_value(value, field=f"dependency {label}")
    except BoundaryError as exc:
        raise ValueError(f"Dependency {label} is invalid") from exc
    path = Path(parsed)
    if not path.is_absolute() or path != path.absolute() or ".." in path.parts:
        raise ValueError(f"Dependency {label} is not canonical")
    return path


def _campaign_id(value: object) -> str:
    try:
        return require_uuid4(value, field="dependency campaign_id")
    except BoundaryError as exc:
        raise ValueError("Dependency Simulation Campaign id is invalid") from exc


def _manifest_digest(value: object) -> str:
    try:
        return require_sha256_digest(value, field="dependency manifest_sha256")
    except BoundaryError as exc:
        raise ValueError("Dependency manifest digest is invalid") from exc

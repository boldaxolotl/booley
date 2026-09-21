"""Authenticated private Simulator Bundle and executable snapshot operations."""

from __future__ import annotations

import hashlib
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import cast

from booley.flows.sim.campaign_durability import (
    durable_copy,
    durable_create,
    durable_directory,
)

from .codec import (
    SimulationCampaignIntegrityError,
    canonical_json_bytes,
    decode_executable_snapshot,
    encode_executable_snapshot,
)
from .model import ExecutableSnapshot, SimulatorBundle


def authenticate_bundle_artifacts(bundle: SimulatorBundle, root: Path) -> None:
    """Verify every declared artifact by type, byte count, and digest."""
    artifacts = cast(tuple[Mapping[str, object], ...], bundle.document["artifacts"])
    for artifact in artifacts:
        path = _regular_child(root, cast(str, artifact["path"]))
        size, digest = _file_identity(path)
        if size != artifact["bytes"] or digest != artifact["sha256"]:
            raise SimulationCampaignIntegrityError(f"bundle artifact changed: {path}")


def create_executable_snapshot(
    *,
    bundle: SimulatorBundle,
    bundle_root: Path,
    snapshot_root: Path,
    campaign_id: str,
    manifest_sha256: str,
    workload_sha256: str,
    work_item_id: str,
    attempt_id: str,
    build_result: Mapping[str, object],
    created_at: str,
) -> ExecutableSnapshot:
    """Copy (never hardlink) the executable surface into an attempt directory."""
    authenticate_bundle_artifacts(bundle, bundle_root)
    if snapshot_root.exists():
        raise SimulationCampaignIntegrityError("executable snapshot already exists")
    durable_directory(snapshot_root)
    snapshot_root.chmod(0o700)
    copied: list[dict[str, object]] = []
    artifacts = cast(tuple[Mapping[str, object], ...], bundle.document["artifacts"])
    for artifact in artifacts:
        if artifact["kind"] == "runtime_input":
            continue
        relative = cast(str, artifact["path"])
        source = _regular_child(bundle_root, relative)
        destination = snapshot_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        mode = stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH
        if artifact["kind"] == "simulator_executable":
            mode |= stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
        durable_copy(source, destination, mode=mode)
        if source.stat(follow_symlinks=False).st_ino == destination.stat(
            follow_symlinks=False
        ).st_ino:
            raise SimulationCampaignIntegrityError("snapshot artifact must not be a hardlink")
        size, digest = _file_identity(destination)
        if size != artifact["bytes"] or digest != artifact["sha256"]:
            raise SimulationCampaignIntegrityError("snapshot copy authentication failed")
        copied.append(dict(artifact))
    inventory = _digest(copied)
    document = {
        "$schema": "booley.executable-snapshot/v1",
        "campaign_id": campaign_id,
        "manifest_sha256": manifest_sha256,
        "workload_sha256": workload_sha256,
        "work_item_id": work_item_id,
        "attempt_id": attempt_id,
        "bundle_id": bundle.document["bundle_id"],
        "build_result": dict(build_result),
        "created_at": created_at,
        "artifacts": copied,
        "inventory_sha256": inventory,
    }
    value = decode_executable_snapshot(canonical_json_bytes(document))
    manifest_path = snapshot_root / "snapshot.json"
    _create_file(manifest_path, encode_executable_snapshot(value))
    return value


def authenticate_executable_snapshot(
    snapshot: ExecutableSnapshot, snapshot_root: Path
) -> str:
    """Authenticate a snapshot before launch or after process-tree death."""
    artifacts = cast(tuple[Mapping[str, object], ...], snapshot.document["artifacts"])
    for artifact in artifacts:
        path = _regular_child(snapshot_root, cast(str, artifact["path"]))
        size, digest = _file_identity(path)
        if size != artifact["bytes"] or digest != artifact["sha256"]:
            raise SimulationCampaignIntegrityError(f"executable snapshot changed: {path}")
    return cast(str, snapshot.document["inventory_sha256"])


def _regular_child(root: Path, relative: str) -> Path:
    path = root / relative
    try:
        path.absolute().relative_to(root.absolute())
    except ValueError as exc:
        raise SimulationCampaignIntegrityError("bundle artifact escapes its root") from exc
    try:
        info = path.lstat()
    except OSError as exc:
        raise SimulationCampaignIntegrityError(f"bundle artifact is unavailable: {path}") from exc
    if not stat.S_ISREG(info.st_mode):
        raise SimulationCampaignIntegrityError(f"bundle artifact is not regular: {path}")
    current = root
    for part in Path(relative).parts[:-1]:
        current /= part
        if current.is_symlink():
            raise SimulationCampaignIntegrityError(f"bundle path contains a link: {current}")
    return path


def _file_identity(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(64 * 1024), b""):
            size += len(chunk)
            digest.update(chunk)
    return size, "sha256:" + digest.hexdigest()


def _digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(canonical_json_bytes(value).rstrip(b"\n")).hexdigest()


def _create_file(path: Path, raw: bytes) -> None:
    durable_create(path, raw, mode=0o400)


__all__ = [
    "authenticate_bundle_artifacts",
    "authenticate_executable_snapshot",
    "create_executable_snapshot",
]

"""Verification of a ready Simulator Bundle where its attempts launch it."""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import cast

from booley.flows.sim.record_files import stream_digest

from .codec import SimulationRecordIntegrityError, validate_relative_path
from .model import BundleBuildResult, SimulatorBundle

_BUNDLE_ARTIFACT_KINDS = frozenset(
    {"simulator_executable", "shared_library", "runtime_data", "runtime_input"}
)


def require_bundle_inventory(bundle: SimulatorBundle) -> None:
    """Refuse an inventory with an unknown kind, a repeated path or no executable.

    The decoder checks no nested member (#1743); in-place verification relies on these.
    """
    artifacts = cast(tuple[Mapping[str, object], ...], bundle.document["artifacts"])
    paths = [artifact["path"] for artifact in artifacts]
    if len(set(paths)) != len(paths):
        raise SimulationRecordIntegrityError("bundle artifact paths must be unique")
    if any(artifact["kind"] not in _BUNDLE_ARTIFACT_KINDS for artifact in artifacts):
        raise SimulationRecordIntegrityError("bundle artifact kind is invalid")
    if not any(artifact["kind"] == "simulator_executable" for artifact in artifacts):
        raise SimulationRecordIntegrityError("ready bundle requires a simulator executable")


def verify_bundle_in_place(
    bundle: SimulatorBundle, build_result: BundleBuildResult, root: Path
) -> str:
    """Verify the ready bundle at *root* where attempts launch it; return its inventory digest.

    Checks every declared artifact by type, byte count and digest, the bundle
    manifest and build evidence against their recorded digests, and that the
    directory holds no other entry. The recorded inventory digest is returned:
    it names the inventory that passed, not a fresh measurement.
    """
    require_bundle_inventory(bundle)
    artifacts = cast(tuple[Mapping[str, object], ...], bundle.document["artifacts"])
    allowed = {"build-attempt.json", "build-result.json"}
    for artifact in artifacts:
        _verify_file(root, artifact, "bundle artifact")
        allowed.add(cast(str, artifact["path"]))
    described = cast(Mapping[str, object], build_result.document["bundle"])
    manifest = {
        "path": described["manifest_path"],
        "bytes": described["manifest_bytes"],
        "sha256": described["manifest_sha256"],
    }
    _verify_file(root, manifest, "bundle manifest")
    allowed.add(cast(str, manifest["path"]))
    for reference in cast(tuple[Mapping[str, object], ...], build_result.document["evidence"]):
        _verify_file(root, reference, "bundle evidence")
        allowed.add(cast(str, reference["path"]))
    _require_only(root, allowed)
    return cast(str, bundle.document["inventory_sha256"])


def _verify_file(root: Path, declared: Mapping[str, object], label: str) -> None:
    path = _regular_child(root, cast(str, declared["path"]))
    if path.lstat().st_nlink != 1:
        raise SimulationRecordIntegrityError(f"bundle artifact has another hard link: {path}")
    try:
        size, digest = stream_digest(path)
    except OSError as exc:
        raise SimulationRecordIntegrityError(f"bundle artifact is unreadable: {path}") from exc
    if size != declared["bytes"] or digest != declared["sha256"]:
        raise SimulationRecordIntegrityError(f"{label} changed: {path}")


def _require_only(root: Path, allowed_files: set[str]) -> None:
    """Refuse any file, directory or link under *root* that no allowed path explains."""
    allowed_directories = {
        parent.as_posix()
        for relative in allowed_files
        for parent in PurePosixPath(relative).parents
        if parent != PurePosixPath(".")
    }
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as scan:
                entries = list(scan)
        except OSError as exc:
            raise SimulationRecordIntegrityError(
                f"bundle directory is unreadable: {directory}"
            ) from exc
        for entry in entries:
            relative = Path(entry.path).relative_to(root).as_posix()
            if entry.is_symlink():
                raise SimulationRecordIntegrityError(f"undeclared bundle entry: {entry.path}")
            if entry.is_dir(follow_symlinks=False) and relative in allowed_directories:
                pending.append(Path(entry.path))
            elif not (entry.is_file(follow_symlinks=False) and relative in allowed_files):
                raise SimulationRecordIntegrityError(f"undeclared bundle entry: {entry.path}")


def _regular_child(root: Path, relative: str) -> Path:
    path = root / validate_relative_path(relative)
    try:
        path.absolute().relative_to(root.absolute())
    except ValueError as exc:
        raise SimulationRecordIntegrityError("bundle artifact escapes its root") from exc
    try:
        info = path.lstat()
    except OSError as exc:
        raise SimulationRecordIntegrityError(f"bundle artifact is unavailable: {path}") from exc
    if not stat.S_ISREG(info.st_mode):
        raise SimulationRecordIntegrityError(f"bundle artifact is not regular: {path}")
    current = root
    for part in Path(relative).parts[:-1]:
        current /= part
        if current.is_symlink():
            raise SimulationRecordIntegrityError(f"bundle path contains a link: {current}")
    return path


__all__ = ["require_bundle_inventory", "verify_bundle_in_place"]

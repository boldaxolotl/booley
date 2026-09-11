"""Recoverable host-issued Session Runtime specification state."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from booley.runtime import devcontainer
from booley.runtime import session_issuance as runtime_spec


@dataclass(frozen=True)
class SessionSpecSnapshot:
    """Recoverable host-spec state retained across a runtime replacement."""

    spec_path: Path
    spec_content: bytes | None
    spec_mode: int
    stamp_path: Path
    stamp_content: bytes | None
    stamp_mode: int
    image_id: str | None


def capture_session_spec(project_root: Path) -> SessionSpecSnapshot:
    """Capture the spec, issuance stamp, and prior immutable image identity."""
    spec_path = devcontainer.devcontainer_path(project_root)
    stamp_path = runtime_spec.stamp_path(project_root)
    stamp_read_path = runtime_spec.recovery_stamp_path(project_root)
    spec_content = spec_path.read_bytes() if spec_path.is_file() else None
    stamp_content = stamp_read_path.read_bytes() if stamp_read_path.is_file() else None
    image_id = None
    if spec_content is not None:
        try:
            raw_image = json.loads(spec_content).get("image")
        except (json.JSONDecodeError, AttributeError):
            raw_image = None
        if isinstance(raw_image, str) and raw_image.startswith("sha256:"):
            image_id = raw_image
    return SessionSpecSnapshot(
        spec_path,
        spec_content,
        stat.S_IMODE(spec_path.stat().st_mode) if spec_content is not None else 0o644,
        stamp_path,
        stamp_content,
        stat.S_IMODE(stamp_read_path.stat().st_mode) if stamp_content is not None else 0o600,
        image_id,
    )


def _restore_snapshot_file(path: Path, content: bytes | None, mode: int) -> None:
    if path.is_symlink() or path.parent.is_symlink():
        raise OSError(f"recovery path must not be a symlink: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    if content is None:
        path.unlink(missing_ok=True)
        _fsync_snapshot_directory(path.parent)
        return
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(temporary)
    try:
        if os.name != "nt":
            os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temp_path.replace(path)
        if os.name == "nt":
            path.chmod(mode)
        _fsync_snapshot_directory(path.parent)
    finally:
        temp_path.unlink(missing_ok=True)


def _fsync_snapshot_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def restore_session_spec(project_root: Path, snapshot: SessionSpecSnapshot) -> None:
    """Restore the prior host issuance after Session replacement rolled back."""
    errors: list[str] = []
    if snapshot.image_id is not None:
        try:
            result = subprocess.run(
                ["docker", "tag", snapshot.image_id, runtime_spec.keeper_image(project_root)],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            errors.append(f"Runtime Image keeper: {exc}")
        else:
            if result.returncode != 0:
                errors.append(
                    "Runtime Image keeper: " + (result.stderr.strip() or "docker tag failed")
                )
    for label, path, content, mode in (
        ("Session spec", snapshot.spec_path, snapshot.spec_content, snapshot.spec_mode),
        ("issuance stamp", snapshot.stamp_path, snapshot.stamp_content, snapshot.stamp_mode),
    ):
        try:
            _restore_snapshot_file(path, content, mode)
        except OSError as exc:
            errors.append(f"{label}: {exc}")
    if errors:
        raise RuntimeError("could not fully restore prior host issuance: " + "; ".join(errors))

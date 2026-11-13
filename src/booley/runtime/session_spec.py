"""Recoverable host-issued Sandbox specification state."""

from __future__ import annotations

import json
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

from booley.runtime import devcontainer
from booley.runtime import session_issuance as runtime_spec
from booley.runtime.atomic_files import atomic_replace_bytes, durable_unlink
from booley.runtime.platform_paths import run_captured_bounded


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
    if content is None:
        durable_unlink(path)
        return
    atomic_replace_bytes(path, content, mode=mode)


def restore_session_spec(project_root: Path, snapshot: SessionSpecSnapshot) -> None:
    """Restore the prior host issuance after Session replacement rolled back."""
    errors: list[str] = []
    first_file_error: OSError | None = None
    if snapshot.image_id is not None:
        try:
            result = run_captured_bounded(
                ["docker", "tag", snapshot.image_id, runtime_spec.keeper_image(project_root)],
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            errors.append(f"Sandbox Image keeper: {exc}")
        else:
            if result.returncode != 0:
                errors.append(
                    "Sandbox Image keeper: " + (result.stderr.strip() or "docker tag failed")
                )
    for label, path, content, mode in (
        ("Session spec", snapshot.spec_path, snapshot.spec_content, snapshot.spec_mode),
        ("issuance stamp", snapshot.stamp_path, snapshot.stamp_content, snapshot.stamp_mode),
    ):
        try:
            _restore_snapshot_file(path, content, mode)
        except OSError as exc:
            errors.append(f"{label}: {exc}")
            first_file_error = first_file_error or exc
    if errors:
        raise RuntimeError(
            "could not fully restore prior host issuance: " + "; ".join(errors)
        ) from first_file_error

"""Internal creation provenance in the selected Project configuration."""

from __future__ import annotations

import hashlib
import os
import stat
import tempfile
from pathlib import Path
from typing import Literal

from booley.harness.setup.plan import (
    InitAction,
    InitFilesystemRequest,
    InitPreconditionError,
    InitTarget,
    apply_init_plan,
    plan_init_filesystem,
)

ProjectOrigin = Literal["scaffold", "port"]
_PREFIX = "# booley-init-origin:"


class _ConfigProbe:
    def is_tracked(self, _root: Path, _path: Path) -> bool:
        return False


def _parse_origin(content: bytes) -> ProjectOrigin | None:
    markers = [line for line in content.decode("utf-8").splitlines() if line.startswith(_PREFIX)]
    if not markers:
        return None
    if len(markers) != 1 or markers[0] not in (f"{_PREFIX} scaffold", f"{_PREFIX} port"):
        raise ValueError(
            "invalid booley-init-origin comment; retain exactly one scaffold or port marker"
        )
    return "scaffold" if markers[0] == f"{_PREFIX} scaffold" else "port"


def read_project_origin(config: Path) -> ProjectOrigin | None:
    """Read explicit provenance only; legacy configuration has unknown origin."""
    if not config.exists():
        return None
    return _parse_origin(config.read_bytes())


def publish_project_origin(config: Path, origin: ProjectOrigin, *, expected: bytes) -> None:
    """Publish after successful creation, refusing a changed configuration."""
    if origin not in ("scaffold", "port"):
        raise ValueError("Project origin must be scaffold or port")
    previous = _parse_origin(expected)
    marker = f"{_PREFIX} {origin}".encode()
    if previous == origin:
        content = expected
    elif previous is None:
        content = marker + b"\n" + expected
    else:
        content = expected.replace(f"{_PREFIX} {previous}".encode(), marker, 1)
    if config.read_bytes() != expected:
        raise InitPreconditionError(
            f"{config} changed before origin publication; rerun initialization"
        )
    target = InitTarget.write_file(
        config,
        content,
        owner_marker=None,
        reason="record successful Project creation",
        recognized_legacy_digests=(hashlib.sha256(expected).hexdigest(),),
    )
    plan = plan_init_filesystem(
        InitFilesystemRequest(root=config.parent, targets=(target,)), _ConfigProbe()
    )

    def publish(action: InitAction) -> None:
        _atomic_publish(action.path, action.target.content, expected)

    apply_init_plan(plan, publish)


def _atomic_publish(config: Path, content: bytes, expected: bytes) -> None:
    mode = stat.S_IMODE(config.stat().st_mode)
    descriptor, temporary = tempfile.mkstemp(prefix=".origin-", dir=config.parent)
    path = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        path.chmod(mode)
        if config.read_bytes() != expected:
            raise InitPreconditionError(
                f"{config} changed before origin publication; rerun initialization"
            )
        path.replace(config)
    finally:
        path.unlink(missing_ok=True)

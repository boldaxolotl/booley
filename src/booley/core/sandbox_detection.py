"""Whether this process runs inside a Booley Sandbox: the one shared classification.

Stdlib and ``booley.core`` only, so configuration, Runtime and commands ask the
same question.
"""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath

from booley.core.sandbox_layout import (
    BOOLEY_DIR_TARGET,
    DEVCONTAINER_TARGET,
    PROJECT_DATA_MOUNT_PATH,
)

_CONTAINER_MARKER_FILES = (Path("/.dockerenv"), Path("/run/.containerenv"))


def container_marker_file() -> bool:
    """Return whether the container runtime left its marker file in this filesystem."""
    return any(marker.exists() for marker in _CONTAINER_MARKER_FILES)


def inside_session_runtime() -> bool:
    """Return whether this process runs inside a Booley Sandbox.

    ``BOOLEY_CONTAINER=1`` declares a Sandbox. ``BOOLEY_CONTAINER=0`` declares a
    container that is itself a Booley host; only issued-Sandbox mount points
    outrank it. Any other value falls back to the container marker files.
    """
    declared = os.environ.get("BOOLEY_CONTAINER")
    if declared == "1":
        return True
    if declared == "0":
        return issued_mount_points(unreadable=False)
    return container_marker_file()


_MOUNTINFO = Path("/proc/self/mountinfo")
#: Mount points only Session Issuance creates; a process cannot unmount them unprivileged.
_ISSUED_MOUNT_TARGETS = (BOOLEY_DIR_TARGET, PROJECT_DATA_MOUNT_PATH, DEVCONTAINER_TARGET)


def issued_mount_points(*, unreadable: bool) -> bool:
    """Return whether any mount point is, or lies under, an issued-Sandbox target.

    *unreadable* is the verdict for an unreadable mountinfo. Inside a Sandbox it
    is ``True``: the only consequence of a false "issued" verdict is a refusal.
    Under ``BOOLEY_CONTAINER=0`` it is ``False``: no table is no evidence.
    """
    try:
        lines = _MOUNTINFO.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return unreadable
    for line in lines:
        fields = line.split()
        if len(fields) < 5:
            continue
        # Field 5 is the mount point; spaces in it are escaped as ``\040``.
        mount_point = PurePosixPath(fields[4].replace("\\040", " "))
        if any(mount_point.is_relative_to(target) for target in _ISSUED_MOUNT_TARGETS):
            return True
    return False

"""The one fake of the Sandbox venue: whether a test runs on the host or in a Sandbox.

It sets the venue check's inputs (``BOOLEY_CONTAINER``, the mount table and the
container-marker checks), so the machine running the tests does not decide.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from booley.core import sandbox_detection
from booley.runtime.runtime_context import RuntimeLocation

_MOUNT_TABLES = Path(__file__).parent / "fixtures" / "mountinfo"


def _mount_table(location: RuntimeLocation) -> Path:
    issued = location is RuntimeLocation.ISSUED_SANDBOX
    return _MOUNT_TABLES / ("issued" if issued else "plain")


def _inputs(
    location: RuntimeLocation, container_host: bool
) -> tuple[str | None, Path | None, bool]:
    """Return ``BOOLEY_CONTAINER``, the mount table and the marker verdict of a venue."""
    if location is not RuntimeLocation.HOST:
        return "1", _mount_table(location), True
    if container_host:
        return "0", _mount_table(location), True
    return None, None, False


def point_mounts(monkeypatch: pytest.MonkeyPatch, location: RuntimeLocation) -> None:
    """Give the venue check the mount table of *location*; ``BOOLEY_CONTAINER`` stays."""
    table = _mount_table(location)
    monkeypatch.setattr(sandbox_detection, "_MOUNTINFO", table)  # boundary: host/Sandbox mounts


def place(
    monkeypatch: pytest.MonkeyPatch, location: RuntimeLocation, *, container_host: bool = False
) -> None:
    """Run the test at *location*; ``container_host`` makes the host a container."""
    declared, table, marker = _inputs(location, container_host)
    if declared is None:
        monkeypatch.delenv("BOOLEY_CONTAINER", raising=False)
    else:
        monkeypatch.setenv("BOOLEY_CONTAINER", declared)
    if table is not None:
        point_mounts(monkeypatch, location)
    # The marker checks read /.dockerenv on the real filesystem.
    markers = (Path(__file__),) * marker
    monkeypatch.setattr(sandbox_detection, "_CONTAINER_MARKER_FILES", markers)  # boundary: venue


def place_child(location: RuntimeLocation) -> None:
    """``place`` for a child Python process, which exits before anything needs undoing."""
    declared, table, marker = _inputs(location, container_host=False)
    if declared is None:
        os.environ.pop("BOOLEY_CONTAINER", None)
    else:
        os.environ["BOOLEY_CONTAINER"] = declared
    # The same host/Sandbox boundary as ``place``: the mount table and marker checks.
    if table is not None:
        sandbox_detection._MOUNTINFO = table
    sandbox_detection._CONTAINER_MARKER_FILES = (Path(__file__),) * marker

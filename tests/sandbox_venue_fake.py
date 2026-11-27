"""The one fake of the Sandbox venue: whether a test runs on the host or in a Sandbox.

It sets the venue check's inputs (``BOOLEY_CONTAINER``, the mount table and the
container-marker checks), so the machine running the tests does not decide.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from booley.runtime import runtime_context
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
    # private: /proc/self/mountinfo is the kernel's mount table; no public input moves it.
    monkeypatch.setattr(runtime_context, "_MOUNTINFO", _mount_table(location))


def place(
    monkeypatch: pytest.MonkeyPatch, location: RuntimeLocation, *, container_host: bool = False
) -> None:
    """Run the test at *location*; ``container_host`` makes the host a container."""
    from booley.config import agent
    from booley.runtime import _codex_backend

    declared, table, marker = _inputs(location, container_host)
    if declared is None:
        monkeypatch.delenv("BOOLEY_CONTAINER", raising=False)
    else:
        monkeypatch.setenv("BOOLEY_CONTAINER", declared)
    if table is not None:
        point_mounts(monkeypatch, location)
    # private: the marker checks read /.dockerenv on the real filesystem; no public
    # input points them at a fixture.
    monkeypatch.setattr(runtime_context, "_CONTAINER_MARKER_FILES", (Path(__file__),) * marker)
    monkeypatch.setattr(agent, "_inside_container", lambda: marker)
    monkeypatch.setattr(_codex_backend, "_inside_container", lambda: marker)


def place_child(location: RuntimeLocation) -> None:
    """``place`` for a child Python process, which exits before anything needs undoing."""
    from booley.config import agent
    from booley.runtime import _codex_backend

    declared, table, marker = _inputs(location, container_host=False)
    if declared is None:
        os.environ.pop("BOOLEY_CONTAINER", None)
    else:
        os.environ["BOOLEY_CONTAINER"] = declared
    if table is not None:
        runtime_context._MOUNTINFO = table
    runtime_context._CONTAINER_MARKER_FILES = (Path(__file__),) * marker
    agent._inside_container = _codex_backend._inside_container = lambda: marker

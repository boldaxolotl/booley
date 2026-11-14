"""Simulate a Sandbox mount point change for stored-path translation tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from booley.runtime import sandbox_layout


def simulate_mount_change(monkeypatch: pytest.MonkeyPatch, current: Path, former: Path) -> str:
    """Make *current* the mount point and *former* the one older records name.

    Writes the former-mount history the way a reissue leaves it (`.runtime/
    sandbox-mounts.json` in the Booley Directory). Returns the former mount as
    the string records store.
    """
    monkeypatch.setattr(sandbox_layout, "BOOLEY_DIR_TARGET", str(current))
    history = current / ".runtime" / "sandbox-mounts.json"
    history.parent.mkdir(parents=True, exist_ok=True)
    history.write_bytes(json.dumps({"schema": 1, "mounts": [str(former)]}).encode("utf-8"))
    return str(former)


def respell(path: Path, current: Path, former: str) -> None:
    """Rewrite *path* so every spelling of *current* reads *former*, as an older writer left it."""
    old = json.dumps(str(current))[1:-1]
    new = json.dumps(former)[1:-1]
    path.write_bytes(path.read_bytes().replace(old.encode("utf-8"), new.encode("utf-8")))

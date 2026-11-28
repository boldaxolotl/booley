"""Simulate a Sandbox mount point change for stored-path translation tests."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from booley.core import sandbox_layout
from tests.subprocess_budget import run_bounded_child


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


def respell_registration(admin: Path, current: Path, former: Path, *, relative: bool) -> None:
    """Rewrite one worktree registration's pointers as Git wrote them at the *former* mount.

    The checkouts stay where they are; only the pointers (the checkout's ``.git``
    file, the registration's ``gitdir`` and a relative ``core.worktree``) are
    spelled as if *current* were still at *former*, absolute or relative. A checkout
    that is already gone leaves its ``gitdir`` as the only pointer.
    """
    mount = current.resolve()

    def moved(path: Path) -> Path:
        return former / path.relative_to(mount) if path.is_relative_to(mount) else path

    def spell(target: Path, base: Path) -> str:
        if relative:
            return os.path.relpath(moved(target), moved(base)).replace(os.sep, "/")
        return str(moved(target))

    admin = admin.resolve()
    gitdir = admin / "gitdir"
    marker = Path(os.path.normpath(admin / gitdir.read_text(encoding="utf-8").strip()))
    gitdir.write_bytes((spell(marker, admin) + "\n").encode())
    if not marker.exists():
        return
    stamp = marker.stat().st_mtime_ns
    marker.write_bytes(f"gitdir: {spell(admin, marker.parent)}\n".encode())
    os.utime(marker, ns=(stamp, stamp))
    config = admin / "config.worktree"
    if config.is_file() and _config_has_core_worktree(config):
        run_bounded_child(
            ["git", "config", "--file", str(config), "core.worktree", spell(marker.parent, admin)],
            check=True,
        )


def _config_has_core_worktree(config: Path) -> bool:
    found = run_bounded_child(["git", "config", "--file", str(config), "--get", "core.worktree"])
    return found.returncode == 0

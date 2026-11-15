"""``recorded_former_mounts`` joins the mount history with the issued spec's mount."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from booley.runtime import mount_history


def _history(source: Path, mounts: list[str]) -> None:
    path = source / mount_history.MOUNT_HISTORY
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(json.dumps({"schema": 1, "mounts": mounts}).encode("utf-8"))


def _spec(path: Path, mount: str) -> Path:
    path.write_bytes(json.dumps({"remoteEnv": {"BOOLEY_DIR": mount}}).encode("utf-8"))
    return path


def test_joins_history_and_issued_spec_without_duplicates(tmp_path: Path) -> None:
    _history(tmp_path, ["/older"])
    spec = _spec(tmp_path / "devcontainer.json", "/former-mount")

    assert mount_history.recorded_former_mounts(tmp_path, spec) == ("/older", "/former-mount")
    _history(tmp_path, ["/older", "/former-mount"])
    assert mount_history.recorded_former_mounts(tmp_path, spec) == ("/older", "/former-mount")


def test_current_mount_in_the_issued_spec_is_not_former(tmp_path: Path) -> None:
    spec = _spec(tmp_path / "devcontainer.json", "/booley")

    assert mount_history.recorded_former_mounts(tmp_path, spec) == ()


def test_absent_history_and_spec_contribute_nothing(tmp_path: Path) -> None:
    assert mount_history.recorded_former_mounts(tmp_path, tmp_path / "missing.json") == ()


def test_unreadable_spec_is_a_history_error(tmp_path: Path) -> None:
    with pytest.raises(mount_history.MountHistoryError, match="cannot read"):
        mount_history.recorded_former_mounts(tmp_path, tmp_path)

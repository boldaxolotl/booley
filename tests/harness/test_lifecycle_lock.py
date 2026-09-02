"""Tests for host-wide Docker lifecycle serialization."""

from __future__ import annotations

import os

import pytest

from booley.harness import lifecycle_lock
from booley.runtime.file_lock import LockContentionError


def test_host_lifecycle_lock_records_owner(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(lifecycle_lock, "config_dir", lambda: tmp_path)

    with lifecycle_lock.host_lifecycle_lock("session refresh"):
        pass

    owner = (tmp_path / "locks" / "docker-lifecycle.lock").read_text(encoding="utf-8")
    assert owner == f"pid={os.getpid()} operation=session refresh\n"


def test_host_lifecycle_lock_reports_current_owner(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(lifecycle_lock, "config_dir", lambda: tmp_path)
    lock_path = tmp_path / "locks" / "docker-lifecycle.lock"
    lock_path.parent.mkdir()
    lock_path.write_text("pid=41 operation=booley init\n", encoding="utf-8")

    def contend(_handle):
        raise LockContentionError("busy")

    monkeypatch.setattr(lifecycle_lock, "acquire_file_lock", contend)

    with (
        pytest.raises(
            lifecycle_lock.LifecycleLockError,
            match=r"pid=41 operation=booley init.*retry after it finishes",
        ),
        lifecycle_lock.host_lifecycle_lock("session up"),
    ):
        pass


def test_host_lifecycle_lock_does_not_reclassify_body_error(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(lifecycle_lock, "config_dir", lambda: tmp_path)
    body_error = LockContentionError("inner resource is busy")

    with (
        pytest.raises(LockContentionError, match="inner resource is busy") as raised,
        lifecycle_lock.host_lifecycle_lock("session refresh"),
    ):
        raise body_error

    assert raised.value is body_error

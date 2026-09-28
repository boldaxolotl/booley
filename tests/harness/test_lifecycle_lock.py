"""Tests for host-wide Docker lifecycle serialization."""

from __future__ import annotations

import multiprocessing
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from booley.runtime import lifecycle_lock
from booley.runtime.file_lock import LockContentionError, LockTimeoutError


def _hold_lock(path_text, ready, release) -> None:
    from booley.runtime.file_lock import acquire_file_lock, release_file_lock

    path = Path(path_text)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        acquire_file_lock(handle)
        handle.seek(0)
        handle.truncate()
        handle.write("pid=41 operation=booley init\n")
        handle.flush()
        ready.set()
        release.wait(5)
        release_file_lock(handle)


def test_host_lifecycle_lock_records_owner(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(lifecycle_lock, "config_dir", lambda: tmp_path)

    with lifecycle_lock.host_lifecycle_lock("session refresh"):
        pass

    owner = (tmp_path / "locks" / "docker-lifecycle.lock").read_text(encoding="utf-8")
    assert owner == f"pid={os.getpid()} operation=session refresh\n"


def test_host_lifecycle_lock_waits_for_current_owner(tmp_path, monkeypatch, caplog) -> None:
    monkeypatch.setattr(lifecycle_lock, "config_dir", lambda: tmp_path)
    lock_path = tmp_path / "locks" / "docker-lifecycle.lock"
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    release = context.Event()
    holder = context.Process(target=_hold_lock, args=(str(lock_path), ready, release))
    holder.start()
    waiting = threading.Event()
    real_wait = lifecycle_lock.wait_for_file_lock

    def observed_wait(handle, *, timeout_s, on_wait=None) -> None:
        waiting.set()
        real_wait(handle, timeout_s=timeout_s, on_wait=on_wait)

    def acquire() -> None:
        with lifecycle_lock.host_lifecycle_lock("session up"):
            pass

    monkeypatch.setattr(lifecycle_lock, "wait_for_file_lock", observed_wait)
    try:
        assert ready.wait(5)
        with ThreadPoolExecutor(max_workers=1) as executor:
            acquired = executor.submit(acquire)
            assert waiting.wait(2)
            assert not acquired.done()
            release.set()
            acquired.result(timeout=5)
    finally:
        release.set()
        holder.join(5)
        if holder.is_alive():
            holder.terminate()
            holder.join(5)

    assert "pid=41 operation=booley init" in caplog.text
    assert f"waiting up to {lifecycle_lock.DEFAULT_WAIT_TIMEOUT_SECONDS:g}s" in caplog.text
    owner = lock_path.read_text(encoding="utf-8")
    assert owner == f"pid={os.getpid()} operation=session up\n"


def test_host_lifecycle_lock_does_not_reclassify_body_error(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(lifecycle_lock, "config_dir", lambda: tmp_path)
    body_error = LockContentionError("inner resource is busy")

    with (
        pytest.raises(LockContentionError, match="inner resource is busy") as raised,
        lifecycle_lock.host_lifecycle_lock("session refresh"),
    ):
        raise body_error

    assert raised.value is body_error


def test_host_lifecycle_lock_allows_explicit_fail_fast(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(lifecycle_lock, "config_dir", lambda: tmp_path)

    def contend(_handle) -> None:
        raise LockContentionError("busy")

    monkeypatch.setattr(lifecycle_lock, "acquire_file_lock", contend)

    with (
        pytest.raises(
            lifecycle_lock.LifecycleLockError,
            match="retry after it finishes",
        ),
        lifecycle_lock.host_lifecycle_lock("session up", wait_timeout_s=None),
    ):
        pass


def test_host_lifecycle_lock_reports_owner_on_default_timeout(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(lifecycle_lock, "config_dir", lambda: tmp_path)
    lock_path = tmp_path / "locks" / "docker-lifecycle.lock"
    lock_path.parent.mkdir()
    lock_path.write_text("pid=41 operation=session refresh\n", encoding="utf-8")

    timeouts: list[float] = []

    def timeout(_handle, *, timeout_s, on_wait=None) -> None:
        del on_wait
        timeouts.append(timeout_s)
        raise LockTimeoutError("busy")

    monkeypatch.setattr(lifecycle_lock, "wait_for_file_lock", timeout)

    with (
        pytest.raises(
            lifecycle_lock.LifecycleLockError,
            match=(
                r"pid=41 operation=session refresh.*timed out after waiting "
                rf"{lifecycle_lock.DEFAULT_WAIT_TIMEOUT_SECONDS:g}s"
            ),
        ),
        lifecycle_lock.host_lifecycle_lock("session command"),
    ):
        pass

    assert timeouts == [lifecycle_lock.DEFAULT_WAIT_TIMEOUT_SECONDS]


def test_lock_owner_falls_back_when_contended_file_cannot_be_read() -> None:
    class UnreadableOwner:
        def seek(self, _offset: int) -> None:
            raise OSError("locked byte cannot be read")

    assert lifecycle_lock._lock_owner(UnreadableOwner()) == "another Booley command"

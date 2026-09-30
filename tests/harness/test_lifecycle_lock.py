"""Tests for host-wide Docker lifecycle serialization."""

from __future__ import annotations

import os
import stat
from concurrent.futures import ThreadPoolExecutor
from io import StringIO

import pytest

from booley.runtime import lifecycle_lock
from booley.runtime.file_lock import LockContentionError, LockTimeoutError
from tests.lifecycle_lock_support import held_lifecycle_lock, observe_lifecycle_contention


def test_host_lifecycle_lock_records_owner(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(lifecycle_lock, "config_dir", lambda: tmp_path)

    with lifecycle_lock.host_lifecycle_lock("session refresh"):
        pass

    owner = (tmp_path / "locks" / "docker-lifecycle.lock").read_text(encoding="utf-8")
    assert owner == f"pid={os.getpid()} operation=session refresh\n"


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode policy")
def test_host_lifecycle_lock_creates_private_booley_directories_below_shared_xdg_root(
    tmp_path, monkeypatch
) -> None:
    xdg_root = tmp_path / "config"
    xdg_root.mkdir(mode=0o755)
    xdg_root.chmod(0o755)
    booley_config = xdg_root / "booley"
    monkeypatch.setattr(lifecycle_lock, "config_dir", lambda: booley_config)

    with lifecycle_lock.host_lifecycle_lock("session up"):
        pass

    assert stat.S_IMODE(xdg_root.stat().st_mode) == 0o755
    assert stat.S_IMODE(booley_config.stat().st_mode) == 0o700
    assert stat.S_IMODE((booley_config / "locks").stat().st_mode) == 0o700


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode policy")
def test_host_lifecycle_lock_creates_missing_xdg_root_privately_under_open_umask(
    tmp_path, monkeypatch
) -> None:
    xdg_root = tmp_path / "config"
    booley_config = xdg_root / "booley"
    monkeypatch.setattr(lifecycle_lock, "config_dir", lambda: booley_config)

    original_umask = os.umask(0o002)
    try:
        with lifecycle_lock.host_lifecycle_lock("session up"):
            pass
    finally:
        os.umask(original_umask)

    assert stat.S_IMODE(xdg_root.stat().st_mode) == 0o700
    assert stat.S_IMODE(booley_config.stat().st_mode) == 0o700
    assert stat.S_IMODE((booley_config / "locks").stat().st_mode) == 0o700


def test_host_lifecycle_lock_waits_for_current_owner(tmp_path, monkeypatch, caplog) -> None:
    monkeypatch.setattr(lifecycle_lock, "config_dir", lambda: tmp_path)
    lock_path = tmp_path / "locks" / "docker-lifecycle.lock"
    waiting = observe_lifecycle_contention(monkeypatch)

    def acquire() -> None:
        with lifecycle_lock.host_lifecycle_lock("session up"):
            pass

    with (
        held_lifecycle_lock(lock_path, "pid=41 operation=booley init") as holder,
        ThreadPoolExecutor(max_workers=1) as executor,
    ):
        acquired = executor.submit(acquire)
        assert waiting.wait(2)
        assert not acquired.done()
        holder.release()
        acquired.result(timeout=5)

    expected_owner = (
        "another Booley command" if os.name == "nt" else "pid=41 operation=booley init"
    )
    assert expected_owner in caplog.text
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


def test_lock_owner_falls_back_when_windows_returns_partial_locked_content() -> None:
    assert (
        lifecycle_lock._lock_owner(StringIO("\0pid=41 operation=booley init\n"))
        == "another Booley command"
    )

"""Tests for host-wide Docker lifecycle serialization."""

from __future__ import annotations

import os
import re
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from booley.runtime import lifecycle_lock
from booley.runtime.file_lock import LockContentionError
from tests.lifecycle_lock_support import held_lifecycle_lock, observe_lifecycle_contention
from tests.time_budget import hang_guard_s


@pytest.fixture
def lock_path(tmp_path: Path) -> Path:
    """The lock file under the config directory the autouse isolation fixture selects."""
    return tmp_path / "host-config" / "locks" / "docker-lifecycle.lock"


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode policy")
@pytest.mark.parametrize("root_exists", [True, False], ids=["existing-root", "missing-root"])
def test_host_lifecycle_lock_keeps_its_directories_private(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, root_exists: bool
) -> None:
    xdg_root = tmp_path / "config"
    if root_exists:
        xdg_root.mkdir(mode=0o755)
        xdg_root.chmod(0o755)
    booley_config = xdg_root / "booley"
    # The autouse config directory's parent always exists; this test chooses whether it does.
    monkeypatch.setattr(lifecycle_lock, "config_dir", lambda: booley_config)

    original_umask = os.umask(0o002)
    try:
        with lifecycle_lock.host_lifecycle_lock("session up"):
            pass
    finally:
        os.umask(original_umask)

    assert stat.S_IMODE(xdg_root.stat().st_mode) == (0o755 if root_exists else 0o700)
    assert stat.S_IMODE(booley_config.stat().st_mode) == 0o700
    assert stat.S_IMODE((booley_config / "locks").stat().st_mode) == 0o700


def test_host_lifecycle_lock_waits_for_current_owner(lock_path, monkeypatch, caplog) -> None:
    waiting = observe_lifecycle_contention(monkeypatch)

    def acquire() -> None:
        with lifecycle_lock.host_lifecycle_lock("session up"):
            pass

    with (
        held_lifecycle_lock(lock_path, "pid=41 operation=booley init") as holder,
        ThreadPoolExecutor(max_workers=1) as executor,
    ):
        acquired = executor.submit(acquire)
        assert waiting.wait(hang_guard_s())
        assert not acquired.done()
        holder.release()
        acquired.result(timeout=hang_guard_s())

    expected_owner = (
        "another Booley command" if os.name == "nt" else "pid=41 operation=booley init"
    )
    assert expected_owner in caplog.text
    assert f"waiting up to {lifecycle_lock.DEFAULT_WAIT_TIMEOUT_SECONDS:g}s" in caplog.text
    owner = lock_path.read_text(encoding="utf-8")
    assert owner == f"pid={os.getpid()} operation=session up\n"


def test_host_lifecycle_lock_does_not_reclassify_body_error() -> None:
    body_error = LockContentionError("inner resource is busy")

    with (
        pytest.raises(LockContentionError, match="inner resource is busy") as raised,
        lifecycle_lock.host_lifecycle_lock("session refresh"),
    ):
        raise body_error

    assert raised.value is body_error


@pytest.mark.parametrize(
    ("owner", "shown"),
    [
        pytest.param("pid=41 operation=booley init", "pid=41 operation=booley init", id="text"),
        pytest.param("\0pid=41 operation=booley init", "another Booley command", id="nul"),
    ],
)
def test_host_lifecycle_lock_timeout_names_readable_owner(
    lock_path: Path, owner: str, shown: str
) -> None:
    expected = "another Booley command" if os.name == "nt" else shown

    with (
        held_lifecycle_lock(lock_path, owner),
        pytest.raises(
            lifecycle_lock.LifecycleLockError,
            match=rf"\({re.escape(expected)}\).*timed out after waiting 0s",
        ),
        lifecycle_lock.host_lifecycle_lock("session command", wait_timeout_s=0),
    ):
        pass


def test_lock_owner_falls_back_when_contended_file_cannot_be_read() -> None:
    class UnreadableOwner:
        def seek(self, _offset: int) -> None:
            raise OSError("locked byte cannot be read")

    # Only a Windows byte-range lock makes the contended read raise; no POSIX run of
    # `host_lifecycle_lock` reaches this fallback.
    assert lifecycle_lock._lock_owner(UnreadableOwner()) == "another Booley command"

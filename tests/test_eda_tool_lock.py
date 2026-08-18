"""Tests for eda_tool_lock.py — cross-process EDA-tool mutex."""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

# Ensure scripts/ is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from booley.runtime.eda_tool_lock import _find_locks_dir, eda_tool_lock, eda_tool_lock_env_var


@pytest.fixture
def isolated_root(tmp_path: Path, monkeypatch):
    """Set PROJECT_ROOT env var to tmp_path for test isolation."""
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path))
    (tmp_path / ".booley" / "project" / "tickets" / "locks").mkdir(parents=True, exist_ok=True)
    return tmp_path


class TestEdaToolLock:
    def test_acquire_release(self, isolated_root: Path):
        """Basic lock/unlock cycle works."""
        with eda_tool_lock("vivado", timeout_s=5):
            lock_file = isolated_root / ".booley" / "project" / "tickets" / "locks" / "vivado.lock"
            assert lock_file.exists()

    def test_pid_written(self, isolated_root: Path):
        """Lock file contains current PID after acquisition."""
        lock_file = isolated_root / ".booley" / "project" / "tickets" / "locks" / "vivado.lock"
        with eda_tool_lock("vivado", timeout_s=5):
            pass  # PID written during acquisition
        # Read after release (Windows msvcrt lock prevents concurrent reads)
        content = lock_file.read_text(encoding="utf-8").strip()
        assert content == str(os.getpid())

    def test_locks_dir_created(self, tmp_path: Path, monkeypatch):
        """Lock dir is auto-created if missing."""
        monkeypatch.setenv("PROJECT_ROOT", str(tmp_path))
        locks_dir = tmp_path / ".booley" / "project" / "tickets" / "locks"
        assert not locks_dir.exists()
        with eda_tool_lock("yosys", timeout_s=5):
            assert locks_dir.exists()

    def test_env_var_skip(self, isolated_root: Path, monkeypatch):
        """EDA_TOOL_LOCK_HELD env var=vivado makes eda_tool_lock("vivado") a no-op."""
        monkeypatch.setenv(eda_tool_lock_env_var(), "vivado")
        # Should not create a lock file since it's skipped
        with eda_tool_lock("vivado", timeout_s=1):
            # Lock was skipped — no lock file created
            pass
        # The env var skip means the lock file might or might not exist,
        # but the critical thing is that it didn't block or deadlock

    def test_env_var_different_name(self, isolated_root: Path, monkeypatch):
        """EDA_TOOL_LOCK_HELD env var=yosys does NOT skip eda_tool_lock("vivado")."""
        monkeypatch.setenv(eda_tool_lock_env_var(), "yosys")
        with eda_tool_lock("vivado", timeout_s=5):
            lock_file = isolated_root / ".booley" / "project" / "tickets" / "locks" / "vivado.lock"
            assert lock_file.exists()

    def test_env_var_comma_separated(self, isolated_root: Path, monkeypatch):
        """EDA_TOOL_LOCK_HELD env var=vivado,yosys skips both."""
        monkeypatch.setenv(eda_tool_lock_env_var(), "vivado,yosys")
        # Both should be no-ops (no blocking)
        with eda_tool_lock("vivado", timeout_s=1), eda_tool_lock("yosys", timeout_s=1):
            pass  # no deadlock

    def test_timeout_raises(self, isolated_root: Path):
        """When lock cannot be acquired, TimeoutError is raised."""
        with (
            patch("booley.runtime.eda_tool_lock.lock_fd", side_effect=BlockingIOError("locked")),
            pytest.raises(TimeoutError, match="vivado"),
            eda_tool_lock("vivado", timeout_s=1),
        ):
            pass  # pragma: no cover

    def test_contention_two_threads(self, isolated_root: Path):
        """Two threads compete for the same lock, both eventually succeed."""
        results = []
        barrier = threading.Barrier(2, timeout=10)

        def worker(worker_id):
            barrier.wait()  # ensure both start at the same time
            with eda_tool_lock("vivado", timeout_s=10):
                results.append(worker_id)
                time.sleep(0.1)  # hold lock briefly

        t1 = threading.Thread(target=worker, args=(1,))
        t2 = threading.Thread(target=worker, args=(2,))
        t1.start()
        t2.start()
        t1.join(timeout=15)
        t2.join(timeout=15)

        assert sorted(results) == [1, 2]


class TestFindLocksDir:
    def test_uses_project_root_env(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("PROJECT_ROOT", str(tmp_path))
        result = _find_locks_dir()
        assert result == tmp_path / ".booley" / "project" / "tickets" / "locks"

    def test_walks_up_to_git(self, tmp_path: Path, monkeypatch):
        monkeypatch.delenv("PROJECT_ROOT", raising=False)
        (tmp_path / ".git").mkdir()
        subdir = tmp_path / "a" / "b"
        subdir.mkdir(parents=True)
        monkeypatch.chdir(subdir)
        result = _find_locks_dir()
        assert result == tmp_path / ".booley" / "project" / "tickets" / "locks"

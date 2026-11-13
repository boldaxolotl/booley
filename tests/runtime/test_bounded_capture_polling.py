"""Interrupt-latency and spawn-failure guards for ``run_captured_bounded``."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from booley.runtime import platform_paths


def _sleep_argv(seconds: float) -> list[str]:
    return [sys.executable, "-c", f"import time; time.sleep({seconds})"]


def test_wait_is_sliced_so_an_interrupt_is_never_blocked_for_the_full_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CPython before 3.14 cannot deliver Ctrl-C to one long Windows join."""
    waits: list[float] = []
    real_communicate = subprocess.Popen.communicate

    def record_wait(self, *args, **kwargs):
        waits.append(kwargs["timeout"])
        return real_communicate(self, *args, **kwargs)

    monkeypatch.setattr(subprocess.Popen, "communicate", record_wait)

    result = platform_paths.run_captured_bounded(_sleep_argv(1.3), timeout=60)

    assert result.returncode == 0
    assert len(waits) > 1
    assert max(waits) <= 1.0, f"one wait lasted {max(waits)}s"


def test_interrupt_in_a_later_slice_stops_the_child(monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[subprocess.Popen[bytes]] = []
    real_communicate = subprocess.Popen.communicate

    def interrupt_second_slice(self, *args, **kwargs):
        started.append(self)
        if len(started) == 2:
            raise KeyboardInterrupt("injected interrupt")
        return real_communicate(self, *args, **kwargs)

    monkeypatch.setattr(subprocess.Popen, "communicate", interrupt_second_slice)

    with pytest.raises(KeyboardInterrupt):
        platform_paths.run_captured_bounded(_sleep_argv(60), timeout=120)

    assert started[0] is started[1]
    assert started[0].wait(timeout=10) is not None, "the child outlived the interrupt"


def test_missing_executable_with_input_raises_and_leaks_no_descriptor(tmp_path: Path) -> None:
    fd_dir = Path("/proc/self/fd")
    before = len(list(fd_dir.iterdir())) if fd_dir.is_dir() else None

    with pytest.raises(FileNotFoundError):
        platform_paths.run_captured_bounded(
            [str(tmp_path / "absent-tool")], input=b"x", text=False, timeout=5
        )

    if before is not None:
        assert len(list(fd_dir.iterdir())) == before


class _FakePipe:
    """A pipe stand-in that records whether it was closed."""

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _FakeReader:
    """A Windows ``communicate`` reader-thread stand-in."""

    def __init__(self, alive: bool) -> None:
        self._alive = alive

    def is_alive(self) -> bool:
        return self._alive


def test_interrupt_before_communicate_starts_closes_both_pipes() -> None:
    """No reader thread owns the pipes yet, so the helper closes them (no leak)."""
    proc = subprocess.Popen(_sleep_argv(0), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        platform_paths._close_unowned_pipes(proc)

        assert proc.stdout is not None and proc.stdout.closed
        assert proc.stderr is not None and proc.stderr.closed
    finally:
        proc.wait(timeout=10)


def test_pipe_with_a_live_reader_thread_is_left_to_that_thread() -> None:
    """Closing a buffered pipe a live reader is blocked on would block."""
    proc = subprocess.Popen(_sleep_argv(0), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    owned, finished = _FakePipe(), _FakePipe()
    real_stdout, real_stderr = proc.stdout, proc.stderr
    proc.stdout, proc.stderr = owned, finished  # type: ignore[assignment]
    proc.stdout_thread = _FakeReader(alive=True)  # type: ignore[attr-defined]
    proc.stderr_thread = _FakeReader(alive=False)  # type: ignore[attr-defined]
    try:
        platform_paths._close_unowned_pipes(proc)

        assert not owned.closed
        assert finished.closed
    finally:
        assert real_stdout is not None and real_stderr is not None
        real_stdout.close()
        real_stderr.close()
        proc.wait(timeout=10)

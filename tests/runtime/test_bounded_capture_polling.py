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

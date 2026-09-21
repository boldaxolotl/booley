"""Behavior tests for host-side Docker build progress."""

from __future__ import annotations

import os
import sys
import threading
import time
from collections import namedtuple
from pathlib import Path
from queue import Queue

import pytest

from booley.runtime import docker_build
from booley.runtime.docker_build import run_docker_build


class _RecordingOutput:
    def __init__(self, *, tty: bool = False) -> None:
        self._tty = tty
        self._condition = threading.Condition()
        self._text = ""

    def isatty(self) -> bool:
        return self._tty

    def write(self, text: str) -> int:
        with self._condition:
            self._text += text
            self._condition.notify_all()
        return len(text)

    def flush(self) -> None:
        pass

    def wait_for(self, text: str, timeout: float = 5.0) -> bool:
        with self._condition:
            return self._condition.wait_for(lambda: text in self._text, timeout=timeout)

    @property
    def text(self) -> str:
        with self._condition:
            return self._text


class _BrokenOutput:
    def isatty(self) -> bool:
        return False

    def write(self, _text: str) -> int:
        raise OSError("redirect closed")

    def flush(self) -> None:
        raise AssertionError("flush should not follow a failed write")


class _FailedCapture:
    def __iter__(self):
        return self

    def __next__(self):
        raise OSError("capture pipe failed")

    def close(self) -> None:
        pass


class _ExitedProcess:
    def __init__(self, stdout=None) -> None:
        self.stdout = stdout
        self.returncode = 0

    def poll(self) -> int:
        return self.returncode

    def wait(self, timeout=None) -> int:
        return self.returncode


def _install_fake_docker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    cached: bool,
    build_cache: str = "20GB",
) -> Path:
    docker = tmp_path / "docker"
    marker = tmp_path / "build-started"
    image_result = "printf 'sha256:cached\\n'" if cached else "exit 1"
    docker.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = info ]; then\n"
        f"  printf '%s\\n' {tmp_path}\n"
        "elif [ \"$1\" = image ]; then\n"
        f"  {image_result}\n"
        "elif [ \"$1\" = system ]; then\n"
        f"  printf 'Build Cache\\t{build_cache}\\t18GB\\n'\n"
        "elif [ \"$1\" = build ]; then\n"
        f"  touch {marker}\n"
        "fi\n",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    return marker


def _set_free_space(monkeypatch: pytest.MonkeyPatch, gib: int) -> None:
    disk_usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(
        "shutil.disk_usage", lambda _path: disk_usage(100, 88, gib * 2**30)
    )


def test_redirected_progress_is_visible_before_build_completes(tmp_path: Path) -> None:
    release = tmp_path / "release"
    child = (
        "import pathlib, sys, time; "
        "gate = pathlib.Path(sys.argv[1]); "
        "print('>>> Building Yosys from source', flush=True); "
        "\nwhile not gate.exists(): time.sleep(0.01)"
    )
    output = _RecordingOutput()
    results = []
    worker = threading.Thread(
        target=lambda: results.append(
            run_docker_build(
                [sys.executable, "-c", child, str(release)],
                image="booley-sandbox",
                verbose=False,
                timeout=10,
                output=output,
            )
        )
    )

    worker.start()
    try:
        assert output.wait_for(">>> Building Yosys from source")
        assert worker.is_alive()
    finally:
        release.touch()
        worker.join(timeout=5)

    assert not worker.is_alive()
    assert results[0].returncode == 0


def test_docker_build_refuses_low_capacity_before_starting(
    tmp_path: Path, monkeypatch
) -> None:
    marker = _install_fake_docker(tmp_path, monkeypatch, cached=False)
    _set_free_space(monkeypatch, 12)

    with pytest.raises(OSError) as raised:
        run_docker_build(
            ["docker", "build", "-t", "booley-sandbox", "."],
            image="booley-sandbox",
            verbose=False,
            timeout=10,
            output=_RecordingOutput(),
        )

    message = str(raised.value)
    assert "12.0 GiB available" in message
    assert "35.0 GiB required" in message
    assert "30.0 GiB cold-build headroom" in message
    assert "5.0 GiB safety reserve" in message
    assert "16.8 GiB reclaimable" in message
    assert "docker builder prune" in message
    assert "BOOLEY_SKIP_IMAGE_DISK_PREFLIGHT=1" in message
    assert not marker.exists()


def test_cached_docker_build_uses_smaller_headroom(tmp_path: Path, monkeypatch) -> None:
    marker = _install_fake_docker(tmp_path, monkeypatch, cached=True)
    _set_free_space(monkeypatch, 16)

    result = run_docker_build(
        ["docker", "build", "-t", "booley-sandbox", "."],
        image="booley-sandbox",
        verbose=False,
        timeout=10,
        output=_RecordingOutput(),
    )

    assert result.returncode == 0
    assert marker.exists()


def test_cached_docker_build_preserves_five_gib_safety_reserve(
    tmp_path: Path, monkeypatch
) -> None:
    marker = _install_fake_docker(tmp_path, monkeypatch, cached=True)
    _set_free_space(monkeypatch, 14)

    with pytest.raises(OSError, match=r"15\.0 GiB required") as raised:
        run_docker_build(
            ["docker", "build", "-t", "booley-sandbox", "."],
            image="booley-sandbox",
            verbose=False,
            timeout=10,
            output=_RecordingOutput(),
        )

    assert "10.0 GiB cached-target headroom" in str(raised.value)
    assert not marker.exists()


def test_target_without_build_cache_uses_cold_build_headroom(
    tmp_path: Path, monkeypatch
) -> None:
    marker = _install_fake_docker(
        tmp_path, monkeypatch, cached=True, build_cache="0B"
    )
    _set_free_space(monkeypatch, 16)

    with pytest.raises(OSError, match=r"35\.0 GiB required") as raised:
        run_docker_build(
            ["docker", "build", "-t", "booley-sandbox", "."],
            image="booley-sandbox",
            verbose=False,
            timeout=10,
            output=_RecordingOutput(),
        )

    assert "30.0 GiB cold-build headroom" in str(raised.value)
    assert not marker.exists()


def test_disk_preflight_override_allows_expert_build(
    tmp_path: Path, monkeypatch
) -> None:
    marker = _install_fake_docker(tmp_path, monkeypatch, cached=False)
    _set_free_space(monkeypatch, 1)
    monkeypatch.setenv("BOOLEY_SKIP_IMAGE_DISK_PREFLIGHT", "1")

    result = run_docker_build(
        ["docker", "build", "-t", "booley-sandbox", "."],
        image="booley-sandbox",
        verbose=False,
        timeout=10,
        output=_RecordingOutput(),
    )

    assert result.returncode == 0
    assert marker.exists()


def test_disk_preflight_override_accepts_only_one(tmp_path: Path, monkeypatch) -> None:
    marker = _install_fake_docker(tmp_path, monkeypatch, cached=False)
    _set_free_space(monkeypatch, 1)
    monkeypatch.setenv("BOOLEY_SKIP_IMAGE_DISK_PREFLIGHT", "true")

    with pytest.raises(OSError, match="Insufficient disk capacity"):
        run_docker_build(
            ["docker", "build", "-t", "booley-sandbox", "."],
            image="booley-sandbox",
            verbose=False,
            timeout=10,
            output=_RecordingOutput(),
        )

    assert not marker.exists()


def test_redirected_silent_build_emits_bounded_heartbeat(tmp_path: Path, monkeypatch) -> None:
    release = tmp_path / "release"
    child = (
        "import pathlib, sys, time; "
        "gate = pathlib.Path(sys.argv[1]); "
        "\nwhile not gate.exists(): time.sleep(0.01)"
    )
    output = _RecordingOutput()
    results = []
    monkeypatch.setattr(docker_build, "HEARTBEAT_INTERVAL_S", 0.05, raising=False)
    worker = threading.Thread(
        target=lambda: results.append(
            run_docker_build(
                [sys.executable, "-c", child, str(release)],
                image="booley-sandbox",
                verbose=False,
                timeout=10,
                output=output,
            )
        )
    )

    worker.start()
    try:
        assert output.wait_for("[booley-sandbox build] elapsed:")
        assert worker.is_alive()
    finally:
        release.touch()
        worker.join(timeout=5)

    assert not worker.is_alive()
    assert results[0].returncode == 0


def test_tty_silent_build_emits_bounded_heartbeat(tmp_path: Path, monkeypatch) -> None:
    release = tmp_path / "release"
    child = (
        "import pathlib, sys, time; "
        "gate = pathlib.Path(sys.argv[1]); "
        "\nwhile not gate.exists(): time.sleep(0.01)"
    )
    output = _RecordingOutput(tty=True)
    results = []
    monkeypatch.setattr(docker_build, "HEARTBEAT_INTERVAL_S", 0.05)
    worker = threading.Thread(
        target=lambda: results.append(
            run_docker_build(
                [sys.executable, "-c", child, str(release)],
                image="booley-sandbox",
                verbose=False,
                timeout=10,
                output=output,
            )
        )
    )

    worker.start()
    try:
        assert output.wait_for("[booley-sandbox build] elapsed:")
        assert worker.is_alive()
    finally:
        release.touch()
        worker.join(timeout=5)

    assert not worker.is_alive()
    assert results[0].returncode == 0


def test_pump_deadline_bounds_an_exited_process_without_eof() -> None:
    records: Queue[object] = Queue()
    output = _RecordingOutput()
    now = time.monotonic()
    state = docker_build._ProgressState(
        "booley-sandbox", False, output, now - 1, now - 0.5, now - 1
    )
    results = []
    worker = threading.Thread(
        target=lambda: results.append(docker_build._pump(_ExitedProcess(), records, state))
    )

    worker.start()
    worker.join(timeout=0.1)
    try:
        assert not worker.is_alive()
    finally:
        records.put(docker_build._EOF)
        worker.join(timeout=1)

    assert results == [False]


def test_output_capture_failure_is_reported_with_build_context(monkeypatch) -> None:
    process = _ExitedProcess(_FailedCapture())
    monkeypatch.setattr(docker_build.subprocess, "Popen", lambda *_args, **_kwargs: process)

    with pytest.raises(OSError, match="booley-sandbox Docker build output capture failed"):
        run_docker_build(
            [sys.executable, "-c", "pass"],
            image="booley-sandbox",
            verbose=False,
            timeout=10,
            output=_RecordingOutput(),
        )


def test_failure_retains_early_error_despite_noisy_cleanup() -> None:
    child = (
        "print('ERROR: package checksum mismatch', flush=True); "
        "[print(f'cleanup line {i}') for i in range(150)]; "
        "raise SystemExit(1)"
    )

    result = run_docker_build(
        [sys.executable, "-c", child],
        image="booley-sandbox",
        verbose=False,
        timeout=10,
        output=_RecordingOutput(),
    )

    assert result.returncode == 1
    assert "ERROR: package checksum mismatch" in result.diagnostics
    assert len(result.diagnostics) <= 120


def test_capacity_exhaustion_after_preflight_has_cleanup_guidance() -> None:
    result = run_docker_build(
        [
            sys.executable,
            "-c",
            "print('ERROR: no space left on device'); raise SystemExit(1)",
        ],
        image="booley-sandbox",
        verbose=False,
        timeout=10,
        output=_RecordingOutput(),
    )

    assert result.returncode == 1
    assert result.diagnostics[-1] == (
        "Docker storage filled during the build; free unused cache with "
        "`docker builder prune`, then retry."
    )


def test_closed_progress_sink_does_not_fail_a_healthy_build() -> None:
    result = run_docker_build(
        [sys.executable, "-c", "print('>>> Building image', flush=True)"],
        image="booley-sandbox",
        verbose=False,
        timeout=10,
        output=_BrokenOutput(),
    )

    assert result.returncode == 0


def test_timeout_still_applies_after_child_closes_output_pipe() -> None:
    child = "import os, time; os.close(1); os.close(2); time.sleep(10)"

    result = run_docker_build(
        [sys.executable, "-c", child],
        image="booley-sandbox",
        verbose=False,
        timeout=0.1,
        output=_RecordingOutput(),
    )

    assert result.timed_out
    assert result.returncode is None


def test_verbose_tty_timeout_uses_the_same_result_contract() -> None:
    result = run_docker_build(
        [sys.executable, "-c", "import time; time.sleep(10)"],
        image="booley-sandbox",
        verbose=True,
        timeout=0.1,
        output=_RecordingOutput(tty=True),
    )

    assert result.timed_out
    assert result.returncode is None


def test_hidden_chatter_does_not_postpone_redirected_heartbeat(
    tmp_path: Path, monkeypatch
) -> None:
    release = tmp_path / "release"
    child = (
        "import pathlib, sys, time; "
        "gate = pathlib.Path(sys.argv[1]); "
        "\nwhile not gate.exists(): print('compiler chatter', flush=True); time.sleep(0.01)"
    )
    output = _RecordingOutput()
    monkeypatch.setattr(docker_build, "HEARTBEAT_INTERVAL_S", 0.05)
    worker = threading.Thread(
        target=lambda: run_docker_build(
            [sys.executable, "-c", child, str(release)],
            image="booley-sandbox",
            verbose=False,
            timeout=10,
            output=output,
        )
    )

    worker.start()
    try:
        assert output.wait_for("[booley-sandbox build] elapsed:")
        assert "compiler chatter" not in output.text
    finally:
        release.touch()
        worker.join(timeout=5)

    assert not worker.is_alive()


def test_tty_non_verbose_preserves_progress_order_without_consecutive_duplicates() -> None:
    child = (
        "print('>>> first', flush=True); print('>>> first', flush=True); "
        "print('hidden chatter', flush=True); print('>>> second', flush=True)"
    )
    output = _RecordingOutput(tty=True)

    result = run_docker_build(
        [sys.executable, "-c", child],
        image="booley-sandbox",
        verbose=False,
        timeout=10,
        output=output,
    )

    assert result.returncode == 0
    assert output.text.splitlines() == [">>> first", ">>> second"]


def test_failure_diagnostics_do_not_repeat_visible_progress() -> None:
    child = "print('>>> compiling', flush=True); print('ERROR: compile failed'); exit(1)"
    output = _RecordingOutput(tty=True)

    result = run_docker_build(
        [sys.executable, "-c", child],
        image="booley-sandbox",
        verbose=False,
        timeout=10,
        output=output,
    )

    assert output.text.splitlines() == [">>> compiling"]
    assert result.diagnostics == ("ERROR: compile failed",)


def test_redirected_verbose_failure_streams_complete_output_once() -> None:
    child = (
        "import sys; print('build detail', flush=True); "
        "print('ERROR: compile failed', file=sys.stderr, flush=True); exit(1)"
    )
    output = _RecordingOutput()

    result = run_docker_build(
        [sys.executable, "-c", child],
        image="booley-sandbox",
        verbose=True,
        timeout=10,
        output=output,
    )

    assert result.returncode == 1
    assert output.text.splitlines() == ["build detail", "ERROR: compile failed"]
    assert result.diagnostics == ()

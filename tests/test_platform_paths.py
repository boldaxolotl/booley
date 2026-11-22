"""Tests for platform_paths: cross-platform path helpers."""

from __future__ import annotations

import math
import ntpath
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tests.pipe_holding_tree import (
    describe_heartbeat_tree,
    heartbeat_tree,
    parent_argv,
    stall_wait_on,
    wait_until_heartbeat_stops,
)
from tests.time_budget import hang_guard_s, scaled

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from booley.runtime import platform_paths
from booley.runtime.platform_paths import (
    cargo_bin,
    docker_mount_path,
    host_path_from_docker_mount,
    kill_process_tree,
    native_binary,
    popen_new_group_kwargs,
    venv_python,
)

# ---------------------------------------------------------------------------
# docker_mount_path
# ---------------------------------------------------------------------------


class TestDockerMountPath:
    @patch.object(platform_paths, "IS_WINDOWS", False)
    def test_posix_passthrough(self):
        assert docker_mount_path(Path("/home/user/project")) == "/home/user/project"

    @patch.object(platform_paths, "IS_WINDOWS", True)
    def test_windows_drive_conversion(self):
        result = docker_mount_path(Path("C:/Users/dev/project"))
        assert result == "/c/Users/dev/project"

    @patch.object(platform_paths, "IS_WINDOWS", True)
    def test_windows_lowercase_drive(self):
        result = docker_mount_path(Path("D:/data"))
        assert result == "/d/data"


class TestHostPathFromDockerMount:
    @patch.object(platform_paths, "IS_WINDOWS", True)
    def test_windows_docker_desktop_drive_source_becomes_native(self):
        assert host_path_from_docker_mount("/c/Users/dev/project").as_posix() == (
            "C:/Users/dev/project"
        )

    @patch.object(platform_paths, "IS_WINDOWS", True)
    @patch.object(platform_paths, "Path", PureWindowsPath)
    def test_windows_drive_root_is_absolute(self):
        assert host_path_from_docker_mount("/c").as_posix() == "C:/"

    @pytest.mark.parametrize(
        "source",
        [
            "/host_mnt/c/Users/dev/project",
            "/run/desktop/mnt/host/c/Users/dev/project",
        ],
    )
    @patch.object(platform_paths, "IS_WINDOWS", True)
    @patch.object(platform_paths, "Path", PureWindowsPath)
    def test_windows_docker_daemon_drive_source_becomes_native(self, source: str):
        assert host_path_from_docker_mount(source).as_posix() == "C:/Users/dev/project"

    @patch.object(platform_paths, "IS_WINDOWS", True)
    def test_windows_unmappable_daemon_source_has_no_native_path(self):
        source = "/run/desktop/mnt/host/wsl/docker-desktop-bind-mounts/Ubuntu/hash"
        assert host_path_from_docker_mount(source) is None

    @patch.object(platform_paths, "IS_WINDOWS", False)
    def test_posix_source_passes_through(self):
        assert host_path_from_docker_mount("/home/dev/project") == Path("/home/dev/project")


# ---------------------------------------------------------------------------
# venv_python
# ---------------------------------------------------------------------------


class TestVenvPython:
    @patch.object(platform_paths, "IS_WINDOWS", True)
    def test_windows_venv_python(self):
        result = venv_python(Path("/some/venv"))
        assert result == Path("/some/venv/Scripts/python.exe")

    @patch.object(platform_paths, "IS_WINDOWS", False)
    def test_posix_venv_python(self):
        result = venv_python(Path("/some/venv"))
        assert result == Path("/some/venv/bin/python")


# ---------------------------------------------------------------------------
# native_binary
# ---------------------------------------------------------------------------


class TestNativeBinary:
    @patch.object(platform_paths, "IS_WINDOWS", True)
    def test_windows_adds_exe_suffix(self):
        result = native_binary(Path("/bin"), "bwave")
        assert result == Path("/bin/bwave.exe")

    @patch.object(platform_paths, "IS_WINDOWS", False)
    def test_posix_no_suffix(self):
        result = native_binary(Path("/bin"), "bwave")
        assert result == Path("/bin/bwave")


# ---------------------------------------------------------------------------
# cargo_bin
# ---------------------------------------------------------------------------


class TestCargoBin:
    @patch("shutil.which", return_value="/usr/bin/cargo")
    def test_found_on_path(self, mock_which):
        assert cargo_bin() == "/usr/bin/cargo"

    @patch("shutil.which", return_value=None)
    @patch.object(platform_paths, "IS_WINDOWS", False)
    def test_fallback_to_plain_name(self, mock_which):
        assert cargo_bin() == "cargo"

    @patch("shutil.which", return_value=None)
    @patch.object(platform_paths, "IS_WINDOWS", True)
    @patch.object(platform_paths, "_MSYS2_CARGO")
    def test_windows_msys2_fallback(self, mock_cargo, mock_which):
        mock_cargo.exists.return_value = True
        mock_cargo.__str__ = lambda self: "C:/msys64/mingw64/bin/cargo.exe"
        assert cargo_bin() == "C:/msys64/mingw64/bin/cargo.exe"


# ---------------------------------------------------------------------------
# popen_new_group_kwargs
# ---------------------------------------------------------------------------


class TestPopenNewGroupKwargs:
    @patch.object(platform_paths, "IS_WINDOWS", True)
    def test_windows_returns_creation_flags(self):
        result = popen_new_group_kwargs()
        # 0x200 is the Win32 CREATE_NEW_PROCESS_GROUP flag.
        assert result == {"creationflags": 0x00000200}

    @patch.object(platform_paths, "IS_WINDOWS", False)
    def test_posix_returns_new_session(self):
        result = popen_new_group_kwargs()
        assert result == {"start_new_session": True}


@pytest.mark.skipif(sys.platform == "win32", reason="os.killpg is a POSIX API")
@patch.object(platform_paths, "IS_WINDOWS", False)
def test_kill_process_tree_kills_descendants_after_wrapper_exits():
    proc = type(
        "Proc",
        (),
        {
            "pid": 123,
            "poll": lambda self: None,
            "wait": lambda self, timeout: 0,
        },
    )()
    signals: list[tuple[int, int]] = []

    def fake_killpg(pgid, sig):
        signals.append((pgid, sig))

    with patch("os.killpg", side_effect=fake_killpg):
        kill_process_tree(proc)

    assert signals == [
        (123, signal.SIGTERM),
        (123, 0),
        (123, signal.SIGKILL),
    ]


def test_run_captured_bounded_kills_the_tree_when_interrupted():
    """A KeyboardInterrupt mid-wait must not leave the child running."""
    started: list[subprocess.Popen] = []
    real_popen = subprocess.Popen

    def recording_popen(*args, **kwargs):
        proc = real_popen(*args, **kwargs)
        # Windows tree-kill also launches taskkill through Popen: record only
        # the helper's own child, not the killer.
        if not started:
            started.append(proc)
        return proc

    real_communicate = real_popen.communicate

    def interrupted(self, *args, **kwargs):
        # Windows tree-kill runs taskkill through subprocess.run, which also
        # calls communicate(): interrupt only the helper's own child, or the
        # killer is interrupted and never kills the tree.
        if started and self is started[0]:
            raise KeyboardInterrupt
        return real_communicate(self, *args, **kwargs)

    with (
        patch.object(platform_paths.subprocess, "Popen", recording_popen),
        patch.object(real_popen, "communicate", interrupted),
        pytest.raises(KeyboardInterrupt),
    ):
        platform_paths.run_captured_bounded(
            [sys.executable, "-c", "import time; time.sleep(60)"], timeout=hang_guard_s()
        )

    (proc,) = started
    try:
        # kill_process_tree already ran; the child is dead or about to be reaped.
        assert proc.wait(timeout=hang_guard_s()) is not None
    finally:
        proc.kill()
        for pipe in (proc.stdout, proc.stderr):
            if pipe is not None:
                pipe.close()


def test_run_captured_bounded_runs_in_cwd_with_env(tmp_path: Path):
    marker = "bounded-marker-value"
    script = "import os; print(os.getcwd()); print(os.environ['BOOLEY_BOUNDED_MARKER'])"

    result = platform_paths.run_captured_bounded(
        [sys.executable, "-c", script],
        timeout=hang_guard_s(),
        cwd=tmp_path,
        env={**os.environ, "BOOLEY_BOUNDED_MARKER": marker},
    )

    cwd_line, marker_line = result.stdout.splitlines()
    assert Path(cwd_line).samefile(tmp_path)
    assert marker_line == marker


# ---------------------------------------------------------------------------
# same_host_path
# ---------------------------------------------------------------------------


class TestSameHostPath:
    def test_spelling_decides_before_the_filesystem(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(os.path, "normcase", ntpath.normcase)
        assert platform_paths.same_host_path("C:/Users/Dev", Path("c:\\users\\dev"))

    def test_missing_different_paths_are_not_equal(self, tmp_path: Path):
        assert not platform_paths.same_host_path(tmp_path / "a", tmp_path / "b")

    def test_nul_byte_in_a_label_is_not_equal(self, tmp_path: Path):
        assert not platform_paths.same_host_path(str(tmp_path) + "\0", tmp_path)

    def test_same_directory_through_a_trailing_separator_is_equal(self, tmp_path: Path):
        assert platform_paths.same_host_path(str(tmp_path) + os.sep, tmp_path)


_EMIT_INVALID_UTF8 = [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'\\xff')"]


def test_run_captured_bounded_replaces_undecodable_bytes_by_default():
    assert (
        "�"
        in platform_paths.run_captured_bounded(_EMIT_INVALID_UTF8, timeout=hang_guard_s()).stdout
    )


def test_run_captured_bounded_decodes_strictly_on_request():
    """``errors`` selects the decoding mode; strict never turns a timeout into a decode error."""
    emit = _EMIT_INVALID_UTF8
    with pytest.raises(UnicodeDecodeError):
        platform_paths.run_captured_bounded(emit, timeout=hang_guard_s(), errors="strict")
    truncated = [
        sys.executable,
        "-c",
        "import sys, time; sys.stdout.buffer.write(b'\\xc3'); sys.stdout.flush(); time.sleep(30)",
    ]
    with pytest.raises(subprocess.TimeoutExpired):
        platform_paths.run_captured_bounded(truncated, timeout=scaled(2), errors="strict")


# ---------------------------------------------------------------------------
# run_captured_bounded: stdin input, stdin stream, output stream, bytes (#3861)
# ---------------------------------------------------------------------------

#: Echo stdin, then append a CRLF and a byte that is not UTF-8.
_ECHO_STDIN_WITH_TAIL = [
    sys.executable,
    "-c",
    "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read() + b'\\r\\n\\xff')",
]


def test_run_captured_bounded_feeds_input_and_streams(tmp_path: Path):
    # Bytes mode keeps the raw output; text mode decodes and translates newlines.
    raw = platform_paths.run_captured_bounded(
        _ECHO_STDIN_WITH_TAIL, timeout=hang_guard_s(), input=b"in", text=False
    )
    assert raw.stdout == b"in\r\n\xff"
    text = platform_paths.run_captured_bounded(
        _ECHO_STDIN_WITH_TAIL, timeout=hang_guard_s(), input=b"in"
    )
    assert text.stdout == "in\n\ufffd"

    # Input far larger than a pipe buffer is written in full while the child reads.
    count = [sys.executable, "-c", "import sys; print(len(sys.stdin.buffer.read()))"]
    big = platform_paths.run_captured_bounded(
        count, timeout=hang_guard_s(), input=b"x" * (1 << 20)
    )
    assert big.stdout.strip() == str(1 << 20)

    # A stdin stream is read by the child; a stdout stream receives its output.
    source = tmp_path / "source.bin"
    source.write_bytes(b"streamed")
    sink = tmp_path / "sink.bin"
    with source.open("rb") as stdin_stream, sink.open("wb") as stdout_stream:
        streamed = platform_paths.run_captured_bounded(
            [
                sys.executable,
                "-c",
                "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read());"
                " sys.stderr.write('warn')",
            ],
            timeout=hang_guard_s(),
            stdin=stdin_stream,
            stdout=stdout_stream,
        )
    assert sink.read_bytes() == b"streamed"
    assert streamed.stdout is None
    assert streamed.stderr == "warn"


#: A child that exits at once.
_EXIT_AT_ONCE = [sys.executable, "-c", "pass"]

#: Deadline given to ``run_captured_bounded`` for a hung child tree.
_HUNG_TREE_DEADLINE_S = scaled(2)


def _assert_hung_tree_is_stopped(tmp_path: Path, **mode) -> subprocess.TimeoutExpired:
    """Run a hung child tree that ignores stdin under *mode*; it must be stopped in time."""
    with heartbeat_tree(tmp_path) as paths:
        argv = parent_argv(tmp_path, sys.executable, paths, "live-until-stop")
        started = time.monotonic()
        expired: subprocess.TimeoutExpired | None = None
        try:
            completed = platform_paths.run_captured_bounded(
                argv, timeout=_HUNG_TREE_DEADLINE_S, **mode
            )
        except subprocess.TimeoutExpired as raised:
            expired = raised
        # The elapsed time is taken at the helper's return; the tree is observed
        # only when a check fails, up to 1 s later.
        elapsed = time.monotonic() - started
        summary = f"run_captured_bounded returned after {elapsed:.2f} s against a {_HUNG_TREE_DEADLINE_S:g} s deadline"
        if expired is None:
            pytest.fail(
                f"{summary} with exit status {completed.returncode} instead of TimeoutExpired;"
                f" shortly after, the tree {describe_heartbeat_tree(paths)}"
            )
        # timeout + the helper's 32 s Windows margin, below the scaled tree life
        assert elapsed < scaled(38), (
            f"{summary}; shortly after, the tree {describe_heartbeat_tree(paths)}"
        )
        assert wait_until_heartbeat_stops(paths["heartbeat"]), (
            f"the descendant was left running; {summary}"
        )
    return expired


def test_run_captured_bounded_stops_a_hung_tree_fed_input(tmp_path: Path):
    # 1 MiB overflows the pipe buffer, so the feeder blocks on a child that never reads.
    _assert_hung_tree_is_stopped(tmp_path, input=b"x" * (1 << 20))
    assert not [t for t in threading.enumerate() if t.name == "bounded-capture-input"]


def test_run_captured_bounded_stops_a_hung_tree_reading_a_stdin_stream(tmp_path: Path):
    stream = tmp_path / "stdin.bin"
    stream.write_bytes(b"x" * 4096)
    with stream.open("rb") as handle:
        _assert_hung_tree_is_stopped(tmp_path, stdin=handle)


def test_run_captured_bounded_stops_a_hung_tree_writing_an_output_stream(tmp_path: Path):
    with (tmp_path / "stdout.bin").open("wb") as handle:
        _assert_hung_tree_is_stopped(tmp_path, stdout=handle)


def test_run_captured_bounded_stops_a_hung_tree_capturing_bytes(tmp_path: Path):
    expired = _assert_hung_tree_is_stopped(tmp_path, text=False)
    assert expired.stderr is None or isinstance(expired.stderr, bytes)


def test_run_captured_bounded_stops_a_hung_tree_while_the_wait_is_stalled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A wait stuck until the tree closes its pipes still times out at the deadline (#4323)."""
    # Exercise the Windows deadline policy on every host, with native process
    # group operations still selected by the real host platform.
    monkeypatch.setattr(platform_paths, "sys", SimpleNamespace(platform="win32"))
    with heartbeat_tree(tmp_path) as paths:
        argv = parent_argv(tmp_path, sys.executable, paths, "live-until-stop")
        stall_wait_on(monkeypatch)
        started = time.monotonic()
        with pytest.raises(subprocess.TimeoutExpired):
            platform_paths.run_captured_bounded(argv, timeout=_HUNG_TREE_DEADLINE_S, text=False)
        assert time.monotonic() - started < scaled(38)
        assert wait_until_heartbeat_stops(paths["heartbeat"]), "the descendant was left running"
    assert not [t for t in threading.enumerate() if t.name == "bounded-capture-deadline"]


def test_deadline_kill_spares_a_child_that_already_exited():
    """A child that exited before the deadline is not reported as timed out (#4323)."""
    proc = subprocess.Popen(_EXIT_AT_ONCE)
    proc.wait(timeout=hang_guard_s())
    killer = platform_paths._DeadlineKiller(proc, 0)
    killer.start()
    killer._thread.join(hang_guard_s())  # the deadline has passed before the wait returns

    assert killer.stop() is False


def test_run_captured_bounded_accepts_an_infinite_timeout(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(platform_paths, "sys", SimpleNamespace(platform="win32"))
    result = platform_paths.run_captured_bounded(_EXIT_AT_ONCE, timeout=math.inf)
    assert result.returncode == 0

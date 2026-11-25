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
from collections.abc import Callable
from pathlib import Path, PureWindowsPath
from unittest.mock import patch

import pytest

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
from tests.host_platform_fake import use_windows_deadline_policy, use_windows_paths
from tests.path_tool_fake import PathToolFake
from tests.pipe_holding_tree import (
    describe_heartbeat_tree,
    heartbeat_tree,
    parent_argv,
    stall_wait_on,
    start_deadline_after_tree_start,
    wait_until_heartbeat_stops,
)
from tests.time_budget import SCALE_VARIABLE, hang_guard_s, scaled

# ---------------------------------------------------------------------------
# platform conventions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("windows", "call", "expected"),
    [
        (False, lambda: docker_mount_path(Path("/home/user/project")), "/home/user/project"),
        (True, lambda: docker_mount_path(Path("C:/Users/dev/project")), "/c/Users/dev/project"),
        (False, lambda: venv_python(Path("/some/venv")).as_posix(), "/some/venv/bin/python"),
        (
            True,
            lambda: venv_python(Path("/some/venv")).as_posix(),
            "/some/venv/Scripts/python.exe",
        ),
        (False, lambda: native_binary(Path("/bin"), "bwave").as_posix(), "/bin/bwave"),
        (True, lambda: native_binary(Path("/bin"), "bwave").as_posix(), "/bin/bwave.exe"),
        (False, popen_new_group_kwargs, {"start_new_session": True}),
        # 0x200 is the Win32 CREATE_NEW_PROCESS_GROUP flag.
        (True, popen_new_group_kwargs, {"creationflags": 0x200}),
        (
            False,
            lambda: host_path_from_docker_mount("/home/dev/project").as_posix(),
            "/home/dev/project",
        ),
    ],
    ids=[
        "docker-posix",
        "docker-windows",
        "venv-posix",
        "venv-windows",
        "binary-posix",
        "binary-windows",
        "group-posix",
        "group-windows",
        "host-path-posix",
    ],
)
def test_conventions_follow_the_host_platform(
    monkeypatch: pytest.MonkeyPatch, windows: bool, call: Callable[[], object], expected: object
):
    use_windows_paths(monkeypatch, windows=windows)
    assert call() == expected


@pytest.mark.parametrize(
    ("source", "native"),
    [
        ("/c/Users/dev/project", "C:/Users/dev/project"),
        ("/c", "C:/"),
        ("/host_mnt/c/Users/dev/project", "C:/Users/dev/project"),
        ("/run/desktop/mnt/host/c/Users/dev/project", "C:/Users/dev/project"),
        ("/run/desktop/mnt/host/wsl/docker-desktop-bind-mounts/Ubuntu/hash", None),
    ],
    ids=["docker-desktop", "drive-root", "host-mnt", "run-desktop", "wsl-bind-mount"],
)
def test_windows_docker_source_becomes_a_native_path_when_addressable(
    monkeypatch: pytest.MonkeyPatch, source: str, native: str | None
):
    use_windows_paths(monkeypatch, windows=True)
    # PureWindowsPath keeps the drive-letter spelling checkable on a POSIX host; a POSIX Path
    # cannot parse a drive letter, so no public input reaches this behavior there.
    monkeypatch.setattr(platform_paths, "Path", PureWindowsPath)
    result = host_path_from_docker_mount(source)
    assert (None if result is None else result.as_posix()) == native


def test_cargo_bin_prefers_cargo_on_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    fake = PathToolFake.install(tmp_path, "cargo", monkeypatch)
    assert platform_paths.same_host_path(cargo_bin(), fake.executable)


@pytest.mark.parametrize("windows", [True, False])
def test_cargo_bin_falls_back_to_msys2_only_on_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, windows: bool
):
    empty = tmp_path / "empty"
    empty.mkdir()
    msys2 = tmp_path / "cargo.exe"
    msys2.write_bytes(b"")
    monkeypatch.setenv("PATH", str(empty))
    use_windows_paths(monkeypatch, windows=windows)
    # private: the MSYS2 cargo is a fixed C: drive path that no public input moves.
    monkeypatch.setattr(platform_paths, "_MSYS2_CARGO", msys2)
    assert cargo_bin() == (str(msys2) if windows else "cargo")


@pytest.mark.skipif(sys.platform == "win32", reason="os.killpg is a POSIX API")
def test_kill_process_tree_kills_descendants_after_wrapper_exits():
    proc = type(
        "Proc", (), {"pid": 123, "poll": lambda self: None, "wait": lambda self, timeout: 0}
    )()
    signals: list[tuple[int, int]] = []

    def fake_killpg(pgid, sig):
        signals.append((pgid, sig))

    with patch("os.killpg", side_effect=fake_killpg):
        kill_process_tree(proc)

    assert signals == [(123, signal.SIGTERM), (123, 0), (123, signal.SIGKILL)]


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


def test_same_host_path_spelling_decides_before_the_filesystem(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(os.path, "normcase", ntpath.normcase)
    assert platform_paths.same_host_path("C:/Users/Dev", Path("c:\\users\\dev"))


@pytest.mark.parametrize(
    ("spellings", "same"),
    [
        (lambda d: (d / "a", d / "b"), False),  # missing paths: samefile raises OSError
        (lambda d: (str(d) + "\0", d), False),  # a NUL byte in a Docker label: ValueError
        (lambda d: (str(d) + os.sep, d), True),  # a trailing separator, same directory
    ],
    ids=["missing-paths", "nul-byte", "trailing-separator"],
)
def test_same_host_path_falls_back_to_the_filesystem(
    tmp_path: Path, spellings: Callable[[Path], tuple[str | Path, Path]], same: bool
):
    left, right = spellings(tmp_path)
    assert platform_paths.same_host_path(left, right) is same


def test_run_captured_bounded_decodes_strictly_on_request():
    """``errors`` selects the decoding mode; strict never turns a timeout into a decode error."""
    emit = [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'\\xff')"]
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
    with heartbeat_tree(tmp_path) as paths, pytest.MonkeyPatch.context() as monkeypatch:
        argv = parent_argv(tmp_path, sys.executable, paths, "live-until-stop")
        started = start_deadline_after_tree_start(monkeypatch, paths)
        expired: subprocess.TimeoutExpired | None = None
        try:
            completed = platform_paths.run_captured_bounded(
                argv, timeout=_HUNG_TREE_DEADLINE_S, **mode
            )
        except subprocess.TimeoutExpired as raised:
            expired = raised
        # The elapsed time is taken at the helper's return; the tree is observed
        # only when a check fails, up to 1 s later.
        elapsed = time.monotonic() - started[0]
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
    use_windows_deadline_policy(monkeypatch)
    with heartbeat_tree(tmp_path) as paths:
        argv = parent_argv(tmp_path, sys.executable, paths, "live-until-stop")
        stall_wait_on(monkeypatch)
        started = start_deadline_after_tree_start(monkeypatch, paths)
        with pytest.raises(subprocess.TimeoutExpired):
            platform_paths.run_captured_bounded(argv, timeout=_HUNG_TREE_DEADLINE_S, text=False)
        assert time.monotonic() - started[0] < scaled(38)
        assert wait_until_heartbeat_stops(paths["heartbeat"]), "the descendant was left running"
    assert not [t for t in threading.enumerate() if t.name == "bounded-capture-deadline"]


def test_deadline_kill_spares_a_child_that_already_exited():
    """A child that exited before the deadline is not reported as timed out (#4323)."""
    proc = subprocess.Popen(_EXIT_AT_ONCE)
    proc.wait(timeout=hang_guard_s())
    # private: whether the killer fires for an exited child is a race inside
    # run_captured_bounded; only the killer itself can order the deadline before stop().
    killer = platform_paths._DeadlineKiller(proc, 0)
    killer.start()
    killer._thread.join(hang_guard_s())  # the deadline has passed before the wait returns

    assert killer.stop() is False


def test_run_captured_bounded_accepts_an_infinite_timeout(monkeypatch: pytest.MonkeyPatch):
    use_windows_deadline_policy(monkeypatch)
    result = platform_paths.run_captured_bounded(_EXIT_AT_ONCE, timeout=math.inf)
    assert result.returncode == 0


def test_a_hung_tree_that_never_starts_fails_at_the_hang_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    cleanup_margin = scaled(15)  # a Windows tree kill may take 10 s
    monkeypatch.setenv(SCALE_VARIABLE, "0.05")  # a 2 s hang guard
    silent = [sys.executable, "-c", "import time; time.sleep(60)"]  # never beats
    with heartbeat_tree(tmp_path) as paths:
        start_deadline_after_tree_start(monkeypatch, paths)
        started = time.monotonic()
        with pytest.raises(pytest.fail.Exception, match="the process tree never started"):
            platform_paths.run_captured_bounded(silent, timeout=hang_guard_s())
        # It waited the whole guard, then stopped the silent child instead of reaping it at 60 s.
        assert hang_guard_s() <= time.monotonic() - started < hang_guard_s() + cleanup_margin

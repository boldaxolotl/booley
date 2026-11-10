"""Tests for platform_paths: cross-platform path helpers."""

from __future__ import annotations

import ntpath
import os
import signal
import subprocess
import sys
from pathlib import Path, PureWindowsPath
from unittest.mock import patch

import pytest

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
    # subprocess.CREATE_NEW_PROCESS_GROUP only exists on Windows, so we inject
    # it (create=True) with its real Win32 value to exercise the Windows branch
    # on any host.
    @patch.object(platform_paths, "IS_WINDOWS", True)
    @patch.object(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200, create=True)
    def test_windows_returns_creation_flags(self):
        result = popen_new_group_kwargs()
        assert result == {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}

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
            [sys.executable, "-c", "import time; time.sleep(60)"], timeout=30
        )

    (proc,) = started
    try:
        # kill_process_tree already ran; the child is dead or about to be reaped.
        assert proc.wait(timeout=15) is not None
    finally:
        proc.kill()
        for pipe in (proc.stdout, proc.stderr):
            if pipe is not None:
                pipe.close()


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

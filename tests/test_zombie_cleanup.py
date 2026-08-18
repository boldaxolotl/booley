"""Tests for zombie process cleanup safety."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


class TestIsInsideDocker:
    def test_returns_bool(self):
        from booley.runtime.zombie_cleanup import _is_inside_docker

        assert isinstance(_is_inside_docker(), bool)

    def test_false_on_host(self):
        """On a normal dev machine, should return False."""
        from booley.runtime.zombie_cleanup import _is_inside_docker

        # This test runs on the host, not in Docker
        assert not _is_inside_docker()


class TestKillZombieFlowProcessesDockerGuard:
    def test_skips_in_docker(self):
        """Zombie cleanup must be a no-op inside Docker."""
        from booley.runtime.zombie_cleanup import kill_zombie_flow_processes

        with (
            patch("booley.runtime.zombie_cleanup._is_inside_docker", return_value=True),
            patch("booley.runtime.zombie_cleanup._kill_zombies_unix") as mock_unix,
            patch("booley.runtime.zombie_cleanup._kill_zombies_windows") as mock_win,
        ):
            kill_zombie_flow_processes("sim")
            mock_unix.assert_not_called()
            mock_win.assert_not_called()


@pytest.mark.skipif(sys.platform == "win32", reason="Unix-only")
class TestAncestorPids:
    def test_includes_own_pid(self):
        from booley.runtime.zombie_cleanup import _ancestor_pids

        ancestors = _ancestor_pids()
        assert os.getpid() in ancestors

    def test_includes_pid_1(self):
        from booley.runtime.zombie_cleanup import _ancestor_pids

        ancestors = _ancestor_pids()
        assert 1 in ancestors

    def test_includes_parent(self):
        from booley.runtime.zombie_cleanup import _ancestor_pids

        ancestors = _ancestor_pids()
        assert os.getppid() in ancestors


@pytest.mark.skipif(sys.platform == "win32", reason="Unix-only")
class TestZombieCleanupSafety:
    def test_does_not_kill_ancestors(self):
        """pgrep -f can match parent processes whose command line contains
        script names (e.g., Claude CLI's -p prompt). The zombie killer
        must skip all ancestor PIDs."""
        from booley.runtime.zombie_cleanup import _kill_zombies_unix

        my_pid = os.getpid()
        parent_pid = os.getppid()

        # Simulate pgrep returning our parent PID and a "zombie" PID
        fake_zombie_pid = 99999
        pgrep_output = f"{parent_pid}\n{my_pid}\n{fake_zombie_pid}\n"

        # _kill_zombies_unix lives in booley.runtime.zombie_cleanup and resolves these
        # names in that module's namespace; project_config only re-exports the
        # function, so patches must target zombie_cleanup.* to take effect.
        with (
            patch("booley.runtime.zombie_cleanup.subprocess.run") as mock_run,
            patch("booley.runtime.zombie_cleanup.os.kill") as mock_kill,
            patch(
                "booley.runtime.zombie_cleanup._ancestor_pids",
                return_value={1, parent_pid, my_pid},
            ),
            # Scoping is covered separately; here every match is "ours" so the
            # ancestor guard is the only thing under test.
            patch("booley.runtime.zombie_cleanup._pid_in_scope", return_value=True),
            patch("booley.runtime.zombie_cleanup._descendant_pids", return_value=[]),
        ):
            # pkill calls return ok, pgrep returns our fake PIDs
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=pgrep_output,
            )
            _kill_zombies_unix("sim")

            # Should only kill the fake zombie, never ancestors
            killed_pids = [call.args[0] for call in mock_kill.call_args_list]
            assert fake_zombie_pid in killed_pids
            assert parent_pid not in killed_pids
            assert my_pid not in killed_pids


@pytest.mark.skipif(sys.platform == "win32", reason="Unix-only /proc walk")
class TestProcStatParsing:
    """A ') ' inside comm must not derail the ppid parse."""

    def test_plain_comm(self):
        from booley.runtime.zombie_cleanup import _parse_ppid

        assert _parse_ppid("42 (bash) S 7 42 42 0 -1 4194304 100") == 7

    def test_comm_containing_a_close_paren_and_space(self):
        """The old ``split(') ')[1]`` read the state field as the ppid here.

        A mis-parse is silently dropped, and in ``_ancestor_pids`` that means a
        truncated ancestor chain — the set that keeps the reaper from killing
        its own caller.
        """
        from booley.runtime.zombie_cleanup import _parse_ppid

        assert _parse_ppid("42 (weird) name) S 7 42 42 0 -1 4194304 100") == 7

    def test_garbage_is_none_not_an_exception(self):
        from booley.runtime.zombie_cleanup import _parse_ppid

        assert _parse_ppid("not a stat line") is None
        assert _parse_ppid("") is None


@pytest.mark.skipif(sys.platform == "win32", reason="Unix-only /proc walk")
class TestKillScoping:
    """A pgrep -f marker hit is host-wide; only THIS project's runs are ours.

    The module markers make ``pgrep -f booley.sim.verilator_run`` match any
    checkout's live run-half. Combined with the descendant walk that is a
    cross-project kill: a bisect in project A would SIGKILL project B's
    in-flight ``venue=host`` simulate and its whole subtree.
    """

    def test_own_process_is_in_scope(self):
        from booley.runtime.zombie_cleanup import _pid_in_scope, _scope_roots

        # pytest runs from the repo root, so our own cwd is inside the scope.
        assert _pid_in_scope(os.getpid(), _scope_roots()) is True

    def test_process_outside_the_project_is_not_in_scope(self, tmp_path: Path):
        import subprocess

        from booley.runtime.zombie_cleanup import _pid_in_scope

        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            cwd="/",
        )
        try:
            assert _pid_in_scope(proc.pid, [tmp_path.resolve()]) is False
        finally:
            proc.kill()
            proc.wait(timeout=10)

    def test_unprovable_scope_means_not_ours(self):
        from booley.runtime.zombie_cleanup import _pid_in_scope

        # A pid that does not exist can never be shown to be ours.
        assert _pid_in_scope(999_999, [Path("/")]) is False
        # No roots at all: nothing is reapable, rather than everything.
        assert _pid_in_scope(os.getpid(), []) is False

    def test_out_of_scope_match_is_never_killed(self):
        """The cross-project scenario: matched, alive, and NOT ours."""
        from booley.runtime.zombie_cleanup import _kill_zombies_unix

        other_project_pid = 99999
        with (
            patch("booley.runtime.zombie_cleanup.subprocess.run") as mock_run,
            patch("booley.runtime.zombie_cleanup.os.kill") as mock_kill,
            patch("booley.runtime.zombie_cleanup._ancestor_pids", return_value={1}),
            patch("booley.runtime.zombie_cleanup._pid_in_scope", return_value=False),
            patch("booley.runtime.zombie_cleanup._descendant_pids") as mock_desc,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout=f"{other_project_pid}\n")
            _kill_zombies_unix("sim")

        mock_kill.assert_not_called()
        # Not even walked: the subtree of a stranger's run is a stranger's too.
        mock_desc.assert_not_called()


class TestSimMarkers:
    """F-13: the run-halves ship as `python3 -m booley.sim.<name>`."""

    def test_module_form_is_matched(self):
        from booley.runtime.zombie_cleanup import _SIM_SCRIPT_MARKERS

        # The cmdline of a live run-half is
        #   python3 -m booley.sim.verilator_run --bin-dir ...
        # which contains NO "verilator_run.py" — matching only the script
        # spelling missed every real orphan this module exists to reap.
        cmdline = "python3 -m booley.sim.verilator_run --bin-dir build --top tb"
        assert any(m in cmdline for m in _SIM_SCRIPT_MARKERS)

    def test_script_form_still_matched(self):
        from booley.runtime.zombie_cleanup import _SIM_SCRIPT_MARKERS

        assert any(m in "python3 /x/verilator_run.py --top tb" for m in _SIM_SCRIPT_MARKERS)


@pytest.mark.skipif(sys.platform == "win32", reason="Unix-only /proc walk")
class TestDescendantPids:
    def test_finds_a_grandchild_in_its_own_session(self):
        """A simulator started with start_new_session is still found.

        Killing the run-half's pid or process group leaves such a child alive;
        only following the parent links reaches it (fpu F-13).
        """
        import subprocess
        import textwrap
        import time

        from booley.runtime.zombie_cleanup import _descendant_pids

        src = textwrap.dedent(
            """
            import subprocess, sys, time
            child = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                start_new_session=True,
            )
            print(child.pid, flush=True)
            time.sleep(30)
            """
        )
        proc = subprocess.Popen([sys.executable, "-c", src], stdout=subprocess.PIPE, text=True)
        try:
            grandchild = int(proc.stdout.readline().strip())
            time.sleep(0.2)
            found = _descendant_pids(os.getpid())
            assert proc.pid in found
            assert grandchild in found
            # Deepest first, so the simulator is killed before its supervisor.
            assert found.index(grandchild) < found.index(proc.pid)
        finally:
            proc.kill()
            proc.wait(timeout=10)

    def test_no_descendants_is_empty(self):
        from booley.runtime.zombie_cleanup import _descendant_pids

        assert _descendant_pids(999_999) == []

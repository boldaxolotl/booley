"""Tests for BooleyFlow — subprocess execution, sentinel scanning, exit codes."""

from __future__ import annotations

import argparse
import contextlib
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, ClassVar
from unittest.mock import patch

import pytest

from booley.criteria.templates import cycle_count_criterion_key
from booley.flows import base as flow_base
from booley.flows.base import BooleyFlow, SubprocessResult
from booley.mcp.base import EXIT_ERROR, EXIT_FAILURE, EXIT_SUCCESS, McpToolResult


def _env_with_state(state_file: Path, slug: str = "test") -> dict[str, str]:
    env = os.environ.copy()
    env["BOOLEY_SLUG"] = slug
    env["BOOLEY_STATE_FILE"] = str(state_file)
    return env


class EchoFlow(BooleyFlow):
    """Concrete BooleyFlow that echoes a message."""

    name = "echo_tool"
    description = "Echo test"

    def _add_args(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--message", default="hello")

    def _build_command(self) -> list[str]:
        return ["echo", self.args.message]

    def _interpret_result(self, result: SubprocessResult) -> McpToolResult:
        if result.timed_out:
            return McpToolResult(exit_code=EXIT_ERROR, report_text="Timed out")
        if result.returncode == 0:
            return McpToolResult(
                exit_code=EXIT_SUCCESS,
                criterion_key="echo_pass",
                criterion_met=True,
                detail={"output": result.stdout.strip()},
            )
        return McpToolResult(exit_code=EXIT_FAILURE, criterion_met=False)


class TestBooleyFlowExecution:
    def test_cycle_count_only_ticket_can_invoke_its_bound_target(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from booley.criteria.state import DevelopmentState
        from booley.runtime import runtime_context

        class CycleCountFlow(EchoFlow):
            name = "sim"
            satisfies: ClassVar[list[str]] = ["cycle_count"]

        key = cycle_count_criterion_key("sim_core", "coremark")
        state_file = tmp_path / "state.json"
        state = DevelopmentState.load(state_file)
        state.init_criteria(
            {key: True},
            criterion_params={
                key: {
                    "target": "sim_core",
                    "test": "coremark",
                    "cycle_count_max": 100,
                }
            },
            strict=True,
        )
        state.save()
        monkeypatch.setattr(runtime_context, "inside_session_runtime", lambda: True)

        flow = CycleCountFlow()
        with (
            patch.dict(os.environ, _env_with_state(state_file)),
            patch.object(
                flow,
                "_run",
                return_value=McpToolResult(exit_code=EXIT_SUCCESS, report_text="ok"),
            ) as run,
        ):
            exit_code = flow.main(["--target", "sim_core", "--work-dir", str(tmp_path)])

        assert exit_code == EXIT_SUCCESS
        run.assert_called_once_with()

    def test_host_entry_is_rejected_before_flow_execution(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        from booley.runtime import runtime_context

        monkeypatch.setattr(runtime_context, "inside_session_runtime", lambda: False)
        flow = EchoFlow()
        with patch.object(flow, "_run") as run:
            exit_code = flow.main(["--target", "test", "--work-dir", str(tmp_path)])

        assert exit_code == EXIT_ERROR
        run.assert_not_called()
        assert "Sandbox" in capsys.readouterr().err

    def test_successful_execution(self, tmp_path: Path):
        state_file = tmp_path / "state.json"
        from booley.criteria.state import DevelopmentState

        DevelopmentState.load(state_file).save()

        env = _env_with_state(state_file)
        flow = EchoFlow()
        with patch.dict(os.environ, env):
            flow.parse_args(["--target", "test", "--message", "hello world"])
        flow.read_state()
        result = flow._run()
        assert result.exit_code == EXIT_SUCCESS
        assert result.criterion_met is True
        assert "hello world" in result.detail.get("output", "")

    def test_command_not_found(self, tmp_path: Path):
        """Verify graceful handling of missing commands."""
        state_file = tmp_path / "state.json"
        from booley.criteria.state import DevelopmentState

        DevelopmentState.load(state_file).save()

        class MissingCmdFlow(BooleyFlow):
            name = "missing"
            description = "test"

            def _add_args(self, parser):
                pass

            def _build_command(self):
                return ["nonexistent_binary_12345"]

            def _interpret_result(self, result):
                if result.returncode != 0:
                    return McpToolResult(exit_code=EXIT_ERROR)
                return McpToolResult()

        env = _env_with_state(state_file)
        flow = MissingCmdFlow()
        with patch.dict(os.environ, env):
            flow.parse_args(["--target", "test"])
        flow.read_state()
        result = flow._run()
        assert result.exit_code == EXIT_ERROR

    def test_empty_command(self, tmp_path: Path):
        state_file = tmp_path / "state.json"
        from booley.criteria.state import DevelopmentState

        DevelopmentState.load(state_file).save()

        class EmptyFlow(BooleyFlow):
            name = "empty"
            description = "test"

            def _add_args(self, parser):
                pass

            def _build_command(self):
                return []

            def _interpret_result(self, result):
                return McpToolResult()

        env = _env_with_state(state_file)
        flow = EmptyFlow()
        with patch.dict(os.environ, env):
            flow.parse_args(["--target", "test"])
        flow.read_state()
        result = flow._run()
        assert result.exit_code == EXIT_ERROR


class TestResourceEvidence:
    def test_local_execution_without_project_data_uses_temp_cache(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from booley.criteria.state import DevelopmentState
        from booley.runtime.booley_dir import reset_cache

        state_file = tmp_path / "state.json"
        DevelopmentState.load(state_file).save()
        monkeypatch.delenv("BOOLEY_DIR", raising=False)
        monkeypatch.delenv("BOOLEY_RUNTIME_DIR", raising=False)
        reset_cache()
        flow = EchoFlow()
        with patch.dict(os.environ, _env_with_state(state_file), clear=True):
            flow.parse_args(["--target", "test", "--work-dir", str(tmp_path)])

        result = flow._execute_local([sys.executable, "-c", "print('ok')"])

        assert result.returncode == 0

    def test_local_execution_relocates_python_and_pytest_caches(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        helper = tmp_path / "custom_helper.py"
        helper.write_text("VALUE = 7\n", encoding="utf-8")
        nested = tmp_path / "test_custom.py"
        nested.write_text("def test_custom():\n    assert True\n", encoding="utf-8")
        for name in (
            "PYTHONPYCACHEPREFIX",
            "PYTHONDONTWRITEBYTECODE",
            "PYTEST_ADDOPTS",
            "PYTEST_XDIST_WORKER",
            "PYTEST_XDIST_TESTRUNUID",
        ):
            monkeypatch.delenv(name, raising=False)
        from booley.criteria.state import DevelopmentState

        state_file = tmp_path / "state.json"
        DevelopmentState.load(state_file).save()
        flow = EchoFlow()
        with patch.dict(os.environ, _env_with_state(state_file)):
            flow.parse_args(["--target", "test", "--work-dir", str(tmp_path)])
        script = (
            "import custom_helper, subprocess, sys; "
            "assert custom_helper.VALUE == 7; "
            f"raise SystemExit(subprocess.run([sys.executable, '-m', 'pytest', '--lf', "
            f"'-q', {str(nested)!r}]).returncode)"
        )

        result = flow._execute_local([sys.executable, "-c", script], timeout=30)

        assert result.returncode == 0, (
            f"timed_out={result.timed_out} after {result.duration_s:.1f} s\n"
            f"stdout:\n{result.stdout or '(none captured)'}\n"
            f"stderr:\n{result.stderr or '(none captured)'}"
        )
        runtime = tmp_path / ".runtime" / "python-artifacts"
        assert any((runtime / "bytecode").rglob("*.pyc"))
        assert any((runtime / "pytest").rglob("*"))
        assert not (tmp_path / "__pycache__").exists()
        assert not (tmp_path / ".pytest_cache").exists()

    @pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc")
    def test_local_execution_measures_descendant_rss(self, tmp_path: Path):
        state_file = tmp_path / "state.json"
        from booley.criteria.state import DevelopmentState

        DevelopmentState.load(state_file).save()
        flow = EchoFlow()
        with patch.dict(os.environ, _env_with_state(state_file)):
            flow.parse_args(["--target", "test", "--work-dir", str(tmp_path)])
        result = flow._execute_local(
            [
                sys.executable,
                "-c",
                "import time; payload = bytearray(16 * 1024 * 1024); time.sleep(0.6)",
            ]
        )
        assert result.returncode == 0
        assert result.peak_rss_mb is not None
        assert result.peak_rss_mb >= 16


class TestBoundaryExecutor:
    """Boundary-named helper is now a local Sandbox subprocess."""

    def test_runs_locally_and_stamps_dispatch_time(self, tmp_path: Path):
        state_file = tmp_path / "state.json"
        from booley.criteria.state import DevelopmentState

        DevelopmentState.load(state_file).save()
        flow = EchoFlow()
        with patch.dict(os.environ, _env_with_state(state_file)):
            flow.parse_args(["--target", "test", "--work-dir", str(tmp_path)])
        with patch.object(flow, "_execute") as execute:
            execute.return_value = SubprocessResult(returncode=0, stdout="ok")
            result = flow._execute_boundary(["make", "-C", "b/r"])
        execute.assert_called_once_with(["make", "-C", "b/r"])
        assert result.dispatched_unix > 0

    def test_stale_artifact_gate(self, tmp_path: Path):
        artifact = tmp_path / "report.rpt"
        artifact.write_text("data")
        mtime = artifact.stat().st_mtime
        assert BooleyFlow._is_stale_artifact(artifact, mtime + 60) is True
        assert BooleyFlow._is_stale_artifact(artifact, mtime - 60) is False
        assert BooleyFlow._is_stale_artifact(artifact, None) is False
        assert BooleyFlow._is_stale_artifact(tmp_path / "gone.rpt", mtime) is True


class TestOpenRunLog:
    """F-26 — every Booley Flow writes run.log only at the END of a run,
    so the file holds the PREVIOUS run's bytes for the whole duration of one.
    Claiming it at the start is what stops a tail reading that as live."""

    def _flow(self, tmp_path: Path) -> EchoFlow:
        state_file = tmp_path / "state.json"
        from booley.criteria.state import DevelopmentState

        DevelopmentState.load(state_file).save()
        flow = EchoFlow()
        with patch.dict(os.environ, _env_with_state(state_file)):
            flow.parse_args(["--target", "test", "--work-dir", str(tmp_path)])
        return flow

    def test_truncates_to_a_header_naming_this_run(self, tmp_path: Path):
        log_dir = tmp_path / "work"
        log_dir.mkdir()
        (log_dir / "run.log").write_text("PASSED (from an older run)\n", encoding="utf-8")

        self._flow(tmp_path)._open_run_log("sim_fifo", log_dir)

        content = (log_dir / "run.log").read_text(encoding="utf-8")
        assert "older run" not in content
        assert content.startswith("[BOOLEY RUN_LOG] ")
        assert "flow=echo_tool target=sim_fifo" in content

    def test_creates_a_missing_work_dir(self, tmp_path: Path):
        log_dir = tmp_path / "not" / "there" / "yet"
        self._flow(tmp_path)._open_run_log("sim_fifo", log_dir)
        assert (log_dir / "run.log").exists()

    def test_unwritable_dir_never_raises(self, tmp_path: Path):
        # A work dir we cannot write is the run's own problem to report.
        flow = self._flow(tmp_path)
        with patch("pathlib.Path.mkdir", side_effect=OSError("read-only fs")):
            flow._open_run_log("sim_fifo", tmp_path / "work")
        assert not (tmp_path / "work" / "run.log").exists()


class TestTimeoutKillsTheWholeTree:
    """A timed-out run must leave no orphan behind (SETUP-F-13).

    ``subprocess.run(timeout=...)`` kills only the direct child, so the
    grandchild that does the actual work (``python -m booley.flows.sim.backends.verilator``
    and its ``V<top>``) was reparented to init and kept burning a core — 99.9%
    CPU for 38+ minutes was observed after a single simulate timeout.
    """

    @staticmethod
    def _alive(pid: int) -> bool:
        """True while *pid* is a real, non-zombie process.

        A reaped-by-nobody grandchild lingers as a zombie in a container whose
        PID 1 does not reap, and ``os.kill(pid, 0)`` succeeds on zombies — so
        read the state field of /proc instead of signalling.
        """
        try:
            stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        except OSError:
            return False
        # "state" is the field right after the (possibly paren-laden) comm.
        return stat.rsplit(")", 1)[1].split()[0] != "Z"

    def _grandchild_flow(
        self, tmp_path: Path, pid_file: Path, *, own_session: bool = False, sleep_s: int = 120
    ):
        """A Flow whose child spawns a long-lived grandchild, then hangs.

        ``own_session`` starts the grandchild in a session of its own, out of
        reach of the group kill: a descendant that survived the kill.
        """
        import sys

        session_kwarg = ", start_new_session=True" if own_session else ""
        script = (
            "import os, subprocess, sys, time\n"
            f"kid = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep({sleep_s})']"
            f"{session_kwarg})\n"
            f"pid_path = {str(pid_file)!r}\n"
            "pid_tmp = pid_path + '.tmp'\n"
            "with open(pid_tmp, 'w') as stream:\n"
            "    stream.write(str(kid.pid))\n"
            "os.replace(pid_tmp, pid_path)\n"
            "time.sleep(120)\n"
        )

        class HangFlow(BooleyFlow):
            name = "hang_tool"
            description = "test"

            def _add_args(self, parser):
                pass

            def _build_command(self):
                return [sys.executable, "-c", script]

            def _interpret_result(self, result):
                return McpToolResult(exit_code=EXIT_ERROR if result.timed_out else EXIT_SUCCESS)

            def _get_timeout(self) -> int:
                return 2

        state_file = tmp_path / "state.json"
        from booley.criteria.state import DevelopmentState

        DevelopmentState.load(state_file).save()
        flow = HangFlow()
        with patch.dict(os.environ, _env_with_state(state_file)):
            flow.parse_args(["--target", "test", "--work-dir", str(tmp_path)])
        flow.read_state()
        return flow

    @pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc")
    def test_grandchild_is_killed_on_timeout(self, tmp_path: Path):
        pid_file = tmp_path / "grandchild.pid"
        flow = self._grandchild_flow(tmp_path, pid_file)

        real_communicate = flow_base._communicate_local_process
        waited_for_tree = False

        def communicate_once_tree_is_up(
            cmd: list[str], proc: subprocess.Popen[Any], *rest: Any, **kwargs: Any
        ) -> SubprocessResult:
            """Start the timeout clock only once the grandchild exists.

            The flow's 2 s timeout starts at ``Popen``, so on a loaded machine
            interpreter start-up alone can eat it and the tree is killed before
            the grandchild's PID is written. Wait for the PID file (or for the
            child to die) before the real, unchanged communicate-with-timeout
            runs.
            """
            nonlocal waited_for_tree
            waited_for_tree = True
            deadline = time.monotonic() + 45
            while not pid_file.exists() and proc.poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)
            return real_communicate(cmd, proc, *rest, **kwargs)

        with patch.object(flow_base, "_communicate_local_process", communicate_once_tree_is_up):
            result = flow._execute(flow._build_command())

        assert waited_for_tree, "the patched communicate was bypassed"
        assert result.timed_out is True, f"child did not time out: {result.stderr}"
        assert result.returncode == -1
        assert pid_file.exists(), "the process tree never came up within 45s"
        grandchild = int(pid_file.read_text(encoding="utf-8"))
        # killpg is asynchronous; give the group a moment to actually die.
        deadline = time.monotonic() + 10
        while self._alive(grandchild) and time.monotonic() < deadline:
            time.sleep(0.1)
        assert not self._alive(grandchild), (
            f"grandchild {grandchild} survived the timeout — the orphan leak is back"
        )

    def _reap_escaped_grandchild(self, pid_file: Path) -> None:
        """SIGKILL a grandchild that escaped the group kill, if it still lives."""
        if not pid_file.exists():
            return
        escaped = int(pid_file.read_text(encoding="utf-8"))
        # The liveness check avoids signalling a recycled PID.
        if not self._alive(escaped):
            return
        with contextlib.suppress(ProcessLookupError):
            os.kill(escaped, signal.SIGKILL)
        deadline = time.monotonic() + 10
        while self._alive(escaped) and time.monotonic() < deadline:
            time.sleep(0.05)

    @pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc")
    def test_timeout_does_not_hang_on_a_pipe_the_tree_held(self, tmp_path: Path):
        """The drain must not block behind a grandchild holding stdout open.

        Two ways this test used to pass without testing that. (1) The flow's
        timeout clock covers child start-up, so under load it could expire
        before the grandchild existed; nothing held the pipe and the drain
        returned at once. The wrapper below waits for the tree before the real
        communicate starts the clock's wait, and fails loudly if it never comes
        up. (2) The grandchild shared the child's session, so the group kill
        took it down and the pipe closed before the drain ran. The grandchild
        now runs in its own session, modelling a descendant that survived the
        kill, so only the drain bound can return in time.

        The drain bound is patched to 1 s to keep the idle runtime short; a
        change to the constant's value is not covered, only the call-site bound.
        The final liveness assertion assumes the kill is group-scoped; a kill
        that reaches other sessions would need a different escape model.
        """
        pid_file = tmp_path / "grandchild.pid"
        tree_wait_s = 15
        flow = self._grandchild_flow(tmp_path, pid_file, own_session=True, sleep_s=40)
        real_communicate = flow_base._communicate_local_process
        seen: dict[str, Any] = {}

        def _wait_for_tree_then_communicate(cmd, proc, scope, started, timeout):
            deadline = time.monotonic() + tree_wait_s
            while not pid_file.exists() and proc.poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)
            seen["tree_up"] = pid_file.exists()
            seen["child_returncode"] = proc.poll()
            if seen["tree_up"]:
                grandchild = int(pid_file.read_text(encoding="utf-8"))
                seen["alive"] = self._alive(grandchild)
                assert proc.stdout is not None
                try:
                    # A vanished grandchild must not abort before the real
                    # communicate runs, or the hung child would leak.
                    seen["holds_stdout"] = (
                        Path(f"/proc/{grandchild}/fd/1").readlink()
                        == Path(f"/proc/self/fd/{proc.stdout.fileno()}").readlink()
                    )
                except OSError:
                    seen["holds_stdout"] = False
            seen["clock_start"] = time.monotonic()
            return real_communicate(cmd, proc, scope, started, timeout)

        try:
            with (
                patch.object(
                    flow_base, "_communicate_local_process", _wait_for_tree_then_communicate
                ),
                patch.object(flow_base, "_FLOW_KILL_DRAIN_TIMEOUT_S", 1),
            ):
                result = flow._execute(flow._build_command())
            if "clock_start" not in seen:
                pytest.fail("the communicate seam was never reached — the flow ran another path")
            elapsed = time.monotonic() - seen["clock_start"]

            if not seen["tree_up"]:
                if seen["child_returncode"] is not None:
                    pytest.fail(
                        f"child exited (rc={seen['child_returncode']}) before spawning the grandchild"
                    )
                pytest.fail(
                    f"process tree never came up within {tree_wait_s} s — the hang was not exercised"
                )
            assert seen["alive"], "grandchild was not alive when the clock started"
            assert seen["holds_stdout"], "grandchild did not hold the child's stdout"
            assert result.timed_out is True, result.stderr
            # 2s budget + kill grace + 1s patched drain; a drain that blocks
            # behind the grandchild lasts until its 40s sleep ends.
            assert elapsed < 30, f"drain blocked {elapsed:.1f}s behind a grandchild holding stdout"
            grandchild = int(pid_file.read_text(encoding="utf-8"))
            assert self._alive(grandchild), (
                "grandchild died, so the drain returned because the pipe closed, "
                "not because its bound expired"
            )
        finally:
            self._reap_escaped_grandchild(pid_file)

    @pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc")
    def test_ctrl_c_kills_the_whole_tree(self, tmp_path: Path, caplog: pytest.LogCaptureFixture):
        """Ctrl-C is the OTHER orphan path, and it needs the same teardown.

        ``start_new_session`` takes the child out of the terminal's foreground
        process group, so a SIGINT from the tty reaches Booley alone. Without a
        kill on the exception path the ``KeyboardInterrupt`` walks out of
        ``_execute_local``, Booley exits, and yosys+abc (or the run-half and its
        ``V<top>``) keep running reparented to init — the very orphan class the
        process-group spawn was added to prevent.
        """
        import subprocess

        pid_file = tmp_path / "grandchild.pid"
        flow = self._grandchild_flow(tmp_path, pid_file)

        def _interrupt(_self, *_args, **_kwargs):
            # Let the tree come up first, then behave like a tty Ctrl-C.
            deadline = time.monotonic() + 10
            while not pid_file.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            raise KeyboardInterrupt

        with (
            caplog.at_level(logging.INFO, logger="booley.flows.base"),
            patch.object(subprocess.Popen, "communicate", _interrupt),
            pytest.raises(KeyboardInterrupt),
        ):
            flow._execute(flow._build_command())

        # Never WARNING: a bare CLI would print it as a second interrupted line.
        records = [r for r in caplog.records if r.name == "booley.flows.base"]
        assert not [r for r in records if r.levelno >= logging.WARNING]
        (killed,) = [r for r in records if r.getMessage().startswith("Interrupted; killing")]
        assert killed.levelno == logging.INFO

        grandchild = int(pid_file.read_text(encoding="utf-8"))
        deadline = time.monotonic() + 10
        while self._alive(grandchild) and time.monotonic() < deadline:
            time.sleep(0.1)
        assert not self._alive(grandchild), (
            f"grandchild {grandchild} survived Ctrl-C — the orphan leak is back"
        )

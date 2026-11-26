"""Tests for BooleyFlow — subprocess execution, sentinel scanning, exit codes."""

from __future__ import annotations

import argparse
import contextlib
import logging
import math
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from booley.flows import base as flow_base
from booley.flows.base import BooleyFlow, SubprocessResult
from booley.mcp.base import EXIT_ERROR, EXIT_FAILURE, EXIT_SUCCESS, McpToolResult
from booley.runtime.endpoint_execution import ExecutionResult
from tests.memory_budget_support import fail_budget
from tests.subprocess_budget import STALL_DUMP_S
from tests.time_budget import hang_guard_s, scaled

# A grandchild must outlive the tree wait, the flow timeout, the kill and the death poll,
# so a broken kill cannot pass because the grandchild exited on its own.
_GRANDCHILD_LIFETIME_S = math.ceil(scaled(120))


def _flow_env(slug: str = "test") -> dict[str, str]:
    env = os.environ.copy()
    env["BOOLEY_SLUG"] = slug
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
                detail={"output": result.stdout.strip()},
            )
        return McpToolResult(exit_code=EXIT_FAILURE)


class CommandFlow(EchoFlow):
    """EchoFlow that runs its ``command`` attribute and keeps what each run produced."""

    name = "command_tool"

    def __init__(self) -> None:
        super().__init__()
        self.command: list[str] = []
        self.seen: list[SubprocessResult] = []

    def _build_command(self) -> list[str]:
        return self.command

    def _interpret_result(self, result: SubprocessResult) -> McpToolResult:
        self.seen.append(result)
        if result.returncode == 0:
            return McpToolResult(exit_code=EXIT_SUCCESS, report_text=result.stdout.strip())
        return McpToolResult(exit_code=EXIT_FAILURE, report_text=result.stderr)


def _run_command(
    root: Path,
    command: list[str],
    monkeypatch: pytest.MonkeyPatch,
    *,
    booley_dir: bool = True,
) -> tuple[ExecutionResult, list[SubprocessResult]]:
    """Run *command* as a Custom Flow through its public CLI entry point."""
    if booley_dir:
        monkeypatch.setenv("BOOLEY_DIR", str(root))
    flow = CommandFlow()
    flow.command = command
    result = flow.execute_cli(
        ["--work-dir", str(root), "--target", "t", "--report-dir", str(root / "reports")]
    )
    return result, flow.seen


class TestBooleyFlowExecution:
    def test_host_entry_is_rejected_before_flow_execution(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        from booley.runtime import runtime_context

        monkeypatch.setattr(runtime_context, "inside_session_runtime", lambda: False)
        flow = CommandFlow()
        flow.command = ["false"]
        exit_code = flow.main(["--target", "test", "--work-dir", str(tmp_path)])

        assert exit_code == EXIT_ERROR
        assert flow.seen == []
        assert "Sandbox" in capsys.readouterr().err

    @pytest.mark.parametrize(
        ("command", "exit_code", "text"),
        [
            pytest.param(["echo", "hello world"], EXIT_SUCCESS, "hello world", id="success"),
            pytest.param(["nonexistent_binary_12345"], EXIT_FAILURE, "", id="command-not-found"),
            pytest.param([], EXIT_ERROR, "No command to execute", id="empty-command"),
        ],
    )
    def test_command_outcomes(
        self,
        session_runtime: Path,
        monkeypatch: pytest.MonkeyPatch,
        command: list[str],
        exit_code: int,
        text: str,
    ) -> None:
        fail_budget(monkeypatch)
        result, seen = _run_command(session_runtime, command, monkeypatch)

        assert result.exit_code == exit_code, result.outcome.report_text
        assert text in result.outcome.report_text
        # Only a Custom Flow that ran a command reports that it ran alone.
        assert ("memory_admission_warning" in result.outcome.detail) == bool(command)
        if command == ["nonexistent_binary_12345"]:
            assert seen[0].returncode != 0


class TestResourceEvidence:
    def test_local_execution_without_project_data_uses_temp_cache(
        self, session_runtime: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from booley.runtime.booley_dir import reset_cache

        reset_cache()

        result, _seen = _run_command(
            session_runtime,
            [sys.executable, "-c", "print('ok')"],
            monkeypatch,
            booley_dir=False,
        )

        assert result.exit_code == EXIT_SUCCESS, result.outcome.report_text

    def test_local_execution_relocates_python_and_pytest_caches(
        self, session_runtime: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tmp_path = session_runtime
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

        script = (
            "import custom_helper, subprocess, sys; "
            "assert custom_helper.VALUE == 7; "
            f"raise SystemExit(subprocess.run([sys.executable, '-m', 'pytest', "
            f"'-o', 'faulthandler_timeout={STALL_DUMP_S:g}', '--lf', "
            f"'-q', {str(nested)!r}]).returncode)"
        )

        result, seen = _run_command(tmp_path, [sys.executable, "-c", script], monkeypatch)

        assert result.exit_code == EXIT_SUCCESS, (
            f"timed_out={seen[0].timed_out} after {seen[0].duration_s:.1f} s\n"
            f"stdout:\n{seen[0].stdout or '(none captured)'}\n"
            f"stderr:\n{seen[0].stderr or '(none captured)'}"
        )
        runtime = tmp_path / ".runtime" / "python-artifacts"
        assert any((runtime / "bytecode").rglob("*.pyc"))
        assert any((runtime / "pytest").rglob("*"))
        assert not (tmp_path / "__pycache__").exists()
        assert not (tmp_path / ".pytest_cache").exists()

    @pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc")
    def test_local_execution_measures_descendant_rss(
        self, session_runtime: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result, seen = _run_command(
            session_runtime,
            [
                sys.executable,
                "-c",
                "import time; payload = bytearray(16 * 1024 * 1024); time.sleep(0.6)",
            ],
            monkeypatch,
        )

        assert result.exit_code == EXIT_SUCCESS
        assert seen[0].peak_rss_mb is not None
        assert seen[0].peak_rss_mb >= 16


def test_an_artifact_that_cannot_be_read_is_stale(tmp_path: Path) -> None:
    # Private seam: every caller checks that the file exists first, so only a
    # file that vanishes between that check and the stat reaches this rule.
    assert BooleyFlow._is_stale_artifact(tmp_path / "gone.rpt", 0.0) is True


class TestOpenRunLog:
    """F-26 — every Booley Flow writes run.log only at the END of a run,
    so the file holds the PREVIOUS run's bytes for the whole duration of one.
    Claiming it at the start is what stops a tail reading that as live."""

    @pytest.mark.parametrize("unwritable", [False, True], ids=["missing-folder", "unwritable"])
    def test_run_log_opening_creates_its_folder_and_never_raises(
        self, tmp_path: Path, unwritable: bool
    ) -> None:
        # A work dir we cannot write is the run's own problem to report.
        log_dir = tmp_path / "not" / "there" / "yet"
        flow = EchoFlow()
        with patch.dict(os.environ, _flow_env()):
            flow.parse_args(["--target", "test", "--work-dir", str(tmp_path)])

        # Private seam: the Flows open the log inside their own run, after Target
        # resolution and a toolchain build, so only a direct call controls the
        # folder it gets.
        if unwritable:
            with patch("pathlib.Path.mkdir", side_effect=OSError("read-only fs")):
                flow._open_run_log("sim_fifo", log_dir)
        else:
            flow._open_run_log("sim_fifo", log_dir)

        assert (log_dir / "run.log").exists() is not unwritable


class TestTimeoutKillsTheWholeTree:
    """A timed-out run must leave no orphan behind (SETUP-F-13).

    Private seam: the timeout clock must start only once the grandchild exists,
    which needs the ``_communicate_local_process`` seam; the drain bound is
    patched to 1 s (scaled); and the Ctrl-C test needs the ``KeyboardInterrupt`` to leave
    ``_execute`` raw (through ``execute_cli`` the lifecycle turns it into exit 130).

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
        self,
        tmp_path: Path,
        pid_file: Path,
        *,
        own_session: bool = False,
        sleep_s: int = _GRANDCHILD_LIFETIME_S,
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
                return math.ceil(scaled(2))

        flow = HangFlow()
        with patch.dict(os.environ, _flow_env()):
            flow.parse_args(["--target", "test", "--work-dir", str(tmp_path)])
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
            deadline = time.monotonic() + hang_guard_s()
            while not pid_file.exists() and proc.poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)
            return real_communicate(cmd, proc, *rest, **kwargs)

        with patch.object(flow_base, "_communicate_local_process", communicate_once_tree_is_up):
            result = flow._execute(flow._build_command())

        assert waited_for_tree, "the patched communicate was bypassed"
        assert result.timed_out is True, f"child did not time out: {result.stderr}"
        assert result.returncode == -1
        assert pid_file.exists(), f"the process tree never came up within {hang_guard_s()}s"
        grandchild = int(pid_file.read_text(encoding="utf-8"))
        # killpg is asynchronous; give the group a moment to actually die.
        deadline = time.monotonic() + hang_guard_s()
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
        deadline = time.monotonic() + hang_guard_s()
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

        The drain bound is patched to 1 s (scaled) to keep the idle runtime short; a
        change to the constant's value is not covered, only the call-site bound.
        The final liveness assertion assumes the kill is group-scoped; a kill
        that reaches other sessions would need a different escape model.
        """
        pid_file = tmp_path / "grandchild.pid"
        tree_wait_s = hang_guard_s()
        flow = self._grandchild_flow(
            tmp_path, pid_file, own_session=True, sleep_s=math.ceil(scaled(40))
        )
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
                patch.object(flow_base, "_FLOW_KILL_DRAIN_TIMEOUT_S", scaled(1)),
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
            # 2 s budget + kill grace + 1 s patched drain (each scaled); a drain that blocks
            # behind the grandchild lasts until its sleep ends.
            assert elapsed < scaled(30), (
                f"drain blocked {elapsed:.1f}s behind a grandchild holding stdout"
            )
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
            deadline = time.monotonic() + hang_guard_s()
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
        deadline = time.monotonic() + hang_guard_s()
        while self._alive(grandchild) and time.monotonic() < deadline:
            time.sleep(0.1)
        assert not self._alive(grandchild), (
            f"grandchild {grandchild} survived Ctrl-C — the orphan leak is back"
        )

"""Tests for BooleyFlow — subprocess execution, sentinel scanning, exit codes."""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from typing import ClassVar
from unittest.mock import patch

import pytest

from booley.dev_support.criteria import cycle_count_criterion_key
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
        from booley.dev_support.development_state import DevelopmentState
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
        assert "Session Runtime" in capsys.readouterr().err

    def test_successful_execution(self, tmp_path: Path):
        state_file = tmp_path / "state.json"
        from booley.dev_support.development_state import DevelopmentState

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
        from booley.dev_support.development_state import DevelopmentState

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

    def test_target_contract_is_checked_before_flow_entry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from booley.runtime import runtime_context
        from booley.ticket_board.frontmatter import format_frontmatter
        from booley.ticket_board.target_contract import build_contract

        contract = build_contract(tmp_path, outer_sha="a" * 40)
        ticket = tmp_path / "ticket.md"
        ticket.write_text(
            format_frontmatter(
                {
                    "base_sha": contract.outer_sha,
                    "target_contract": contract.as_dict(),
                },
                "ticket",
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(runtime_context, "inside_session_runtime", lambda: True)
        monkeypatch.setenv("BOOLEY_TICKET_FILE", str(ticket))
        flow = EchoFlow()
        flow.parse_args(["--target", "test", "--work-dir", str(tmp_path)])
        assert flow._pre_state_gate() is None

        (tmp_path / "changed.core").write_text("CAPI=2:\nname: ::changed:0\n")
        rejected = flow._pre_state_gate()

        assert rejected is not None
        assert rejected.exit_code == EXIT_ERROR
        assert "target-contract-change-required" in rejected.report_text

    def test_empty_command(self, tmp_path: Path):
        state_file = tmp_path / "state.json"
        from booley.dev_support.development_state import DevelopmentState

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


class TestSubprocessResult:
    def test_defaults(self):
        r = SubprocessResult()
        assert r.returncode == -1
        assert r.stdout == ""
        assert r.stderr == ""
        assert r.timed_out is False


class TestResourceEvidence:
    @pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc")
    def test_local_execution_measures_descendant_rss(self, tmp_path: Path):
        state_file = tmp_path / "state.json"
        from booley.dev_support.development_state import DevelopmentState

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

    def test_cgroup_oom_counter_parser(self, tmp_path: Path, monkeypatch):
        from booley.flows import base as flow_base

        events = tmp_path / "memory.events"
        events.write_text("low 0\nhigh 0\noom 3\noom_kill 2\n", encoding="utf-8")
        monkeypatch.setattr(flow_base, "_CGROUP_MEMORY_EVENT_PATHS", (events,))
        assert flow_base._cgroup_oom_kill_count() == 2


class TestBoundaryExecutor:
    """Boundary-named helper is now a local Session Runtime subprocess."""

    def test_runs_locally_and_stamps_dispatch_time(self, tmp_path: Path):
        state_file = tmp_path / "state.json"
        from booley.dev_support.development_state import DevelopmentState

        DevelopmentState.load(state_file).save()
        flow = EchoFlow()
        with patch.dict(os.environ, _env_with_state(state_file)):
            flow.parse_args(["--target", "test", "--work-dir", str(tmp_path)])
        with patch.object(flow, "_execute") as execute:
            execute.return_value = SubprocessResult(returncode=0, stdout="ok")
            result = flow._execute_boundary(["make", "-C", "b/r"])
        execute.assert_called_once_with(["make", "-C", "b/r"])
        assert result.returncode == 0
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
        from booley.dev_support.development_state import DevelopmentState

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
    grandchild that does the actual work (``python -m booley.sim.verilator_run``
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

    def _grandchild_flow(self, tmp_path: Path, pid_file: Path):
        """A Flow whose child spawns a long-lived grandchild, then hangs."""
        import sys

        script = (
            "import subprocess, sys, time\n"
            "kid = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
            f"open({str(pid_file)!r}, 'w').write(str(kid.pid))\n"
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
        from booley.dev_support.development_state import DevelopmentState

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

        result = flow._execute(flow._build_command())

        assert result.timed_out is True
        assert result.returncode == -1
        grandchild = int(pid_file.read_text(encoding="utf-8"))
        # killpg is asynchronous; give the group a moment to actually die.
        deadline = time.monotonic() + 10
        while self._alive(grandchild) and time.monotonic() < deadline:
            time.sleep(0.1)
        assert not self._alive(grandchild), (
            f"grandchild {grandchild} survived the timeout — the orphan leak is back"
        )

    @pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc")
    def test_timeout_does_not_hang_on_a_pipe_the_tree_held(self, tmp_path: Path):
        """The drain must not block behind a grandchild holding stdout open."""
        pid_file = tmp_path / "grandchild.pid"
        flow = self._grandchild_flow(tmp_path, pid_file)

        start = time.monotonic()
        flow._execute(flow._build_command())
        # 2s budget + kill grace; anything near the child's 120s sleep is a hang.
        assert time.monotonic() - start < 30

    @pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc")
    def test_ctrl_c_kills_the_whole_tree(self, tmp_path: Path):
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
            patch.object(subprocess.Popen, "communicate", _interrupt),
            pytest.raises(KeyboardInterrupt),
        ):
            flow._execute(flow._build_command())

        grandchild = int(pid_file.read_text(encoding="utf-8"))
        deadline = time.monotonic() + 10
        while self._alive(grandchild) and time.monotonic() < deadline:
            time.sleep(0.1)
        assert not self._alive(grandchild), (
            f"grandchild {grandchild} survived Ctrl-C — the orphan leak is back"
        )

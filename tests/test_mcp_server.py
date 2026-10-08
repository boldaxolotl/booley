"""Tests for mcp_server: parameter conversion, subprocess dispatch, result formatting."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# We can test the pure helpers without importing the full MCP stack.
# Import selectively to avoid needing the `mcp` package installed.


# ---------------------------------------------------------------------------
# _params_to_argv
# ---------------------------------------------------------------------------


class TestParamsToArgv:
    @pytest.fixture(autouse=True)
    def _import(self):
        # Stub mcp.server and mcp.types to avoid import errors
        mcp_stubs = {
            "mcp": MagicMock(),
            "mcp.server": MagicMock(),
            "mcp.server.models": MagicMock(),
            "mcp.server.request_state": MagicMock(),
            "mcp.server.stdio": MagicMock(),
            "mcp.types": MagicMock(),
        }
        with patch.dict(sys.modules, mcp_stubs):
            from booley.mcp.server import _params_to_argv

            self._params_to_argv = _params_to_argv

    def test_option_like_value_uses_eq_form(self):
        # F-12: as the next argv item, argparse would read the selector as a
        # new option and drop it; the `=` form keeps it attached to its flag.
        result = self._params_to_argv({"test": "--meminit=ram,firmware.elf"})
        assert result == ["--test=--meminit=ram,firmware.elf"]

    def test_option_like_list_items_use_eq_form(self):
        result = self._params_to_argv({"defines": ["-DFOO", "BAR"]})
        assert result == ["--defines=-DFOO", "--defines", "BAR"]


# ---------------------------------------------------------------------------
# _McpLifetime
# ---------------------------------------------------------------------------


class TestMcpLifetime:
    @pytest.fixture(autouse=True)
    def _import(self):
        mcp_stubs = {
            "mcp": MagicMock(),
            "mcp.server": MagicMock(),
            "mcp.server.models": MagicMock(),
            "mcp.server.request_state": MagicMock(),
            "mcp.server.stdio": MagicMock(),
            "mcp.types": MagicMock(),
        }
        with patch.dict(sys.modules, mcp_stubs):
            from booley.mcp.server import _env_timeout_seconds, _McpLifetime

            self._McpLifetime = _McpLifetime
            self._env_timeout_seconds = _env_timeout_seconds

    def test_idle_timeout_requests_exit(self):
        now = 100.0
        lifetime = self._McpLifetime(
            idle_timeout_seconds=10,
            max_age_seconds=None,
            now=lambda: now,
        )

        should_exit, reason = lifetime.should_exit()
        assert not should_exit
        assert reason == ""

        now = 111.0
        should_exit, reason = lifetime.should_exit()
        assert should_exit
        assert "idle" in reason

    def test_in_flight_mcp_tool_suppresses_exit(self):
        now = 100.0
        lifetime = self._McpLifetime(
            idle_timeout_seconds=10,
            max_age_seconds=None,
            now=lambda: now,
        )
        lifetime.mark_mcp_endpoint_start()

        now = 200.0
        should_exit, reason = lifetime.should_exit()
        assert not should_exit
        assert reason == ""

        lifetime.mark_mcp_endpoint_end()
        should_exit, reason = lifetime.should_exit()
        assert not should_exit

    def test_max_age_exits_after_in_flight_mcp_tool_finishes(self):
        now = 100.0
        lifetime = self._McpLifetime(
            idle_timeout_seconds=None,
            max_age_seconds=10,
            now=lambda: now,
        )
        lifetime.mark_mcp_endpoint_start()

        now = 200.0
        should_exit, reason = lifetime.should_exit()
        assert not should_exit

        lifetime.mark_mcp_endpoint_end()
        should_exit, reason = lifetime.should_exit()
        assert should_exit
        assert "older" in reason

    def test_env_timeout_zero_disables(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("BOOLEY_TEST_TIMEOUT", "0")
        assert self._env_timeout_seconds("BOOLEY_TEST_TIMEOUT", 5) is None

    def test_heartbeat_written_for_reaper(self, tmp_path):
        # ADR 0018 WS4: wall-clock heartbeat for the external reaper.
        hb = tmp_path / "hb"
        lifetime = self._McpLifetime(
            idle_timeout_seconds=10,
            max_age_seconds=None,
            heartbeat_path=str(hb),
        )
        assert hb.exists()  # written at construction
        first = hb.read_text(encoding="utf-8")
        lifetime.mark_activity()
        assert hb.read_text(encoding="utf-8")  # refreshed on activity
        # Value is epoch seconds (wall clock), not the injected monotonic now.
        assert float(first.strip()) > 1_000_000_000

    def test_no_heartbeat_when_path_unset(self, tmp_path):
        # heartbeat_path=None disables the reaper heartbeat entirely.
        lifetime = self._McpLifetime(idle_timeout_seconds=None, max_age_seconds=None)
        lifetime.mark_activity()  # must not raise

    def test_from_env_ticket_stdio_writes_heartbeat(self, tmp_path, monkeypatch):
        # ADR 0028 Decision 11: Ticket Mode stdio servers share the Session
        # Runtime container, so their MCP tool activity must feed the reaper
        # heartbeat too — an active ticket never reads as idle. No self-exit:
        # the spawning client owns the process lifetime.
        import booley.mcp.server as mod

        hb = tmp_path / "hb"
        monkeypatch.setattr(mod, "_MCP_HEARTBEAT_PATH", str(hb))
        monkeypatch.delenv("BOOLEY_MCP_MODE", raising=False)
        lifetime = mod._McpLifetime.from_env()
        assert lifetime.idle_timeout_seconds is None
        assert lifetime.max_age_seconds is None
        assert hb.exists()  # heartbeat written at construction

    def test_from_env_http_disables_self_exit_keeps_heartbeat(
        self,
        tmp_path,
        monkeypatch,
    ):
        # ADR 0023: the HTTP server must never self-exit (clients reconnect to
        # its URL for the container's whole life), but the reaper heartbeat
        # stays so idle containers are still stopped at the container level.
        # Use the module's own class: the fixture's import can be a different,
        # orphaned module instance (patch.dict removes it from sys.modules).
        import booley.mcp.server as mod

        hb = tmp_path / "hb"
        monkeypatch.setattr(mod, "_MCP_HEARTBEAT_PATH", str(hb))
        monkeypatch.setenv("BOOLEY_MCP_MODE", "interactive")
        lifetime = mod._McpLifetime.from_env(self_exit=False)
        assert lifetime.idle_timeout_seconds is None
        assert lifetime.max_age_seconds is None
        assert hb.exists()  # heartbeat written at construction

    def test_from_env_stdio_keeps_self_exit(self, tmp_path, monkeypatch):
        import booley.mcp.server as mod

        monkeypatch.setattr(mod, "_MCP_HEARTBEAT_PATH", str(tmp_path / "hb"))
        monkeypatch.setenv("BOOLEY_MCP_MODE", "interactive")
        lifetime = mod._McpLifetime.from_env()
        assert lifetime.idle_timeout_seconds is not None
        assert lifetime.max_age_seconds is not None


class TestHttpPort:
    def test_default(self, monkeypatch):
        from booley.mcp.server import DEFAULT_HTTP_PORT, http_port

        monkeypatch.delenv("BOOLEY_MCP_HTTP_PORT", raising=False)
        assert http_port() == DEFAULT_HTTP_PORT

    def test_env_override(self, monkeypatch):
        from booley.mcp.server import http_port

        monkeypatch.setenv("BOOLEY_MCP_HTTP_PORT", "9123")
        assert http_port() == 9123

    def test_invalid_falls_back(self, monkeypatch):
        from booley.mcp.server import DEFAULT_HTTP_PORT, http_port

        monkeypatch.setenv("BOOLEY_MCP_HTTP_PORT", "not-a-port")
        assert http_port() == DEFAULT_HTTP_PORT
        monkeypatch.setenv("BOOLEY_MCP_HTTP_PORT", "70000")
        assert http_port() == DEFAULT_HTTP_PORT


# ---------------------------------------------------------------------------
# _format_mcp_tool_result
# ---------------------------------------------------------------------------


class TestFormatMcpToolResult:
    @pytest.fixture(autouse=True)
    def _import(self):
        mcp_stubs = {
            "mcp": MagicMock(),
            "mcp.server": MagicMock(),
            "mcp.server.models": MagicMock(),
            "mcp.server.request_state": MagicMock(),
            "mcp.server.stdio": MagicMock(),
            "mcp.types": MagicMock(),
        }
        with patch.dict(sys.modules, mcp_stubs):
            from booley.mcp.server import _format_mcp_tool_result

            self._format_mcp_tool_result = _format_mcp_tool_result

    def test_with_report(self):
        report = {
            "status": "pass",
            "summary": "all good",
            "errors": [],
            "report_text": "RESULT: PASS",
            "detail": {"reason": "done", "error": "none"},
        }
        result = self._format_mcp_tool_result(0, "output", "", report)
        assert "status: pass" in result
        assert "summary: all good" in result
        assert "report_text: RESULT: PASS" in result
        assert "detail.reason: done" in result
        assert "detail.error: none" in result

    @pytest.mark.parametrize(
        ("stdout", "stderr"),
        [("RESULT: PASS\n", ""), ("", "RESULT: PASS\n")],
    )
    def test_report_text_already_displayed_as_whole_lines_is_not_repeated(
        self, stdout: str, stderr: str
    ) -> None:
        result = self._format_mcp_tool_result(
            0,
            stdout,
            stderr,
            {"report_text": "RESULT: PASS"},
        )

        assert result.count("RESULT: PASS") == 1
        assert "report_text:" not in result

    @pytest.mark.parametrize(
        ("stdout", "stderr"),
        [
            ("progress mentions RESULT: PASS but keeps going\n", ""),
            ("", "progress mentions RESULT: PASS but keeps going\n"),
        ],
    )
    def test_report_text_mentioned_inside_a_log_line_is_retained(
        self, stdout: str, stderr: str
    ) -> None:
        result = self._format_mcp_tool_result(
            0,
            stdout,
            stderr,
            {"report_text": "RESULT: PASS"},
        )

        assert "report_text: RESULT: PASS" in result

    def test_report_text_truncated_out_of_displayed_streams_is_retained(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BOOLEY_MCP_MAX_STDOUT_BYTES", "12")
        monkeypatch.setenv("BOOLEY_MCP_MAX_STDERR_BYTES", "12")

        result = self._format_mcp_tool_result(
            1,
            "RESULT: FAIL\n" + "x" * 20,
            "RESULT: FAIL\n" + "y" * 20,
            {"report_text": "RESULT: FAIL"},
        )

        assert "report_text: RESULT: FAIL" in result

    def test_unrelated_stderr_and_report_are_both_retained(self) -> None:
        result = self._format_mcp_tool_result(
            1,
            "",
            "compiler warning\n",
            {"report_text": "RESULT: FAIL"},
        )

        assert "compiler warning" in result
        assert "report_text: RESULT: FAIL" in result


# ---------------------------------------------------------------------------
# _run_subprocess
# ---------------------------------------------------------------------------


class TestRunSubprocess:
    @pytest.fixture(autouse=True)
    def _import(self):
        mcp_stubs = {
            "mcp": MagicMock(),
            "mcp.server": MagicMock(),
            "mcp.server.models": MagicMock(),
            "mcp.server.request_state": MagicMock(),
            "mcp.server.stdio": MagicMock(),
            "mcp.types": MagicMock(),
        }
        with patch.dict(sys.modules, mcp_stubs):
            from booley.mcp.server import _run_subprocess

            self._run_subprocess = _run_subprocess

    def test_successful_run(self):
        # Real subprocess via asyncio — `python -c` is portable on Win/Linux.
        import asyncio

        code, out, _err, timed_out = asyncio.run(
            self._run_subprocess([sys.executable, "-c", "print('ok')"]),
        )
        assert code == 0
        assert "ok" in out
        assert timed_out is False

    def test_timeout(self):
        import asyncio

        code, _out, err, timed_out = asyncio.run(
            self._run_subprocess(
                [sys.executable, "-c", "import time; time.sleep(5)"],
                timeout=1,
            ),
        )
        assert code == 2
        assert "timed out" in err.lower()
        assert timed_out is True

    def test_os_error(self):
        import asyncio

        code, _out, err, timed_out = asyncio.run(
            self._run_subprocess(["__definitely_not_a_real_binary__"]),
        )
        assert code == 2
        assert err  # message describes the launch failure
        assert timed_out is False

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX process-group semantics")
    def test_cancellation_kills_process_group(self, tmp_path):
        """Interrupting a MCP tool cancels this coroutine; the whole subprocess
        group must be reaped so an interrupted `simulate` can't leave an
        orphaned simulator holding the sim lock.
        """
        import asyncio

        started = tmp_path / "started"
        # A grandchild in the same session writes this only if it outlives the
        # kill. Its absence proves the group (not just the direct child) died.
        survived = tmp_path / "survived"
        script = (
            "import os, sys, time, subprocess, pathlib\n"
            "subprocess.Popen([sys.executable, '-c',"
            f' "import time, pathlib; time.sleep(1.5);'
            f" pathlib.Path({str(survived)!r}).write_text('x')\"])\n"
            f"pathlib.Path({str(started)!r}).write_text(str(os.getpid()))\n"
            "time.sleep(30)\n"
        )

        async def drive():
            task = asyncio.create_task(
                self._run_subprocess([sys.executable, "-c", script], timeout=30)
            )
            for _ in range(250):  # up to ~5s for the child to come up
                if started.exists():
                    break
                await asyncio.sleep(0.02)
            assert started.exists(), "subprocess never started"
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await asyncio.sleep(2.0)  # past the grandchild's 1.5s write attempt

        asyncio.run(drive())
        assert not survived.exists(), (
            "grandchild survived cancellation — process group was not killed"
        )

    def test_stdout_tail_cap(self):
        """Output larger than cap is truncated to the tail."""
        import asyncio

        from booley.mcp.server import _stdout_cap_bytes

        cap = _stdout_cap_bytes()
        # Print 2x the cap; verify we keep only the tail (ends with last marker).
        script = (
            "import sys\n"
            f"chunk = 'A' * 1024\n"
            f"for _ in range({(cap * 2) // 1024}): sys.stdout.write(chunk)\n"
            "sys.stdout.write('END_MARKER')\n"
        )
        code, out, _err, _timed_out = asyncio.run(
            self._run_subprocess([sys.executable, "-c", script]),
        )
        assert code == 0
        assert out.endswith("END_MARKER")
        assert len(out.encode("utf-8")) <= cap


class TestMcpToolTimeoutSeconds:
    @pytest.fixture(autouse=True)
    def _import(self):
        mcp_stubs = {
            "mcp": MagicMock(),
            "mcp.server": MagicMock(),
            "mcp.server.models": MagicMock(),
            "mcp.server.request_state": MagicMock(),
            "mcp.server.stdio": MagicMock(),
            "mcp.types": MagicMock(),
        }
        with patch.dict(sys.modules, mcp_stubs):
            from booley.mcp.server import _mcp_tool_timeout_seconds

            self._mcp_tool_timeout_seconds = _mcp_tool_timeout_seconds

    def test_simulate_trace_timeout_gets_cleanup_margin(self):
        timeout = self._mcp_tool_timeout_seconds(
            "sim",
            {"timeout_ms": 10_000, "trace": True},
            {"default_timeout": 600},
        )
        assert timeout == 4330

    def test_simulate_non_trace_timeout_gets_small_margin(self):
        timeout = self._mcp_tool_timeout_seconds(
            "sim",
            {"timeout_ms": 10_000, "trace": False},
            {"default_timeout": 600},
        )
        assert timeout == 4240

    def test_simulate_configured_default_gets_small_margin(self, tmp_path):
        with patch(
            "booley.flows.sim.flow._resolve_sim_timeout_ms",
            return_value=600_000,
        ):
            timeout = self._mcp_tool_timeout_seconds(
                "sim",
                {"work_dir": str(tmp_path), "trace": False},
                {"default_timeout": 600},
            )
        assert timeout == 4830

    def test_simulate_campaign_budget_scales_by_work_units(self):
        with patch(
            "booley.flows.sim.flow._resolve_sim_campaign_work_units",
            return_value=4,
        ):
            timeout = self._mcp_tool_timeout_seconds(
                "sim",
                {"target": "a,b", "timeout_ms": 600_000, "trace": False},
                {"default_timeout": 1290},
            )
        assert timeout == 2 * 3600 + 4 * (600 + 600) + 30

    def test_simulate_trace_margin_scales_by_work_units(self):
        with patch(
            "booley.flows.sim.flow._resolve_sim_campaign_work_units",
            return_value=3,
        ):
            timeout = self._mcp_tool_timeout_seconds(
                "sim",
                {"target": "a,b,c", "timeout_ms": 600_000, "trace": True},
                {"default_timeout": 1290},
            )
        assert timeout == 3 * (3600 + 600 + 600) + 3 * 90 + 30

    def test_elab_only_standalone_budget_counts_targets_and_one_sweep(self):
        from booley.flows.sim.mode import SimulationMode

        with patch(
            "booley.flows.sim.flow._resolve_sim_campaign_work_units",
            return_value=3,
        ) as resolve_units:
            timeout = self._mcp_tool_timeout_seconds(
                "sim",
                {
                    "target": "a,b",
                    "mode": "elab_only_standalone",
                    "timeout_ms": 10_000,
                },
                {"default_timeout": 1},
            )
        assert timeout == 2 * 3600 + 10 + 30
        assert resolve_units.call_args.args[-1] is SimulationMode.ELAB_ONLY_STANDALONE

    def test_elab_only_budget_counts_only_target_builds(self):
        timeout = self._mcp_tool_timeout_seconds(
            "sim",
            {"target": "a,b", "mode": "elab_only", "timeout_ms": 10_000},
            {"default_timeout": 1},
        )

        assert timeout == 2 * 3600 + 30

    def test_cycle_count_baseline_uses_same_budget_for_both_revisions(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        state = tmp_path / "state.json"
        state.write_text(
            json.dumps(
                {"criteria": {"cycle_count_core": {"params": {"_baseline_ref": "a" * 40}}}}
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv("BOOLEY_STATE_FILE", str(state))
        with patch(
            "booley.flows.sim.flow._resolve_sim_campaign_work_units",
            return_value=1,
        ):
            timeout = self._mcp_tool_timeout_seconds(
                "sim",
                {"target": "core", "timeout_ms": 10_000},
                {"default_timeout": 1},
            )

        assert timeout == 2 * (3600 + 600 + 10) + 30

    def test_lint_short_timeout_uses_outer_floor(self):
        timeout = self._mcp_tool_timeout_seconds(
            "lint",
            {"timeout_ms": 10_000, "trace": True},
            {"default_timeout": 120},
        )
        assert timeout == 120

    def test_lint_matrix_scales_after_default_floor(self):
        timeout = self._mcp_tool_timeout_seconds(
            "lint",
            {"target": "a,b,c,d,e", "timeout_ms": 120_000},
            {"default_timeout": 600},
        )
        assert timeout == 5 * 120 + 30

    def test_canonical_timeout_wins_for_implementation(self):
        timeout = self._mcp_tool_timeout_seconds(
            "synth",
            {"target": "core", "timeout_ms": 4_000_000},
            {"default_timeout": 600},
        )
        assert timeout == 4000 + 60 + 120

    def test_legacy_timeout_is_rejected_for_mcp(self):
        with pytest.raises(ValueError, match="removed; use timeout_ms"):
            self._mcp_tool_timeout_seconds(
                "lint",
                {"target": "core", "timeout": 2000},
                {"default_timeout": 600},
            )

    def test_synth_matrix_budget_scales_per_target(self):
        timeout = self._mcp_tool_timeout_seconds(
            "synth",
            {
                "target": ",".join(f"asic_{idx}" for idx in range(9)),
                "timeout_ms": 1_800_000,
            },
            {"default_timeout": 7200},
        )
        assert timeout == 9 * 1800 + 9 * 60 + 120

    def test_synth_baseline_budgets_both_passes(self):
        timeout = self._mcp_tool_timeout_seconds(
            "synth",
            {
                "target": "asic_small,asic_full",
                "timeout_ms": 4_000_000,
                "baseline": "main",
            },
            {"default_timeout": 600},
        )
        assert timeout == 4 * 4000 + 4 * 60 + 120

    def test_synth_ticket_baseline_budgets_both_passes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        state = tmp_path / "state.json"
        state.write_text(
            json.dumps(
                {
                    "criteria": {
                        "synthesis_ok_core": {
                            "params": {"_baseline_ref": "a" * 40},
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv("BOOLEY_STATE_FILE", str(state))

        timeout = self._mcp_tool_timeout_seconds(
            "synth",
            {"target": "core", "timeout_ms": 4_000_000},
            {"default_timeout": 600},
        )

        assert timeout == 2 * 4000 + 2 * 60 + 120

    def test_fpga_ticket_baseline_budgets_both_passes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        state = tmp_path / "state.json"
        state.write_text(
            json.dumps(
                {
                    "criteria": {
                        "fpga_impl_ok_core": {
                            "params": {"_baseline_ref": "a" * 40},
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv("BOOLEY_STATE_FILE", str(state))

        timeout = self._mcp_tool_timeout_seconds(
            "fpga",
            {"target": "core", "timeout_ms": 4_000_000},
            {"default_timeout": 7200},
        )

        assert timeout == 2 * 4000 + 2 * 60 + 120

    def test_simulate_no_timeout_arg_honors_config_knob(self, tmp_path: Path):
        """F4: without a call override, the watchdog honors [flows.sim].timeout_ms.

        Otherwise a config-only raise would be silently killed by the outer cap.
        """
        from booley.runtime.project_dir import reset_cache

        reset_cache()
        proj = tmp_path / ".booley_project"
        proj.mkdir()
        (proj / "booley.toml").write_text(
            "[flows.sim]\ntimeout_ms = 1800000\n",
            encoding="utf-8",
        )
        timeout = self._mcp_tool_timeout_seconds(
            "sim",
            {"work_dir": str(tmp_path), "trace": False},
            {"default_timeout": 600},
        )
        assert timeout == 3600 + 600 + 1800 + 30

    def test_lint_no_work_dir_honors_current_workspace_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from booley.runtime.project_dir import reset_cache

        project = tmp_path / ".booley_project"
        project.mkdir()
        (project / "booley.toml").write_text(
            "[flows.lint]\ntimeout_ms = 900000\n",
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)
        reset_cache()

        timeout = self._mcp_tool_timeout_seconds(
            "lint",
            {},
            {"default_timeout": 600},
        )

        assert timeout == 930

    def test_simulate_no_timeout_arg_unconfigured_uses_default(self, tmp_path: Path):
        """No call override or config knob -> the wrapper default budget stands."""
        from booley.runtime.project_dir import reset_cache

        reset_cache()
        # The wrapper default and builtin sim budget are both 600s, then the
        # outer watchdog adds time for report persistence and process cleanup.
        timeout = self._mcp_tool_timeout_seconds(
            "sim",
            {"work_dir": str(tmp_path), "trace": False},
            {"default_timeout": 600},
        )
        assert timeout == 4830

    def test_simulate_custom_build_timeout_is_included_exactly(self, tmp_path: Path):
        from booley.runtime.project_dir import reset_cache

        reset_cache()
        project = tmp_path / ".booley_project"
        project.mkdir()
        (project / "booley.toml").write_text(
            "[flows.sim]\nbuild_timeout_ms = 7000\n",
            encoding="utf-8",
        )

        timeout = self._mcp_tool_timeout_seconds(
            "sim",
            {"work_dir": str(tmp_path), "timeout_ms": 10_000},
            {"default_timeout": 1},
        )

        assert timeout == 7 + 600 + 10 + 30

    def test_coverage_budget_counts_run_and_target_work_exactly(self, tmp_path: Path):
        project = tmp_path / ".booley_project"
        project.mkdir()
        (project / "booley.toml").write_text(
            "[flows.sim]\nbuild_timeout_ms = 7000\n",
            encoding="utf-8",
        )
        with patch(
            "booley.flows.sim.flow._resolve_sim_campaign_work_units",
            return_value=3,
        ):
            timeout = self._mcp_tool_timeout_seconds(
                "sim",
                {
                    "work_dir": str(tmp_path),
                    "target": "a,b",
                    "timeout_ms": 10_000,
                    "coverage": True,
                },
                {"default_timeout": 1},
            )

        assert timeout == 3 * (10 + 600 + 90) + 2 * (7 + 30 + 600) + 30

    @pytest.mark.parametrize("value", [True, 0, -1, 1.5, "7000"])
    def test_invalid_build_timeout_fails_before_spawn_budgeting(
        self,
        tmp_path: Path,
        value: object,
    ):
        project = tmp_path / ".booley_project"
        project.mkdir()
        rendered = "true" if value is True else repr(value)
        (project / "booley.toml").write_text(
            f"[flows.sim]\nbuild_timeout_ms = {rendered}\n",
            encoding="utf-8",
        )

        with pytest.raises(ValueError, match="build_timeout_ms"):
            self._mcp_tool_timeout_seconds(
                "sim",
                {"work_dir": str(tmp_path)},
                {"default_timeout": 1},
            )


# ---------------------------------------------------------------------------
# _try_read_report
# ---------------------------------------------------------------------------


class TestTryReadReport:
    @pytest.fixture(autouse=True)
    def _import(self):
        mcp_stubs = {
            "mcp": MagicMock(),
            "mcp.server": MagicMock(),
            "mcp.server.models": MagicMock(),
            "mcp.server.request_state": MagicMock(),
            "mcp.server.stdio": MagicMock(),
            "mcp.types": MagicMock(),
        }
        with patch.dict(sys.modules, mcp_stubs):
            from booley.mcp import server as mcp_server

            self.mcp_server = mcp_server
            self._try_read_report = mcp_server._try_read_report

    def test_no_env_var(self, monkeypatch):
        monkeypatch.delenv("BOOLEY_LOGS_DIR", raising=False)
        assert self._try_read_report() is None

    def test_nonexistent_dir(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("BOOLEY_LOGS_DIR", str(tmp_path / "nope"))
        assert self._try_read_report() is None

    def test_valid_report(self, tmp_path: Path, monkeypatch):
        import json

        monkeypatch.setenv("BOOLEY_LOGS_DIR", str(tmp_path))
        step_dir = tmp_path / ".runtime" / "flow-reports" / "sim" / "1"
        step_dir.mkdir(parents=True)
        report = {"status": "pass", "summary": "ok"}
        (step_dir / "report.json").write_text(json.dumps(report), encoding="utf-8")
        result = self._try_read_report()
        assert result["status"] == "pass"

    def test_malformed_json(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("BOOLEY_LOGS_DIR", str(tmp_path))
        step_dir = tmp_path / ".runtime" / "flow-reports" / "sim" / "1"
        step_dir.mkdir(parents=True)
        (step_dir / "report.json").write_text("NOT JSON", encoding="utf-8")
        assert self._try_read_report() is None

    def test_non_persisting_dry_run_does_not_attach_stale_report(self, monkeypatch, tmp_path):
        monkeypatch.setenv("BOOLEY_RUNTIME_DIR", str(tmp_path / "runtime"))

        async def fake_run(_cmd, timeout=600, env=None):
            del timeout, env
            return (
                2,
                '{"flow": "lint", "schema_version": 1}',
                "Dry-run planning failed: invalid target",
                False,
            )

        monkeypatch.setattr(self.mcp_server, "_run_subprocess", fake_run)
        monkeypatch.setattr(
            self.mcp_server,
            "_try_read_report",
            lambda: pytest.fail("dry-run must not read a historical verdict"),
        )
        monkeypatch.setattr(
            self.mcp_server,
            "TextContent",
            lambda **kwargs: SimpleNamespace(type=kwargs["type"], text=kwargs["text"]),
        )
        result = asyncio.run(
            self.mcp_server._dispatch_booley_mcp_tool(
                "lint",
                {"dry_run": True, "target": "lint_demo"},
                {
                    "module": "lint",
                    "default_timeout": 600,
                    "non_persisting_dry_run": True,
                    "schema": {
                        "type": "object",
                        "properties": {
                            "dry_run": {"type": "boolean"},
                            "target": {"type": "string"},
                        },
                    },
                },
                {},
                MagicMock(),
            )
        )

        assert isinstance(result, self.mcp_server.McpDispatchResult)
        assert result.is_error is True
        assert '"flow": "lint"' in result.value[0].text
        assert "Dry-run planning failed: invalid target" in result.value[0].text


class TestJobManagerResultText:
    @pytest.fixture(autouse=True)
    def _import(self):
        mcp_stubs = {
            "mcp": MagicMock(),
            "mcp.server": MagicMock(),
            "mcp.server.models": MagicMock(),
            "mcp.server.request_state": MagicMock(),
            "mcp.server.stdio": MagicMock(),
            "mcp.types": MagicMock(),
        }
        with patch.dict(sys.modules, mcp_stubs):
            from booley.mcp import server as mcp_server

            self.mcp_server = mcp_server

    def test_captured_stderr_and_fresh_report_render_one_verdict(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        manager = object.__new__(self.mcp_server._JobManager)
        manager._job_roots = {"run-1": tmp_path}
        manager._results = {"run-1": (1, "", "RESULT: FAIL\n", False)}
        read_roots: list[Path] = []
        monkeypatch.setattr(
            self.mcp_server.jobrec,
            "read_record",
            lambda _run_id, root: read_roots.append(root) or SimpleNamespace(),
        )
        monkeypatch.setattr(
            self.mcp_server,
            "_job_report",
            lambda _record: ({"report_text": "RESULT: FAIL"}, True),
        )

        result = manager.result_text("run-1")

        assert result.count("RESULT: FAIL") == 1
        assert "report_text:" not in result
        assert read_roots == [tmp_path]


# ---------------------------------------------------------------------------
# _resolve_transcript_dir
# ---------------------------------------------------------------------------


class TestResolveTranscriptDir:
    @pytest.fixture(autouse=True)
    def _import(self):
        mcp_stubs = {
            "mcp": MagicMock(),
            "mcp.server": MagicMock(),
            "mcp.server.models": MagicMock(),
            "mcp.server.request_state": MagicMock(),
            "mcp.server.stdio": MagicMock(),
            "mcp.types": MagicMock(),
        }
        with patch.dict(sys.modules, mcp_stubs):
            from collections import defaultdict

            from booley.mcp.server import _resolve_transcript_dir

            self._resolve = _resolve_transcript_dir
            self._new_counts = lambda: defaultdict(int)

    def test_creates_dir_under_logs(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("BOOLEY_LOGS_DIR", str(tmp_path))
        counts = self._new_counts()
        result = self._resolve("tb_coder", counts)
        assert result == tmp_path / ".runtime" / "transcripts" / "tb_coder" / "1"
        assert result.is_dir()

    def test_sequential_numbering(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("BOOLEY_LOGS_DIR", str(tmp_path))
        counts = self._new_counts()
        r1 = self._resolve("tb_coder", counts)
        r2 = self._resolve("tb_coder", counts)
        assert r1.name == "1"
        assert r2.name == "2"

    def test_fallback_when_env_unset(self, monkeypatch):
        """Must still return a valid dir (not None) even without BOOLEY_LOGS_DIR."""
        monkeypatch.delenv("BOOLEY_LOGS_DIR", raising=False)
        counts = self._new_counts()
        result = self._resolve("tb_coder", counts)
        assert result.is_dir()
        assert "transcripts" in str(result)


# ---------------------------------------------------------------------------
# MCP exposure filtering
# ---------------------------------------------------------------------------


class TestMcpExposureFiltering:
    @pytest.fixture(autouse=True)
    def _import(self):
        mcp_stubs = {
            "mcp": MagicMock(),
            "mcp.server": MagicMock(),
            "mcp.server.models": MagicMock(),
            "mcp.server.request_state": MagicMock(),
            "mcp.server.stdio": MagicMock(),
            "mcp.types": MagicMock(),
        }
        with patch.dict(sys.modules, mcp_stubs):
            from booley.mcp.server import (
                _bwave_mcp_tools_for_mode,
                _mcp_tool_visible,
                _status_mcp_tool_visible,
            )

            self._bwave_mcp_tools_for_mode = _bwave_mcp_tools_for_mode
            self._mcp_tool_visible = _mcp_tool_visible
            self._status_mcp_tool_visible = _status_mcp_tool_visible

    def test_default_mode_exposes_autonomous_tools(self, monkeypatch):
        monkeypatch.delenv("BOOLEY_NESTED_AGENT", raising=False)
        monkeypatch.delenv("BOOLEY_NESTED_MCP_TOOLS", raising=False)
        monkeypatch.delenv("BOOLEY_MCP_MODE", raising=False)
        monkeypatch.delenv("BOOLEY_MCP_TOOLS", raising=False)

        assert self._mcp_tool_visible("tb_coder")
        assert self._mcp_tool_visible("reviewer")
        assert self._mcp_tool_visible("sim")
        assert self._mcp_tool_visible("submit_run_report")

    @pytest.mark.parametrize(
        "mcp_tool_name",
        ["tb_coder", "submit_run_report"],
    )
    def test_interactive_mode_hides_autonomous_only_tools(
        self,
        monkeypatch,
        mcp_tool_name,
    ):
        monkeypatch.delenv("BOOLEY_NESTED_AGENT", raising=False)
        monkeypatch.delenv("BOOLEY_MCP_TOOLS", raising=False)
        monkeypatch.setenv("BOOLEY_MCP_MODE", "interactive")

        assert not self._mcp_tool_visible(mcp_tool_name)

    def test_interactive_mode_keeps_interactive_tools(self, monkeypatch):
        monkeypatch.delenv("BOOLEY_NESTED_AGENT", raising=False)
        monkeypatch.delenv("BOOLEY_MCP_TOOLS", raising=False)
        monkeypatch.setenv("BOOLEY_MCP_MODE", "interactive")

        assert self._mcp_tool_visible("sim")
        assert self._mcp_tool_visible("mutation_tester")
        assert self._mcp_tool_visible("reviewer")
        assert self._status_mcp_tool_visible()
        bwave_tools = self._bwave_mcp_tools_for_mode()
        assert {t["name"] for t in bwave_tools} == {"bwave"}
        by_name = {t["name"]: t for t in bwave_tools}
        description = by_name["bwave"]["description"]
        assert "RTL debug helper" in description
        # Investigation contract: trace before hypothesizing, walk backwards from
        # the wrong value, and check that a passing reproducer really triggered.
        assert "rerun `sim` with `trace: true`" in description
        assert "before acting on an RTL hypothesis" in description
        assert "backwards cycle by cycle" in description
        assert "suspected trigger actually occurred" in description
        assert "before editing RTL" in description
        # Detailed syntax and presentation guidance are discovered through these entry points.
        assert 'extra_args=["skill"]' in by_name["bwave"]["description"]
        assert 'extra_args=["--help"]' in by_name["bwave"]["description"]

    def test_explicit_allowlist_overrides_interactive_defaults(self, monkeypatch):
        monkeypatch.delenv("BOOLEY_NESTED_AGENT", raising=False)
        monkeypatch.setenv("BOOLEY_MCP_MODE", "interactive")
        monkeypatch.setenv("BOOLEY_MCP_TOOLS", "reviewer,bwave,sim")

        assert self._mcp_tool_visible("reviewer")
        assert self._mcp_tool_visible("sim")
        assert self._status_mcp_tool_visible()
        assert not self._mcp_tool_visible("lint")
        assert {t["name"] for t in self._bwave_mcp_tools_for_mode()} == {
            "bwave",
        }

    def test_nested_allowlist_takes_precedence(self, monkeypatch):
        monkeypatch.setenv("BOOLEY_NESTED_AGENT", "1")
        monkeypatch.setenv("BOOLEY_NESTED_MCP_TOOLS", "sim")
        monkeypatch.setenv("BOOLEY_MCP_TOOLS", "reviewer")

        assert self._mcp_tool_visible("sim")
        assert not self._mcp_tool_visible("reviewer")
        assert not self._status_mcp_tool_visible()


class TestCoverageEvidenceTool:
    @pytest.fixture(autouse=True)
    def _import(self):
        mcp_stubs = {
            "mcp": MagicMock(),
            "mcp.server": MagicMock(),
            "mcp.server.models": MagicMock(),
            "mcp.server.request_state": MagicMock(),
            "mcp.server.stdio": MagicMock(),
            "mcp.types": MagicMock(),
        }
        with patch.dict(sys.modules, mcp_stubs):
            from booley.mcp import server as mcp_server

            self.mcp_server = mcp_server

    def test_bound_nested_server_exposes_only_coverage_evidence(self, monkeypatch):
        monkeypatch.setenv("BOOLEY_NESTED_AGENT", "1")
        monkeypatch.setenv("BOOLEY_NESTED_MCP_TOOLS", "coverage_evidence")
        monkeypatch.setenv("BOOLEY_COVERAGE_CAMPAIGN", "/reports/coverage.json")
        monkeypatch.setenv("BOOLEY_COVERAGE_PROJECT", "/project")
        monkeypatch.setattr(self.mcp_server, "_bwave_mcp_tools_for_mode", lambda: [])

        tools = self.mcp_server._all_mcp_tool_defs([])

        assert [tool["name"] for tool in tools] == ["coverage_evidence"]
        assert tools[0]["schema"]["additionalProperties"] is False
        assert tools[0]["schema"]["properties"]["point_refs"]["maxItems"] == 10
        assert "point_ids" not in tools[0]["schema"]["properties"]
        assert "point_refs" in tools[0]["description"]
        assert "point_ids" not in tools[0]["description"]

    def test_coverage_evidence_is_hidden_without_bound_campaign(self, monkeypatch):
        monkeypatch.setenv("BOOLEY_NESTED_AGENT", "1")
        monkeypatch.setenv("BOOLEY_NESTED_MCP_TOOLS", "coverage_evidence")
        monkeypatch.delenv("BOOLEY_COVERAGE_CAMPAIGN", raising=False)
        monkeypatch.delenv("BOOLEY_COVERAGE_PROJECT", raising=False)

        assert self.mcp_server._coverage_evidence_tool_def() is None


class TestBooleyStatus:
    @pytest.fixture(autouse=True)
    def _import(self):
        mcp_stubs = {
            "mcp": MagicMock(),
            "mcp.server": MagicMock(),
            "mcp.server.models": MagicMock(),
            "mcp.server.request_state": MagicMock(),
            "mcp.server.stdio": MagicMock(),
            "mcp.types": MagicMock(),
        }
        with patch.dict(sys.modules, mcp_stubs):
            from booley.mcp import server as mcp_server

            self.mcp_server = mcp_server

    def test_format_status_card(self, monkeypatch):
        from booley.harness import auto_doctor

        monkeypatch.setattr(
            self.mcp_server.socket,
            "gethostname",
            lambda: "4f3c2a1b",
        )
        monkeypatch.setattr(auto_doctor, "current_summary", lambda _root: "clean")
        monkeypatch.setattr(
            self.mcp_server,
            "format_status_line",
            lambda: "Booley: 0.1.0 (abc123); last updated yesterday; sandbox image built today.",
        )

        card = self.mcp_server._format_status_card(
            [
                "sim",
                "lint",
            ]
        )

        assert card == (
            "```text\n"
            "Booley ready. Sandbox container 4f3c2a1b is running.\n"
            "Booley: 0.1.0 (abc123); last updated yesterday; sandbox image built today.\n"
            "Available MCP tools: sim, lint.\n"
            "Health: clean\n"
            "```"
        )

    def test_status_mcp_tool_def_only_interactive_by_default(self, monkeypatch):
        monkeypatch.delenv("BOOLEY_NESTED_AGENT", raising=False)
        monkeypatch.delenv("BOOLEY_MCP_TOOLS", raising=False)
        monkeypatch.delenv("BOOLEY_MCP_MODE", raising=False)

        assert self.mcp_server._status_mcp_tool_def() is None

        monkeypatch.setenv("BOOLEY_MCP_MODE", "interactive")
        mcp_tool_def = self.mcp_server._status_mcp_tool_def()

        assert mcp_tool_def is not None
        assert mcp_tool_def["name"] == "booley_status"
        assert mcp_tool_def["schema"]["additionalProperties"] is False

    def test_status_mcp_tool_list_entry_is_appended(self, monkeypatch):
        monkeypatch.delenv("BOOLEY_NESTED_AGENT", raising=False)
        monkeypatch.delenv("BOOLEY_MCP_TOOLS", raising=False)
        monkeypatch.setenv("BOOLEY_MCP_MODE", "interactive")
        monkeypatch.setattr(self.mcp_server, "_bwave_mcp_tools_for_mode", lambda: [])

        mcp_tools = self.mcp_server._all_mcp_tool_defs(
            [
                {
                    "name": "sim",
                    "description": "Run simulation",
                    "schema": {"type": "object"},
                },
            ]
        )

        assert [mcp_tool["name"] for mcp_tool in mcp_tools] == [
            "booley_cancel",
            "booley_poll",
            "booley_report",
            "booley_status",
            "booley_targets",
            "sim",
        ]

    def test_dispatch_status_returns_text_content(self, monkeypatch):
        from booley.harness import auto_doctor

        def fake_text_content(**kwargs):
            return SimpleNamespace(type=kwargs["type"], text=kwargs["text"])

        monkeypatch.setattr(
            self.mcp_server.socket,
            "gethostname",
            lambda: "4f3c2a1b",
        )
        monkeypatch.setattr(
            self.mcp_server,
            "TextContent",
            fake_text_content,
        )
        monkeypatch.setattr(auto_doctor, "current_summary", lambda _root: "1 WARN")
        monkeypatch.setattr(
            self.mcp_server,
            "format_status_line",
            lambda: "Booley: 0.1.0; last updated unknown; sandbox image built unknown.",
        )

        result = self.mcp_server._dispatch_status(["sim"])

        assert result[0].type == "text"
        assert result[0].text == (
            "```text\n"
            "Booley ready. Sandbox container 4f3c2a1b is running.\n"
            "Booley: 0.1.0; last updated unknown; sandbox image built unknown.\n"
            "Available MCP tools: sim.\n"
            "Health: 1 WARN\n"
            "```"
        )

    def test_first_mcp_tool_result_gets_changed_health_warning(self, monkeypatch):
        from booley.harness import auto_doctor

        def fake_text_content(**kwargs):
            return SimpleNamespace(type=kwargs["type"], text=kwargs["text"])

        monkeypatch.setattr(self.mcp_server, "_status_mcp_tool_visible", lambda: True)
        monkeypatch.setattr(self.mcp_server, "TextContent", fake_text_content)
        monkeypatch.setattr(
            auto_doctor,
            "consume_changed_summary",
            lambda *_a, **_kw: "Automatic Doctor found 1 FAIL",
        )
        content = [fake_text_content(type="text", text="MCP tool result")]

        result = self.mcp_server._prepend_changed_health_alert(content)

        assert result[0].text.startswith("HEALTH WARNING:")
        assert result[1].text == "MCP tool result"

    def test_health_warning_preserves_error_disposition(self, monkeypatch):
        from booley.harness import auto_doctor

        def fake_text_content(**kwargs):
            return SimpleNamespace(type=kwargs["type"], text=kwargs["text"])

        monkeypatch.setattr(self.mcp_server, "_status_mcp_tool_visible", lambda: True)
        monkeypatch.setattr(self.mcp_server, "TextContent", fake_text_content)
        monkeypatch.setattr(
            auto_doctor,
            "consume_changed_summary",
            lambda *_a, **_kw: "Automatic Doctor found 1 FAIL",
        )
        content = self.mcp_server.McpDispatchResult(
            value=[fake_text_content(type="text", text="EXIT_CODE: 2")],
            is_error=True,
        )

        result = self.mcp_server._prepend_changed_health_alert(content)

        assert result.is_error is True
        assert result.value[0].text.startswith("HEALTH WARNING:")
        assert result.value[1].text == "EXIT_CODE: 2"

    def test_health_warning_rejects_invalid_content_shape(self):
        block = SimpleNamespace(type="text", text="HEALTH WARNING")

        with pytest.raises(TypeError, match="content that is not a list"):
            self.mcp_server._prepend_health_block(object(), block)


class TestBooleySleep:
    @pytest.fixture(autouse=True)
    def _import(self):
        mcp_stubs = {
            "mcp": MagicMock(),
            "mcp.server": MagicMock(),
            "mcp.server.models": MagicMock(),
            "mcp.server.request_state": MagicMock(),
            "mcp.server.stdio": MagicMock(),
            "mcp.types": MagicMock(),
        }
        with patch.dict(sys.modules, mcp_stubs):
            from booley.mcp import server as mcp_server

            self.mcp_server = mcp_server

    def test_hidden_by_default(self, monkeypatch):
        monkeypatch.delenv("BOOLEY_MCP_DEBUG_TOOLS", raising=False)

        assert self.mcp_server._sleep_mcp_tool_def() is None

    def test_falsey_flag_values_stay_hidden(self, monkeypatch):
        for raw in ("", "0", "false", "no", " FALSE "):
            monkeypatch.setenv("BOOLEY_MCP_DEBUG_TOOLS", raw)
            assert self.mcp_server._sleep_mcp_tool_def() is None

    def test_visible_when_flag_set(self, monkeypatch):
        monkeypatch.setenv("BOOLEY_MCP_DEBUG_TOOLS", "1")

        mcp_tool_def = self.mcp_server._sleep_mcp_tool_def()

        assert mcp_tool_def is not None
        assert mcp_tool_def["name"] == "booley_sleep"
        assert mcp_tool_def["schema"]["required"] == ["seconds"]
        assert mcp_tool_def["schema"]["additionalProperties"] is False

    def test_allowlists_do_not_filter_it(self, monkeypatch):
        monkeypatch.setenv("BOOLEY_MCP_DEBUG_TOOLS", "1")
        monkeypatch.setenv("BOOLEY_MCP_TOOLS", "lint")

        assert self.mcp_server._sleep_mcp_tool_visible() is True

    def test_list_entry_appended_when_enabled(self, monkeypatch):
        monkeypatch.delenv("BOOLEY_NESTED_AGENT", raising=False)
        monkeypatch.delenv("BOOLEY_MCP_TOOLS", raising=False)
        monkeypatch.setenv("BOOLEY_MCP_MODE", "interactive")
        monkeypatch.setenv("BOOLEY_MCP_DEBUG_TOOLS", "1")
        monkeypatch.setattr(self.mcp_server, "_bwave_mcp_tools_for_mode", lambda: [])

        mcp_tools = self.mcp_server._all_mcp_tool_defs(
            [
                {
                    "name": "sim",
                    "description": "Run simulation",
                    "schema": {"type": "object"},
                },
            ]
        )

        assert [mcp_tool["name"] for mcp_tool in mcp_tools] == [
            "booley_cancel",
            "booley_poll",
            "booley_report",
            "booley_sleep",
            "booley_status",
            "booley_targets",
            "sim",
        ]

    def test_dispatch_rejects_bad_seconds(self, monkeypatch):
        def fake_text_content(**kwargs):
            return SimpleNamespace(type=kwargs["type"], text=kwargs["text"])

        monkeypatch.setattr(self.mcp_server, "TextContent", fake_text_content)

        for arguments in ({}, {"seconds": True}, {"seconds": -1}, {"seconds": "5"}):
            result = asyncio.run(self.mcp_server._dispatch_sleep(arguments))
            assert "non-negative number" in result[0].text

    def test_dispatch_sleeps_and_reports(self, monkeypatch):
        def fake_text_content(**kwargs):
            return SimpleNamespace(type=kwargs["type"], text=kwargs["text"])

        monkeypatch.setattr(self.mcp_server, "TextContent", fake_text_content)

        result = asyncio.run(self.mcp_server._dispatch_sleep({"seconds": 0}))

        assert result[0].type == "text"
        assert result[0].text.startswith("SLEEP_COMPLETE: requested=0.0s")


class TestBooleyTargets:
    @pytest.fixture(autouse=True)
    def _import(self):
        mcp_stubs = {
            "mcp": MagicMock(),
            "mcp.server": MagicMock(),
            "mcp.server.models": MagicMock(),
            "mcp.server.request_state": MagicMock(),
            "mcp.server.stdio": MagicMock(),
            "mcp.types": MagicMock(),
        }
        with patch.dict(sys.modules, mcp_stubs):
            from booley.mcp import server as mcp_server

            self.mcp_server = mcp_server

    @staticmethod
    def _project(tmp_path: Path) -> Path:
        (tmp_path / "alpha.core").write_text(
            "CAPI=2:\n"
            "name: acme:ip:alpha:1.0\n"
            "filesets:\n"
            "  rtl:\n"
            "    files:\n"
            "      - rtl/alpha.sv: {file_type: systemVerilogSource}\n"
            "targets:\n"
            "  default:\n"
            "    filesets: [rtl]\n"
            "  sim:\n"
            "    flow: sim\n"
            "    flow_options: {tool: verilator}\n"
            "    filesets: [rtl]\n"
            "  synth:\n"
            "    flow: generic\n"
            "    flow_options: {tool: yosys, arch: xilinx}\n"
            "    filesets: [rtl]\n"
            "  fpga_core:\n"
            "    flow: generic\n"
            "    flow_options: {tool: verilator, part: xc7a35tcpg236-1}\n"
            "    filesets: [rtl]\n",
            encoding="utf-8",
        )
        return tmp_path

    def _patch_text_content(self, monkeypatch):
        monkeypatch.setattr(
            self.mcp_server,
            "TextContent",
            lambda **kwargs: SimpleNamespace(type=kwargs["type"], text=kwargs["text"]),
        )

    def test_visible_in_every_mode(self, monkeypatch):
        monkeypatch.setenv("BOOLEY_NESTED_AGENT", "1")
        monkeypatch.setenv("BOOLEY_NESTED_MCP_TOOLS", "lint")
        assert self.mcp_server._targets_mcp_tool_visible() is True

        mcp_tool_def = self.mcp_server._targets_mcp_tool_def()
        assert mcp_tool_def is not None
        assert mcp_tool_def["name"] == "booley_targets"
        assert mcp_tool_def["schema"]["additionalProperties"] is False

    def test_dispatch_returns_json_surface(self, tmp_path, monkeypatch):
        import json

        self._patch_text_content(monkeypatch)
        monkeypatch.chdir(self._project(tmp_path))

        result = self.mcp_server._dispatch_targets({})

        payload = json.loads(result[0].text)
        names = [t["name"] for c in payload["cores"] for t in c["targets"]]
        assert sorted(names) == ["fpga_core", "sim", "synth"]

    def test_dispatch_applies_filters(self, tmp_path, monkeypatch):
        import json

        self._patch_text_content(monkeypatch)
        monkeypatch.chdir(self._project(tmp_path))

        result = self.mcp_server._dispatch_targets({"for_flow": "synth"})

        payload = json.loads(result[0].text)
        names = [t["name"] for c in payload["cores"] for t in c["targets"]]
        assert names == ["synth"]

    def test_dispatch_fpga_filter_uses_target_axis(self, tmp_path, monkeypatch):
        import json

        self._patch_text_content(monkeypatch)
        monkeypatch.chdir(self._project(tmp_path))

        result = self.mcp_server._dispatch_targets({"for_flow": "fpga"})

        payload = json.loads(result[0].text)
        targets = [target for core in payload["cores"] for target in core["targets"]]
        assert [target["name"] for target in targets] == ["fpga_core"]
        assert targets[0]["eda_tool"] == "verilator"
        assert targets[0]["drivable_by"] == ["fpga"]

    def test_dispatch_rejects_bad_for_flow(self, tmp_path, monkeypatch):
        self._patch_text_content(monkeypatch)
        monkeypatch.chdir(self._project(tmp_path))

        result = self.mcp_server._dispatch_targets({"for_flow": "reviewer"})

        assert result[0].text.startswith("ERROR:")
        assert "not a target-aware Booley Flow" in result[0].text

    def test_dispatch_validates_work_dir(self, tmp_path, monkeypatch):
        self._patch_text_content(monkeypatch)

        result = self.mcp_server._dispatch_targets({"work_dir": str(tmp_path / "not-a-worktree")})

        assert result[0].text.startswith("ERROR: work_dir")


class TestBwaveDispatch:
    @pytest.fixture(autouse=True)
    def _import(self):
        mcp_stubs = {
            "mcp": MagicMock(),
            "mcp.server": MagicMock(),
            "mcp.server.models": MagicMock(),
            "mcp.server.request_state": MagicMock(),
            "mcp.server.stdio": MagicMock(),
            "mcp.types": MagicMock(),
        }
        with patch.dict(sys.modules, mcp_stubs):
            from booley.mcp import server as mcp_server

            self.mcp_server = mcp_server
            self._dispatch_bwave = mcp_server._dispatch_bwave

    def _patch_dispatch(self, monkeypatch):
        calls = []

        async def fake_run(cmd, timeout=600):
            calls.append(cmd)
            return 0, "ok", "", False

        def fake_text_content(**kwargs):
            return SimpleNamespace(type=kwargs["type"], text=kwargs["text"])

        monkeypatch.setattr(self.mcp_server, "_run_subprocess", fake_run)
        monkeypatch.setattr(
            self.mcp_server,
            "TextContent",
            fake_text_content,
        )
        return calls

    def test_register_subcommand_shape(self, monkeypatch):
        calls = self._patch_dispatch(monkeypatch)
        result = asyncio.run(
            self._dispatch_bwave(
                "bwave",
                {"extra_args": ["register", "sim/work", "--as", "dut"]},
            )
        )

        assert result and "EXIT_CODE: 0" in result[0].text
        assert calls == [
            [
                sys.executable,
                "-m",
                "booley.bwave.cli",
                "register",
                "sim/work",
                "--as",
                "dut",
            ]
        ]

    def test_bwave_command_shape(self, monkeypatch):
        calls = self._patch_dispatch(monkeypatch)
        asyncio.run(
            self._dispatch_bwave(
                "bwave",
                {"extra_args": ["@dut", "wave", "-t", "start:done"]},
            )
        )

        assert calls == [
            [
                sys.executable,
                "-m",
                "booley.bwave.cli",
                "@dut",
                "wave",
                "-t",
                "start:done",
            ]
        ]

    def test_bwave_query_emits_console_activity(self, tmp_path, monkeypatch):
        self._patch_dispatch(monkeypatch)
        monkeypatch.setenv("BOOLEY_RUNTIME_DIR", str(tmp_path))

        asyncio.run(
            self._dispatch_bwave(
                "bwave",
                {"extra_args": ["@dut", "wave", "-t", "start:done"]},
            )
        )

        events = [
            json.loads(line)
            for line in (tmp_path / "display.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        assert [event["type"] for event in events] == ["endpoint_start", "endpoint_end"]
        assert [event["endpoint"] for event in events] == ["bwave", "bwave"]
        assert [event["display_label"] for event in events] == [
            "@dut wave -t start:done",
            "@dut wave -t start:done",
        ]
        assert events[1]["exit_code"] == 0
        assert events[1]["duration_s"] >= 0

    def test_bwave_help_command_shape(self, monkeypatch):
        calls = self._patch_dispatch(monkeypatch)
        asyncio.run(
            self._dispatch_bwave(
                "bwave",
                {"extra_args": ["--help"]},
            )
        )

        assert calls == [
            [
                sys.executable,
                "-m",
                "booley.bwave.cli",
                "--help",
            ]
        ]

    def test_bwave_skill_command_shape(self, monkeypatch):
        calls = self._patch_dispatch(monkeypatch)
        asyncio.run(
            self._dispatch_bwave(
                "bwave",
                {"extra_args": ["skill"]},
            )
        )

        assert calls == [
            [
                sys.executable,
                "-m",
                "booley.bwave.cli",
                "skill",
            ]
        ]

    def test_bwave_nonzero_exit_and_stderr_are_preserved(self, tmp_path, monkeypatch):
        async def fake_run(cmd, timeout=600):
            return 9, "", "bad virtual signal", False

        def fake_text_content(**kwargs):
            return SimpleNamespace(type=kwargs["type"], text=kwargs["text"])

        monkeypatch.setattr(self.mcp_server, "_run_subprocess", fake_run)
        monkeypatch.setattr(
            self.mcp_server,
            "TextContent",
            fake_text_content,
        )
        monkeypatch.setenv("BOOLEY_RUNTIME_DIR", str(tmp_path))

        result = asyncio.run(
            self._dispatch_bwave(
                "bwave",
                {"extra_args": ["@dut", "wave", "--virtual", "bad = *nope["]},
            )
        )

        assert result is not None
        assert "EXIT_CODE: 9" in result[0].text
        assert "bad virtual signal" in result[0].text
        events = [
            json.loads(line)
            for line in (tmp_path / "display.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        assert events[-1]["type"] == "endpoint_end"
        assert events[-1]["exit_code"] == 9

    def test_bwave_spawn_error_still_closes_console_activity(self, tmp_path, monkeypatch):
        async def fail_to_spawn(cmd, timeout=600):
            raise OSError("spawn failed")

        monkeypatch.setattr(self.mcp_server, "_run_subprocess", fail_to_spawn)
        monkeypatch.setenv("BOOLEY_RUNTIME_DIR", str(tmp_path))

        with pytest.raises(OSError, match="spawn failed"):
            asyncio.run(
                self._dispatch_bwave(
                    "bwave",
                    {"extra_args": ["@dut", "stats", "clk"]},
                )
            )

        events = [
            json.loads(line)
            for line in (tmp_path / "display.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        assert [event["type"] for event in events] == ["endpoint_start", "endpoint_end"]
        assert events[-1]["exit_code"] == 2

    def test_markers_subcommand_shape(self, monkeypatch):
        calls = self._patch_dispatch(monkeypatch)
        asyncio.run(
            self._dispatch_bwave(
                "bwave",
                {"extra_args": ["markers", "@dut", "set", "start", "10"]},
            )
        )

        assert calls == [
            [
                sys.executable,
                "-m",
                "booley.bwave.cli",
                "markers",
                "@dut",
                "set",
                "start",
                "10",
            ]
        ]

    def test_unknown_bwave_name_returns_none(self, monkeypatch):
        calls = self._patch_dispatch(monkeypatch)

        assert asyncio.run(self._dispatch_bwave("bwave_nope", {})) is None
        assert calls == []

    def test_hidden_bwave_mcp_tool_does_not_dispatch(self, monkeypatch):
        calls = self._patch_dispatch(monkeypatch)
        monkeypatch.setenv("BOOLEY_MCP_TOOLS", "sim")

        assert (
            asyncio.run(
                self._dispatch_bwave(
                    "bwave",
                    {"extra_args": ["markers", "@dut", "list"]},
                )
            )
            is None
        )
        assert calls == []


# ---------------------------------------------------------------------------
# _load_agent_settings_from_toml (Interactive Mode honors [agent] in booley.toml)
# ---------------------------------------------------------------------------


class TestLoadAgentSettingsFromToml:
    """Interactive Mode must preload agent settings from booley.toml.

    Regression guard: without this, Runtime may lazily use defaults even when
    the Project selected another provider.
    """

    @pytest.fixture(autouse=True)
    def _import(self):
        mcp_stubs = {
            "mcp": MagicMock(),
            "mcp.server": MagicMock(),
            "mcp.server.models": MagicMock(),
            "mcp.server.request_state": MagicMock(),
            "mcp.server.stdio": MagicMock(),
            "mcp.types": MagicMock(),
        }
        with patch.dict(sys.modules, mcp_stubs):
            from booley.mcp.server import _load_agent_settings_from_toml
            from booley.runtime.agent_config import get_backend_config, set_backend_config

            self._load = _load_agent_settings_from_toml
            self._get = get_backend_config
            self._set = set_backend_config
            # Start from a clean global so we exercise the real load path.
            self._set(None)
            try:
                yield
            finally:
                self._set(None)

    def _write_project(self, tmp_path: Path, body: str) -> Path:
        bp = tmp_path / ".booley_project"
        bp.mkdir()
        (bp / "booley.toml").write_text(body)
        return tmp_path

    def test_claude_provider_from_project_dir(self, tmp_path, monkeypatch):
        # BOOLEY_PROJECT_DIR points at the .booley_project dir; the loader must
        # resolve the *parent* as the repo root.
        root = self._write_project(
            tmp_path,
            '[agent]\nprovider = "claude"\n',
        )
        monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(root / ".booley_project"))

        self._load()

        assert self._get().settings.provider == "claude"

    def test_falls_back_to_cwd_when_env_unset(self, tmp_path, monkeypatch):
        root = self._write_project(
            tmp_path,
            '[agent]\nprovider = "claude"\n',
        )
        monkeypatch.delenv("BOOLEY_PROJECT_DIR", raising=False)
        monkeypatch.chdir(root)

        self._load()

        assert self._get().settings.provider == "claude"

    def test_missing_toml_does_not_raise(self, tmp_path, monkeypatch):
        # No .booley_project/booley.toml at all — must not crash server startup.
        monkeypatch.delenv("BOOLEY_PROJECT_DIR", raising=False)
        monkeypatch.chdir(tmp_path)

        self._load()  # should be a no-op-ish, not an exception


# ---------------------------------------------------------------------------
# _discover_builtin_mcp_tools AST gate
# ---------------------------------------------------------------------------


class TestBuiltinDiscoveryGate:
    """The registry gives the importer only AST-validated MCP endpoints."""

    @pytest.fixture(autouse=True)
    def _import(self, monkeypatch):
        mcp_stubs = {
            "mcp": MagicMock(),
            "mcp.server": MagicMock(),
            "mcp.server.models": MagicMock(),
            "mcp.server.request_state": MagicMock(),
            "mcp.server.stdio": MagicMock(),
            "mcp.types": MagicMock(),
        }
        with patch.dict(sys.modules, mcp_stubs):
            from booley.mcp.server import _discover_builtin_mcp_tools

            self._discover = _discover_builtin_mcp_tools
        # MCP visibility filters must not leak in from the invoking shell.
        for var in ("BOOLEY_NESTED_AGENT", "BOOLEY_MCP_TOOLS", "BOOLEY_MCP_MODE"):
            monkeypatch.delenv(var, raising=False)

    def test_helper_module_skipped_without_import(self, tmp_path, monkeypatch):
        # The registry excludes support modules before the import stage.
        (tmp_path / "some_helper.py").write_text(
            'raise RuntimeError("helper must never be imported by discovery")\n',
            encoding="utf-8",
        )

        results, errors = self._discover([])

        assert results == []
        assert errors == []

    def test_mcp_endpoint_reaches_import_stage(self):
        from booley.mcp.registry import McpToolInfo

        info = McpToolInfo(
            name="shiny_mcp_tool",
            description="an MCP endpoint",
            path="mcp_tools/shiny_mcp_tool.py",
        )
        results, errors = self._discover([info])

        assert results == []
        assert len(errors) == 1
        assert "IMPORT FAILED" in errors[0]

    def test_real_endpoint_dirs_discover_cleanly(self):
        # Golden: over the real Flow/MCP packages the import scan must agree
        # with the AST scan and produce zero errors — no current or future
        # helper module may surface as NO MCP ENDPOINT CLASS FOUND.
        from booley.mcp.registry import discover_mcp_tools

        discovered = discover_mcp_tools()

        results, errors = self._discover(discovered)

        assert errors == []
        assert {r["name"] for r in results} == {endpoint.name for endpoint in discovered}


# ---------------------------------------------------------------------------
# main() — proxy env self-heal
# ---------------------------------------------------------------------------


class TestMainProxySelfHeal:
    """Codex replaces the MCP child env per config.toml [env]; a config that
    drops the proxy vars leaves the server with no egress. main() must
    self-heal via venue.ensure_proxy_env() so the whole MCP tool subtree under
    the server inherits a working proxy path."""

    @pytest.fixture(autouse=True)
    def _import(self, monkeypatch):
        mcp_stubs = {
            "mcp": MagicMock(),
            "mcp.server": MagicMock(),
            "mcp.server.models": MagicMock(),
            "mcp.server.request_state": MagicMock(),
            "mcp.server.stdio": MagicMock(),
            "mcp.types": MagicMock(),
        }
        with patch.dict(sys.modules, mcp_stubs):
            from booley.mcp import server as mcp_server

            self.mcp_server = mcp_server
        # Sandbox venue with a clean (Codex-replaced) env: marker present,
        # no proxy vars delivered.
        monkeypatch.setenv("BOOLEY_CONTAINER", "1")
        for var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "NO_PROXY"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.delenv("BOOLEY_MCP_TRANSPORT", raising=False)

    def test_main_defaults_proxy_env_in_sandbox(self, monkeypatch):
        import os

        from booley.runtime import runtime_context

        # Neuter the server itself — only the entry path is under test.
        monkeypatch.setattr(self.mcp_server, "_main", MagicMock())
        monkeypatch.setattr(self.mcp_server, "asyncio", MagicMock())
        monkeypatch.setattr(sys, "argv", ["booley-mcp"])

        self.mcp_server.main()

        for var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            assert os.environ[var] == runtime_context._PROXY_URL
        assert os.environ["NO_PROXY"] == "localhost,127.0.0.1"

    def test_main_respects_delivered_proxy_env(self, monkeypatch):
        """A config that DOES forward the proxy vars must win untouched."""
        import os

        monkeypatch.setenv("HTTPS_PROXY", "http://corp-proxy:3128")
        monkeypatch.setattr(self.mcp_server, "_main", MagicMock())
        monkeypatch.setattr(self.mcp_server, "asyncio", MagicMock())
        monkeypatch.setattr(sys, "argv", ["booley-mcp"])

        self.mcp_server.main()

        assert os.environ["HTTPS_PROXY"] == "http://corp-proxy:3128"
        assert "HTTP_PROXY" not in os.environ


# ---------------------------------------------------------------------------
# Interactive Mode: honest answer for a deliberately hidden MCP tool (F-38)
# ---------------------------------------------------------------------------


class TestInteractiveHiddenNote:
    """An interactive tab explains autonomous-only MCP tools hidden from MCP tools/list."""

    @pytest.fixture(autouse=True)
    def _import(self):
        mcp_stubs = {
            "mcp": MagicMock(),
            "mcp.server": MagicMock(),
            "mcp.server.models": MagicMock(),
            "mcp.server.request_state": MagicMock(),
            "mcp.server.stdio": MagicMock(),
            "mcp.types": MagicMock(),
        }
        with patch.dict(sys.modules, mcp_stubs):
            from booley.mcp.server import (
                _INTERACTIVE_HIDDEN_REASONS,
                _INTERACTIVE_MCP_EXCLUDED,
                _interactive_hidden_note,
            )

            self._note = _interactive_hidden_note
            self._reasons = _INTERACTIVE_HIDDEN_REASONS
            self._excluded = _INTERACTIVE_MCP_EXCLUDED

    def test_every_hidden_mcp_tool_has_a_reason(self):
        # The set is derived from the reasons — they cannot drift apart.
        assert self._excluded == frozenset(self._reasons)

    @pytest.mark.parametrize("mcp_tool_name", ["submit_run_report", "tb_coder"])
    def test_hidden_mcp_tool_explains_itself(self, monkeypatch, mcp_tool_name):
        monkeypatch.setenv("BOOLEY_MCP_MODE", "interactive")

        note = self._note(mcp_tool_name)

        assert note is not None
        assert "hidden in Interactive Mode" in note
        assert "Ticket Mode" in note  # where it does run

    def test_genuinely_unknown_mcp_tool_gets_no_excuse(self, monkeypatch):
        monkeypatch.setenv("BOOLEY_MCP_MODE", "interactive")

        assert self._note("no_such_mcp_tool") is None

    def test_autonomous_mode_has_nothing_to_explain(self, monkeypatch):
        monkeypatch.delenv("BOOLEY_MCP_MODE", raising=False)

        assert self._note("submit_run_report") is None


def test_report_timeout_rejects_previous_completed_attempt(tmp_path, monkeypatch):
    from booley.mcp.server import _committed_submission_report
    from booley.ticket_board.report_submission import Submission

    old = Submission(tmp_path, "a" * 32, {}, "execution")
    old.stage(b"report")
    old.commit()
    old.close()
    monkeypatch.setenv("BOOLEY_LOGS_DIR", str(tmp_path))
    report, diagnosis = _committed_submission_report({"criterion_met": True}, "b" * 32, 124)
    assert report is None
    assert "before commit" in diagnosis


@pytest.mark.parametrize("exit_code", [0, 124, 2])
def test_matching_completed_receipt_preserves_process_outcome(tmp_path, monkeypatch, exit_code):
    from booley.mcp.server import _committed_submission_report
    from booley.ticket_board.report_submission import Submission

    attempt = Submission(tmp_path, "a" * 32, {}, "execution")
    attempt.stage(b"report")
    attempt.commit()
    attempt.close()
    from booley.criteria.state import DevelopmentState
    from booley.ticket_board.report_submission import DIGEST_KEY, ID_KEY, KEY

    state = DevelopmentState.load(tmp_path / ".runtime/booley_state.json")
    state.init_criteria({KEY: True})
    state.set_criterion(
        KEY, True, detail={ID_KEY: "a" * 32, DIGEST_KEY: attempt.row["report_sha256"]}
    )
    state.save()
    monkeypatch.setenv("BOOLEY_LOGS_DIR", str(tmp_path))
    report, diagnosis = _committed_submission_report({}, "a" * 32, exit_code)
    assert report["criterion_met"] is True
    assert "again" not in diagnosis
    assert bool(diagnosis) is bool(exit_code)


def test_committed_process_result_preserves_latest_unmet_report(tmp_path, monkeypatch):
    from booley.criteria.state import DevelopmentState
    from booley.mcp.server import _committed_submission_report
    from booley.ticket_board.acceptance_ledger import record_changes
    from booley.ticket_board.report_submission import DIGEST_KEY, ID_KEY, KEY, Submission

    state = DevelopmentState.load(tmp_path / ".runtime/booley_state.json")
    state.init_criteria({KEY: True})
    attempt = Submission(tmp_path, "a" * 32, {}, "execution")
    attempt.stage(b"report")
    changes = state.set_criterion(
        KEY, True, detail={ID_KEY: "a" * 32, DIGEST_KEY: attempt.row["report_sha256"]}
    )
    record_changes(
        tmp_path,
        state,
        changes,
        invocation_id="one",
        producer="report",
        execution_id="execution",
        ticket_identity={},
    )
    state.save()
    attempt.commit()
    attempt.close()
    changes = state.set_criterion(KEY, False, detail={"reason": "later invalidation"})
    record_changes(
        tmp_path,
        state,
        changes,
        invocation_id="two",
        producer="review",
        execution_id="execution",
        ticket_identity={},
    )
    monkeypatch.setenv("BOOLEY_LOGS_DIR", str(tmp_path))
    report, diagnosis = _committed_submission_report({}, "a" * 32, 0)
    assert report["criterion_met"] is False
    assert report["detail"]["report_submission_status"] == "completed"
    assert "committed" in diagnosis.lower()
    assert "invalidated" in diagnosis


def test_simulation_count_transport_omits_only_full_mapping():
    from copy import deepcopy

    from booley.mcp.server import _structured_from_report

    report = {
        "$schema": "booley.simulation-report/v3",
        "flow": "sim",
        "passed": True,
        "detail": {
            "campaigns": {
                "sim": {
                    "observations": [{"test": "one", "cycle_count": 7}],
                    "artifacts": {"simulation": {"path": "targets/sim/simulation.json"}},
                }
            }
        },
        "cycle_counts": {
            "sim": [{"test": "x" * 512, "cycle_count": index} for index in range(4000)]
        },
    }
    before = deepcopy(report)
    result = _structured_from_report(report)
    assert report == before
    expected = {key: value for key, value in report.items() if key != "cycle_counts"}
    assert result["reports"] == [expected]
    report["flow"] = "synth"
    assert _structured_from_report(report)["truncated"] is True


@pytest.mark.parametrize("completion_error", [False, True])
def test_cycle_card_fits_exact_stream_boundary_until_completion_append(
    monkeypatch, completion_error
):
    from types import SimpleNamespace

    from booley.flows.sim.flow import _campaign_report_lines
    from booley.mcp.server import _format_mcp_tool_result
    from booley.runtime.endpoint_execution import EndpointOutcome, normalize_completion_error

    manifest = Path("/manifest.json")
    pointer_bytes = len(f"  manifest: {manifest.resolve()}\n".encode())
    budget = 4000 + pointer_bytes
    monkeypatch.setenv("BOOLEY_MCP_MAX_STDOUT_BYTES", str(budget))
    monkeypatch.setenv("BOOLEY_MCP_MAX_STDERR_BYTES", str(budget))
    observation = {"test": "smoke", "cycle_count": 7, "detail": {"reason": "x" * 3870}}
    outcome = SimpleNamespace(
        target={"selector": "sim"},
        manifest_path=manifest,
        aggregate_grade="fail",
        observations=[observation],
    )
    card = "\n".join(_campaign_report_lines([outcome]))
    assert len(card.encode()) + 2 <= budget
    result = EndpointOutcome(exit_code=1, report_text=card)
    if completion_error:
        normalize_completion_error(result, OSError("y" * 200), "publish")
    stderr = "earlier output\n" * 300 + "\n" + result.report_text + "\n"
    rendered = _format_mcp_tool_result(
        result.exit_code, "", stderr, {"report_text": result.report_text}
    )
    assert rendered.count("sim: FAIL") == 1
    assert ("--- report ---" in rendered) is completion_error


def test_fetched_simulation_card_never_expands_top_level_counts():
    from booley.mcp.server import _format_report_card

    report = {
        "flow": "sim",
        "detail": {"campaigns": {}},
        "cycle_counts": {"sim": [{"test": "hidden-full-row", "cycle_count": 7}]},
    }
    assert "hidden-full-row" not in _format_report_card(report, "sim")


@pytest.mark.parametrize("response_kind", ["handoff", "attach", "poll", "adopted_poll"])
def test_live_sim_checkpoint_survives_mcp_background_responses(
    tmp_path, monkeypatch, response_kind
):
    """A gated real Job keeps its current checkpoint through every MCP handoff."""
    from booley.mcp import server
    from booley.mcp.call_context import CallContext

    monkeypatch.setenv("BOOLEY_MCP_JOB_INLINE_WAIT_SECONDS", "0")
    monkeypatch.setattr(server, "_bwave_mcp_tools_for_mode", lambda: [])
    monkeypatch.setattr(server, "_prepend_changed_health_alert", lambda result: result)
    context = CallContext(tmp_path, tmp_path / "jobs", None, None, tmp_path, {}, None)
    monkeypatch.setattr(server, "job_roots", lambda: [context.jobs_root])

    asyncio.run(_live_checkpoint_scenario(server, context, tmp_path, monkeypatch, response_kind))


async def _live_checkpoint_scenario(server, context, tmp_path, monkeypatch, response_kind):
    from booley.flows.progress_lifecycle import progress_document, write_progress_json

    entered = asyncio.Event()
    release = asyncio.Event()
    checkpoints = {}

    async def gated_child(_cmd, *, env, on_spawn, **_kwargs):
        on_spawn(os.getpid())
        run_id = env["BOOLEY_RUN_ID"]
        checkpoint = progress_document(
            flow="sim",
            run_id=run_id,
            phase="running",
            targets=["dut"],
            completed_targets=[],
            detail={},
            extra={
                "active": [
                    {
                        "target": "dut",
                        "stage": "executing",
                        "role": "candidate",
                        "run_log": "build/dut/run.log",
                    }
                ]
            },
        )
        write_progress_json(tmp_path / "flow-reports/sim/1/progress.json", checkpoint)
        # A newer sibling invocation cannot substitute its active checkpoint.
        foreign = {**checkpoint, "run_id": "other-invocation", "active": []}
        write_progress_json(tmp_path / "flow-reports/sim/2/progress.json", foreign)
        checkpoints[run_id] = checkpoint
        entered.set()
        await asyncio.wait_for(release.wait(), timeout=5)
        return 0, "", "", False

    monkeypatch.setattr(server, "_run_subprocess", gated_child)
    jobs = server._JobManager(server._McpLifetime(None, None))
    # Yield inside the real inline-wait boundary until the child published.
    original_wait = jobs.wait

    async def inline_wait(run_id, seconds):
        await asyncio.wait_for(entered.wait(), timeout=5)
        return await original_wait(run_id, seconds)

    monkeypatch.setattr(jobs, "wait", inline_wait)
    try:
        handoff = await server._dispatch_async_job("sim", [], 60, jobs, context=context)
        run_id = next(iter(checkpoints))
        assert server._job_record(run_id).status == "running"
        structured = await _live_checkpoint_response(
            server, jobs, context, response_kind, handoff, run_id, monkeypatch
        )
        _assert_live_checkpoint(structured, checkpoints[run_id])
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(*jobs._tasks.values()), timeout=5)


async def _live_checkpoint_response(
    server, jobs, context, response_kind, handoff, run_id, monkeypatch
):
    if response_kind == "handoff":
        assert isinstance(handoff, tuple), "handoff lost structured checkpoint"
        structured = handoff[1]
    elif response_kind == "attach":
        attached = await server._dispatch_async_job("sim", [], 60, jobs, context=context)
        assert len(jobs._tasks) == 1
        assert isinstance(attached, tuple), "idempotent attach lost checkpoint"
        structured = attached[1]
    else:
        selected_jobs = jobs
        if response_kind == "adopted_poll":
            selected_jobs = server._JobManager(server._McpLifetime(None, None))
        monkeypatch.setattr(server, "_JobManager", lambda _lifetime: selected_jobs)
        application = server._build_mcp_application([], [], server._McpLifetime(None, None))
        assert "booley_poll" in [tool.name for tool in application.list_tools()]
        payload = await application.call_tool(
            "booley_poll",
            {
                "run_id": run_id,
                "wait_seconds": 0,
            },
        )
        assert not payload.is_error
        structured = payload.structured_content
        assert "synthesis verdict" not in payload.content[0].text
    return structured


def _assert_live_checkpoint(structured, checkpoint):
    assert structured is not None
    report = structured["reports"][0]
    assert report == {**checkpoint, "partial": True}
    assert report["complete"] is False
    assert report["pending_targets"] == ["dut"]
    assert "passed" not in structured
    assert "exit_code" not in report


def test_registered_poll_observes_blocked_legacy_simulation_execution(tmp_path, monkeypatch):
    """The real Flow and execution publish an owned log before the invoker returns."""
    from concurrent.futures import ThreadPoolExecutor
    from contextlib import ExitStack
    from threading import Event

    from booley.flows.base import SubprocessResult
    from booley.flows.run_log import write_run_log_progress
    from booley.flows.sim.execution import SimulationExecution, SimulationOptions
    from booley.mcp import server
    from booley.runtime.timefmt import utc_now_rfc3339
    from tests.flows.sim.test_execution_engine import (
        _compile_surface_patch,
        _handle,
        _inspection,
        _prepared,
    )
    from tests.flows.sim.test_flow import _make_flow

    run_id = "gated-legacy-sim"
    monkeypatch.setenv("BOOLEY_RUN_ID", run_id)
    flow = _make_flow(tmp_path, config="sim")
    monkeypatch.setattr(
        flow,
        "_flow_plan",
        SimpleNamespace(
            work_units=(SimpleNamespace(selector="sim", role="ordinary", test_or_module_scope=()),)
        ),
        raising=False,
    )
    handle = _handle(tmp_path)
    prepared = _prepared(handle, cocotb=False)
    entered, release = Event(), Event()
    session = MagicMock()
    session.__enter__.return_value = session
    session.new_generation.return_value = prepared.work_root
    session.try_reuse.return_value = None
    session.capture_inputs.return_value = {}

    def invoke(command, *, timeout):
        del timeout
        if "BOOLEY_BUILD_STAGE" in command[-1]:
            return SubprocessResult(returncode=0, stdout="BOOLEY_BUILD_STAGE token=abc123 rc=0\n")
        write_run_log_progress(
            prepared.work_root, "synthetic exception\n" * 4, elapsed_s=1, line_count=4
        )
        entered.set()
        assert release.wait(5), "test failed to release simulator invoker"
        return SubprocessResult(returncode=0)

    execution = SimulationExecution(invoke=invoke, options=SimulationOptions())
    monkeypatch.setattr(flow, "_simulation_execution", lambda **_kwargs: execution)
    monkeypatch.setattr(flow, "_target_handle", lambda _target: handle)
    with ExitStack() as stack:
        stack.callback(flow.context.publication_resources.close)
        stack.enter_context(_compile_surface_patch(handle))
        stack.enter_context(
            patch(
                "booley.flows.sim.execution.engine.TargetCatalog.build",
                return_value=_inspection(cocotb=False),
            )
        )
        stack.enter_context(
            patch(
                "booley.flows.sim.execution.engine.prepare_simulation_build", return_value=prepared
            )
        )
        stack.enter_context(
            patch("booley.flows.sim.execution.engine.SimulationBuildSession", return_value=session)
        )
        stack.enter_context(
            patch("booley.flows.sim.execution.engine.new_attempt_token", return_value="abc123")
        )
        _poll_blocked_legacy_flow(
            server,
            flow,
            tmp_path,
            run_id,
            entered,
            release,
            monkeypatch,
            utc_now_rfc3339,
            ThreadPoolExecutor,
        )


def _poll_blocked_legacy_flow(
    server, flow, root, run_id, entered, release, monkeypatch, now, pool_type
):
    import time

    jobs_root = root / "runtime/jobs"
    server.jobrec.write_record(
        server.jobrec.JobRecord(
            run_id=run_id,
            endpoint="sim",
            started_at=now(),
            timeout_s=60,
            pid=os.getpid(),
            argv=[],
        ),
        root=jobs_root,
    )
    monkeypatch.setattr(server, "job_roots", lambda: [jobs_root])
    monkeypatch.setattr(
        server, "_endpoint_report_dirs", lambda *_args, **_kwargs: (root / "reports",)
    )
    monkeypatch.setattr(server, "_bwave_mcp_tools_for_mode", lambda: [])
    monkeypatch.setattr(server, "_prepend_changed_health_alert", lambda result: result)
    with pool_type(max_workers=1) as pool:
        future = pool.submit(flow._run_legacy_selected_mode, ["sim"], {}, time.monotonic(), 0)
        try:
            assert entered.wait(5), "simulator launch was not reached"
            application = server._build_mcp_application([], [], server._McpLifetime(None, None))
            payload = asyncio.run(
                application.call_tool(
                    "booley_poll",
                    {
                        "run_id": run_id,
                        "wait_seconds": 0,
                    },
                )
            )
            report = payload.structured_content["reports"][0]
            assert report["run_id"] == run_id
            assert report["phase"] == "running"
            assert report["partial"] is True and report["complete"] is False
            assert report["completed_targets"] == []
            assert report["pending_targets"] == ["sim"]
            active = next(item for item in report["active"] if item["stage"] == "executing")
            assert active["stage"] == "executing"
            assert active["log"]["live"] is True
            assert "synthetic exception" in (root / active["log"]["path"]).read_text()
            assert "passed" not in payload.structured_content
        finally:
            release.set()
            future.result(timeout=5)

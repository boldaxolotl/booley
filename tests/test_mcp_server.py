"""Tests for mcp_server: parameter conversion, subprocess dispatch, result formatting."""

from __future__ import annotations

import asyncio
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
            "mcp.server.stdio": MagicMock(),
            "mcp.types": MagicMock(),
        }
        with patch.dict(sys.modules, mcp_stubs):
            from booley.mcp_server import _params_to_argv

            self._params_to_argv = _params_to_argv

    def test_string_param(self):
        result = self._params_to_argv({"target": "sim_a"})
        assert result == ["--target", "sim_a"]

    def test_bool_true(self):
        result = self._params_to_argv({"verbose": True})
        assert result == ["--verbose"]

    def test_bool_false_omitted(self):
        result = self._params_to_argv({"verbose": False})
        assert result == []

    def test_list_param(self):
        result = self._params_to_argv({"defines": ["A", "B"]})
        assert result == ["--defines", "A", "--defines", "B"]

    def test_underscore_to_hyphen(self):
        result = self._params_to_argv({"trace_scope": "tb.dut"})
        assert result == ["--trace-scope", "tb.dut"]

    def test_mixed_params(self):
        result = self._params_to_argv(
            {
                "target": "sim_a",
                "trace": True,
                "debug": False,
                "defines": ["X"],
            }
        )
        assert "--target" in result
        assert "--trace" in result
        assert "--debug" not in result
        assert "--defines" in result

    def test_empty_params(self):
        result = self._params_to_argv({})
        assert result == []

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
            "mcp.server.stdio": MagicMock(),
            "mcp.types": MagicMock(),
        }
        with patch.dict(sys.modules, mcp_stubs):
            from booley.mcp_server import _env_timeout_seconds, _McpLifetime

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
        import booley.mcp_server as mod

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
        import booley.mcp_server as mod

        hb = tmp_path / "hb"
        monkeypatch.setattr(mod, "_MCP_HEARTBEAT_PATH", str(hb))
        monkeypatch.setenv("BOOLEY_MCP_MODE", "interactive")
        lifetime = mod._McpLifetime.from_env(self_exit=False)
        assert lifetime.idle_timeout_seconds is None
        assert lifetime.max_age_seconds is None
        assert hb.exists()  # heartbeat written at construction

    def test_from_env_stdio_keeps_self_exit(self, tmp_path, monkeypatch):
        import booley.mcp_server as mod

        monkeypatch.setattr(mod, "_MCP_HEARTBEAT_PATH", str(tmp_path / "hb"))
        monkeypatch.setenv("BOOLEY_MCP_MODE", "interactive")
        lifetime = mod._McpLifetime.from_env()
        assert lifetime.idle_timeout_seconds is not None
        assert lifetime.max_age_seconds is not None


class TestHttpPort:
    def test_default(self, monkeypatch):
        from booley.mcp_server import DEFAULT_HTTP_PORT, http_port

        monkeypatch.delenv("BOOLEY_MCP_HTTP_PORT", raising=False)
        assert http_port() == DEFAULT_HTTP_PORT

    def test_env_override(self, monkeypatch):
        from booley.mcp_server import http_port

        monkeypatch.setenv("BOOLEY_MCP_HTTP_PORT", "9123")
        assert http_port() == 9123

    def test_invalid_falls_back(self, monkeypatch):
        from booley.mcp_server import DEFAULT_HTTP_PORT, http_port

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
            "mcp.server.stdio": MagicMock(),
            "mcp.types": MagicMock(),
        }
        with patch.dict(sys.modules, mcp_stubs):
            from booley.mcp_server import _format_mcp_tool_result

            self._format_mcp_tool_result = _format_mcp_tool_result

    def test_basic_result(self):
        result = self._format_mcp_tool_result(0, "hello", "")
        assert "EXIT_CODE: 0" in result
        assert "hello" in result

    def test_with_stderr(self):
        result = self._format_mcp_tool_result(1, "", "error occurred")
        assert "EXIT_CODE: 1" in result
        assert "error occurred" in result

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

    def test_truncation_stdout(self):
        long_stdout = "x" * 20000
        result = self._format_mcp_tool_result(0, long_stdout, "")
        assert "truncated" in result

    def test_empty_result(self):
        result = self._format_mcp_tool_result(0, "", "")
        assert "EXIT_CODE: 0" in result


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
            "mcp.server.stdio": MagicMock(),
            "mcp.types": MagicMock(),
        }
        with patch.dict(sys.modules, mcp_stubs):
            from booley.mcp_server import _run_subprocess

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

        from booley.mcp_server import _stdout_cap_bytes

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
            "mcp.server.stdio": MagicMock(),
            "mcp.types": MagicMock(),
        }
        with patch.dict(sys.modules, mcp_stubs):
            from booley.mcp_server import _mcp_tool_timeout_seconds

            self._mcp_tool_timeout_seconds = _mcp_tool_timeout_seconds

    def test_simulate_trace_timeout_gets_cleanup_margin(self):
        timeout = self._mcp_tool_timeout_seconds(
            "sim",
            {"timeout": 10_000, "trace": True},
            {"default_timeout": 600},
        )
        assert timeout == 690

    def test_simulate_non_trace_timeout_gets_small_margin(self):
        timeout = self._mcp_tool_timeout_seconds(
            "sim",
            {"timeout": 10_000, "trace": False},
            {"default_timeout": 600},
        )
        assert timeout == 630

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
        assert timeout == 630

    def test_simulate_campaign_budget_scales_by_work_units(self):
        with patch(
            "booley.flows.sim.flow._resolve_sim_campaign_work_units",
            return_value=4,
        ):
            timeout = self._mcp_tool_timeout_seconds(
                "sim",
                {"target": "a,b", "timeout": 600_000, "trace": False},
                {"default_timeout": 1290},
            )
        assert timeout == 4 * 600 + 30

    def test_simulate_trace_margin_scales_by_work_units(self):
        with patch(
            "booley.flows.sim.flow._resolve_sim_campaign_work_units",
            return_value=3,
        ):
            timeout = self._mcp_tool_timeout_seconds(
                "sim",
                {"target": "a,b,c", "timeout": 600_000, "trace": True},
                {"default_timeout": 1290},
            )
        assert timeout == 3 * 600 + 3 * 90

    def test_non_simulate_uses_default(self):
        timeout = self._mcp_tool_timeout_seconds(
            "lint",
            {"timeout": 10_000, "trace": True},
            {"default_timeout": 120},
        )
        assert timeout == 120

    def test_synth_matrix_budget_scales_per_target(self):
        timeout = self._mcp_tool_timeout_seconds(
            "synth",
            {
                "target": ",".join(f"asic_{idx}" for idx in range(9)),
                "timeout": 1_800_000,
            },
            {"default_timeout": 7200},
        )
        assert timeout == 9 * 1800 + 9 * 60 + 120

    def test_synth_baseline_budgets_both_passes(self):
        timeout = self._mcp_tool_timeout_seconds(
            "synth",
            {
                "target": "asic_small,asic_full",
                "timeout": 4_000_000,
                "baseline": "main",
            },
            {"default_timeout": 600},
        )
        assert timeout == 4 * 4000 + 4 * 60 + 120

    def test_simulate_no_timeout_arg_honors_config_knob(self, tmp_path: Path):
        """F4: with no --timeout arg the watchdog honors [flows.sim].timeout_ms.

        Otherwise a config-only raise would be silently killed by the outer cap.
        """
        from booley.project_dir import reset_cache

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
        # max(default 600, 1800000ms -> 1800s) + report-persistence margin.
        assert timeout == 1830

    def test_simulate_no_timeout_arg_unconfigured_uses_default(self, tmp_path: Path):
        """No --timeout and no config knob -> the wrapper default budget stands."""
        from booley.project_dir import reset_cache

        reset_cache()
        # The wrapper default and builtin sim budget are both 600s, then the
        # outer watchdog adds time for report persistence and process cleanup.
        timeout = self._mcp_tool_timeout_seconds(
            "sim",
            {"work_dir": str(tmp_path), "trace": False},
            {"default_timeout": 600},
        )
        assert timeout == 630


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
            "mcp.server.stdio": MagicMock(),
            "mcp.types": MagicMock(),
        }
        with patch.dict(sys.modules, mcp_stubs):
            from booley.mcp_server import _try_read_report

            self._try_read_report = _try_read_report

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
            "mcp.server.stdio": MagicMock(),
            "mcp.types": MagicMock(),
        }
        with patch.dict(sys.modules, mcp_stubs):
            from collections import defaultdict

            from booley.mcp_server import _resolve_transcript_dir

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
            "mcp.server.stdio": MagicMock(),
            "mcp.types": MagicMock(),
        }
        with patch.dict(sys.modules, mcp_stubs):
            from booley.mcp_server import (
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
        assert self._mcp_tool_visible("elab")
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
        assert "RTL debug helper" in by_name["bwave"]["description"]
        # QA-8: description no longer over-promises blanket auto-conversion;
        # a directly-passed .vcd must be built with `bwave build` first.
        assert "auto-builds an .fst" in by_name["bwave"]["description"]
        assert "`bwave build`" in by_name["bwave"]["description"]
        assert 'extra_args=["skill"]' in by_name["bwave"]["description"]
        assert 'extra_args=["--help"]' in by_name["bwave"]["description"]

    def test_explicit_allowlist_overrides_interactive_defaults(self, monkeypatch):
        monkeypatch.delenv("BOOLEY_NESTED_AGENT", raising=False)
        monkeypatch.setenv("BOOLEY_MCP_MODE", "interactive")
        monkeypatch.setenv("BOOLEY_MCP_TOOLS", "reviewer,bwave,elab")

        assert self._mcp_tool_visible("reviewer")
        assert self._mcp_tool_visible("elab")
        assert self._status_mcp_tool_visible()
        assert not self._mcp_tool_visible("sim")
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


class TestBooleyStatus:
    @pytest.fixture(autouse=True)
    def _import(self):
        mcp_stubs = {
            "mcp": MagicMock(),
            "mcp.server": MagicMock(),
            "mcp.server.models": MagicMock(),
            "mcp.server.stdio": MagicMock(),
            "mcp.types": MagicMock(),
        }
        with patch.dict(sys.modules, mcp_stubs):
            from booley import mcp_server

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
                "elab",
            ]
        )

        assert card == (
            "```text\n"
            "Booley ready. Sandbox container 4f3c2a1b is running.\n"
            "Booley: 0.1.0 (abc123); last updated yesterday; sandbox image built today.\n"
            "Available MCP tools: sim, lint, elab.\n"
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
        def fake_mcp_tool(**kwargs):
            return SimpleNamespace(**kwargs)

        monkeypatch.delenv("BOOLEY_NESTED_AGENT", raising=False)
        monkeypatch.delenv("BOOLEY_MCP_TOOLS", raising=False)
        monkeypatch.setenv("BOOLEY_MCP_MODE", "interactive")
        monkeypatch.setattr(self.mcp_server, "McpSdkTool", fake_mcp_tool)
        monkeypatch.setattr(self.mcp_server, "_bwave_mcp_tools_for_mode", lambda: [])

        mcp_tools = self.mcp_server._build_mcp_tool_list(
            [
                {
                    "name": "sim",
                    "description": "Run simulation",
                    "schema": {"type": "object"},
                },
            ]
        )

        assert [mcp_tool.name for mcp_tool in mcp_tools] == [
            "sim",
            "booley_status",
            "booley_report",
            "booley_poll",
            "booley_cancel",
            "booley_targets",
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


class TestBooleySleep:
    @pytest.fixture(autouse=True)
    def _import(self):
        mcp_stubs = {
            "mcp": MagicMock(),
            "mcp.server": MagicMock(),
            "mcp.server.models": MagicMock(),
            "mcp.server.stdio": MagicMock(),
            "mcp.types": MagicMock(),
        }
        with patch.dict(sys.modules, mcp_stubs):
            from booley import mcp_server

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
        def fake_mcp_tool(**kwargs):
            return SimpleNamespace(**kwargs)

        monkeypatch.delenv("BOOLEY_NESTED_AGENT", raising=False)
        monkeypatch.delenv("BOOLEY_MCP_TOOLS", raising=False)
        monkeypatch.setenv("BOOLEY_MCP_MODE", "interactive")
        monkeypatch.setenv("BOOLEY_MCP_DEBUG_TOOLS", "1")
        monkeypatch.setattr(self.mcp_server, "McpSdkTool", fake_mcp_tool)
        monkeypatch.setattr(self.mcp_server, "_bwave_mcp_tools_for_mode", lambda: [])

        mcp_tools = self.mcp_server._build_mcp_tool_list(
            [
                {
                    "name": "sim",
                    "description": "Run simulation",
                    "schema": {"type": "object"},
                },
            ]
        )

        assert [mcp_tool.name for mcp_tool in mcp_tools] == [
            "sim",
            "booley_status",
            "booley_report",
            "booley_poll",
            "booley_cancel",
            "booley_sleep",
            "booley_targets",
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
            "mcp.server.stdio": MagicMock(),
            "mcp.types": MagicMock(),
        }
        with patch.dict(sys.modules, mcp_stubs):
            from booley import mcp_server

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
        assert sorted(names) == ["sim", "synth"]

    def test_dispatch_applies_filters(self, tmp_path, monkeypatch):
        import json

        self._patch_text_content(monkeypatch)
        monkeypatch.chdir(self._project(tmp_path))

        result = self.mcp_server._dispatch_targets({"for_flow": "synth"})

        payload = json.loads(result[0].text)
        names = [t["name"] for c in payload["cores"] for t in c["targets"]]
        assert names == ["synth"]

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
            "mcp.server.stdio": MagicMock(),
            "mcp.types": MagicMock(),
        }
        with patch.dict(sys.modules, mcp_stubs):
            from booley import mcp_server

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

    def test_bwave_nonzero_exit_and_stderr_are_preserved(self, monkeypatch):
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

        result = asyncio.run(
            self._dispatch_bwave(
                "bwave",
                {"extra_args": ["@dut", "wave", "--virtual", "bad = *nope["]},
            )
        )

        assert result is not None
        assert "EXIT_CODE: 9" in result[0].text
        assert "bad virtual signal" in result[0].text

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
# _load_backend_config_from_toml (Interactive Mode honors [agent] in booley.toml)
# ---------------------------------------------------------------------------


class TestLoadBackendConfigFromToml:
    """Interactive Mode must read [agent] primary/secondary from booley.toml.

    Regression guard: without this, get_backend_config() lazily defaults to
    codex-primary, so specialists ran on Codex even when the project selected
    primary = "claude" — the exact bug this fix addresses.
    """

    @pytest.fixture(autouse=True)
    def _import(self):
        mcp_stubs = {
            "mcp": MagicMock(),
            "mcp.server": MagicMock(),
            "mcp.server.models": MagicMock(),
            "mcp.server.stdio": MagicMock(),
            "mcp.types": MagicMock(),
        }
        with patch.dict(sys.modules, mcp_stubs):
            from booley.harness.config import get_backend_config, set_backend_config
            from booley.mcp_server import _load_backend_config_from_toml

            self._load = _load_backend_config_from_toml
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

        assert self._get().provider == "claude"

    def test_falls_back_to_cwd_when_env_unset(self, tmp_path, monkeypatch):
        root = self._write_project(
            tmp_path,
            '[agent]\nprovider = "claude"\n',
        )
        monkeypatch.delenv("BOOLEY_PROJECT_DIR", raising=False)
        monkeypatch.chdir(root)

        self._load()

        assert self._get().provider == "claude"

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
            "mcp.server.stdio": MagicMock(),
            "mcp.types": MagicMock(),
        }
        with patch.dict(sys.modules, mcp_stubs):
            from booley.mcp_server import _discover_builtin_mcp_tools

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
        from booley.mcp_tools.registry import McpToolInfo

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
        from booley.mcp_tools.registry import discover_mcp_tools

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
            "mcp.server.stdio": MagicMock(),
            "mcp.types": MagicMock(),
        }
        with patch.dict(sys.modules, mcp_stubs):
            from booley import mcp_server

            self.mcp_server = mcp_server
        # Sandbox venue with a clean (Codex-replaced) env: marker present,
        # no proxy vars delivered.
        monkeypatch.setenv("BOOLEY_CONTAINER", "1")
        for var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "NO_PROXY"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.delenv("BOOLEY_MCP_TRANSPORT", raising=False)

    def test_main_defaults_proxy_env_in_sandbox(self, monkeypatch):
        import os

        from booley import runtime_context

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
            "mcp.server.stdio": MagicMock(),
            "mcp.types": MagicMock(),
        }
        with patch.dict(sys.modules, mcp_stubs):
            from booley.mcp_server import (
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

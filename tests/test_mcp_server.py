"""Tests for the MCP server: tool calls, lifetime policy, result text, Jobs, and entry path."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

import tests.mcp_runner_fake as fake
from booley.mcp import catalog, endpoint_process, job_results, report_encoding, server
from booley.mcp.application import McpRequestContext
from booley.mcp.report_encoding import ReportEncoder
from booley.runtime.booley_dir import reset_cache
from tests.target_test_support import write_sim_preview_project
from tests.time_budget import hang_guard_s, scaled


def test_option_like_values_keep_their_flag(monkeypatch, tmp_path):
    """F-12: as the next argv item, argparse would read a selector as a new option and drop it.

    The `=` form keeps it attached to its flag.
    """
    calls = fake.install(monkeypatch)
    app = fake.application(monkeypatch, tmp_path)

    fake.call(app, "sim", {"target": "--meminit=ram,firmware.elf", "test": ["-DFOO", "BAR"]})

    assert calls[0].cmd[3:] == [
        "--target=--meminit=ram,firmware.elf",
        "--test=-DFOO",
        "--test",
        "BAR",
    ]


# id: (idle timeout, max age, steps, expected: "" keeps serving, else the exit reason's fragment)
_LIFETIMES = {
    "idle": (10, None, ["check", 111.0, "check"], ["", "idle"]),
    "in-flight": (10, None, ["start", 200.0, "check", "end", "check"], ["", ""]),
    "max-age-after-in-flight": (
        None,
        10,
        ["start", 200.0, "check", "end", "check"],
        ["", "older"],
    ),
}


@pytest.mark.parametrize(
    ("idle", "max_age", "steps", "expected"), _LIFETIMES.values(), ids=list(_LIFETIMES)
)
def test_lifetime_exit_policy(idle, max_age, steps, expected):
    # Private seam: the lifetime decides only when `main()`'s stdio server stops serving;
    # no public call returns its decision.
    clock = [100.0]
    lifetime = server._McpLifetime(idle, max_age, now=lambda: clock[0])
    decisions = []
    for step in steps:
        if isinstance(step, float):
            clock[0] = step
        elif step == "check":
            decisions.append(lifetime.should_exit())
        else:
            getattr(lifetime, f"mark_mcp_endpoint_{step}")()
    for (should_exit, reason), fragment in zip(decisions, expected, strict=True):
        assert should_exit is bool(fragment)
        assert fragment in reason


# id: (developer exposure, self_exit, idle timeout env, idle timeout set, max age set)
_FROM_ENV = {
    "client-owned": (False, True, None, False, False),
    "http": (True, False, None, False, False),
    "developer-stdio": (True, True, None, True, True),
    "developer-zero-idle": (True, True, "0", False, True),
}


@pytest.mark.parametrize(
    ("developer", "self_exit", "idle_env", "idle_set", "max_age_set"),
    _FROM_ENV.values(),
    ids=list(_FROM_ENV),
)
def test_lifetime_from_env(
    monkeypatch, tmp_path, developer, self_exit, idle_env, idle_set, max_age_set
):
    # Private seam: the lifetime decides only when `main()`'s stdio server stops serving;
    # the reaper heartbeat is a file no public call reads.
    heartbeat = tmp_path / "hb"
    monkeypatch.setattr(server, "_MCP_HEARTBEAT_PATH", str(heartbeat))
    monkeypatch.delenv("BOOLEY_MCP_IDLE_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("BOOLEY_MCP_DEVELOPER_AGENT", raising=False)
    if developer:
        monkeypatch.setenv("BOOLEY_MCP_DEVELOPER_AGENT", "1")
    if idle_env is not None:
        monkeypatch.setenv("BOOLEY_MCP_IDLE_TIMEOUT_SECONDS", idle_env)

    lifetime = server._McpLifetime.from_env(self_exit=self_exit)

    assert (lifetime.idle_timeout_seconds is not None) is idle_set
    assert (lifetime.max_age_seconds is not None) is max_age_set
    # ADR 0018 WS4: wall-clock heartbeat for the external reaper, written at construction
    # and refreshed on activity; ADR 0023: the HTTP server keeps it while never self-exiting.
    assert float(heartbeat.read_text(encoding="utf-8").strip()) > 1_000_000_000
    heartbeat.unlink()
    lifetime.mark_activity()
    assert heartbeat.read_text(encoding="utf-8")


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


class TestFormatMcpToolResult:
    def test_with_report(self):
        report = {
            "status": "pass",
            "summary": "all good",
            "errors": [],
            "report_text": "RESULT: PASS",
            "detail": {"reason": "done", "error": "none"},
        }
        result = ReportEncoder().text_result(0, "output", "", report)
        assert "status: pass" in result
        assert "summary: all good" in result
        assert "report_text: RESULT: PASS" in result
        assert "detail.reason: done" in result
        assert "detail.error: none" in result

    @pytest.mark.parametrize(
        ("stdout", "stderr", "limit", "repeated"),
        [
            pytest.param("RESULT: PASS\n", "", None, False, id="shown-in-stdout"),
            pytest.param("", "RESULT: PASS\n", None, False, id="shown-in-stderr"),
            pytest.param(
                "progress mentions RESULT: PASS ok\n", "", None, True, id="inside-stdout-line"
            ),
            pytest.param(
                "", "progress mentions RESULT: PASS ok\n", None, True, id="inside-stderr-line"
            ),
            pytest.param(
                "RESULT: PASS\n" + "x" * 20,
                "RESULT: PASS\n" + "y" * 20,
                "12",
                True,
                id="truncated",
            ),
        ],
    )
    def test_report_text_shown_once(self, monkeypatch, stdout, stderr, limit, repeated):
        """A verdict printed twice, or dropped when only mentioned or truncated, is the 324ad230d bug."""
        if limit:
            monkeypatch.setenv("BOOLEY_MCP_MAX_STDOUT_BYTES", limit)
            monkeypatch.setenv("BOOLEY_MCP_MAX_STDERR_BYTES", limit)

        result = ReportEncoder().text_result(0, stdout, stderr, {"report_text": "RESULT: PASS"})

        assert ("report_text: RESULT: PASS" in result) is repeated
        if not repeated:
            assert result.count("RESULT: PASS") == 1

    def test_unrelated_stderr_and_report_are_both_retained(self):
        result = ReportEncoder().text_result(
            1, "", "compiler warning\n", {"report_text": "RESULT: FAIL"}
        )

        assert "compiler warning" in result
        assert "report_text: RESULT: FAIL" in result


# ---------------------------------------------------------------------------
# _run_subprocess
# ---------------------------------------------------------------------------


def _child(cmd, **kwargs):
    # Private seam: process supervision (timeout, process group, output cap) of the one function
    # that starts an endpoint child; no MCP tool call exposes it apart from a real endpoint.
    return server._run_subprocess(cmd, **kwargs)


class TestRunSubprocess:
    def test_successful_run(self):
        # Real subprocess via asyncio — `python -c` is portable on Win/Linux.
        code, out, _err, timed_out = asyncio.run(
            _child([sys.executable, "-c", "print('ok')"]),
        )
        assert code == 0
        assert "ok" in out
        assert timed_out is False

    def test_timeout(self):
        code, _out, err, timed_out = asyncio.run(
            _child(
                [sys.executable, "-c", "import time; time.sleep(5)"],
                timeout=scaled(1),
            ),
        )
        assert code == 2
        assert "timed out" in err.lower()
        assert timed_out is True

    def test_os_error(self):
        code, _out, err, timed_out = asyncio.run(
            _child(["__definitely_not_a_real_binary__"]),
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
        started = tmp_path / "started"
        # A grandchild in the same session writes this only if it outlives the
        # kill. Its absence proves the group (not just the direct child) died.
        survived = tmp_path / "survived"
        script = (
            "import os, sys, time, subprocess, pathlib\n"
            "subprocess.Popen([sys.executable, '-c',"
            f' "import time, pathlib; time.sleep({scaled(1.5)});'
            f" pathlib.Path({str(survived)!r}).write_text('x')\"])\n"
            f"pathlib.Path({str(started)!r}).write_text(str(os.getpid()))\n"
            "time.sleep(30)\n"
        )

        async def drive():
            task = asyncio.create_task(
                _child([sys.executable, "-c", script], timeout=hang_guard_s())
            )
            child_deadline = time.monotonic() + hang_guard_s()
            while time.monotonic() < child_deadline:  # wait for the child to come up
                if started.exists():
                    break
                await asyncio.sleep(0.02)
            assert started.exists(), "subprocess never started"
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await asyncio.sleep(scaled(2.0))  # past the grandchild's write attempt

        asyncio.run(drive())
        assert not survived.exists(), (
            "grandchild survived cancellation — process group was not killed"
        )

    def test_stdout_tail_cap(self):
        """Output larger than cap is truncated to the tail."""
        # Private seam: the tail cap is read from the environment per call; no public reader.
        cap = endpoint_process._stdout_cap_bytes()
        # Print 2x the cap; verify we keep only the tail (ends with last marker).
        script = (
            "import sys\n"
            f"chunk = 'A' * 1024\n"
            f"for _ in range({(cap * 2) // 1024}): sys.stdout.write(chunk)\n"
            "sys.stdout.write('END_MARKER')\n"
        )
        code, out, _err, _timed_out = asyncio.run(
            _child([sys.executable, "-c", script]),
        )
        assert code == 0
        assert out.endswith("END_MARKER")
        assert len(out.encode("utf-8")) <= cap


def sim_project(root: Path, toml: str = "") -> Path:
    """A Project whose Test registry gives `a` and `b` two Tests and `coco` (Cocotb) three."""
    data = write_sim_preview_project(
        root,
        {"a": ["first", "second"], "b": ["first", "second"], "coco": ["reset", "stress", "idle"]},
    )
    (root / "coco.core").write_text(
        "CAPI=2:\nname: ::coco:0\ntargets:\n  coco:\n    flow: sim\n"
        "    flow_options: {tool: verilator, cocotb_module: test_dut}\n    toplevel: tb\n",
        encoding="utf-8",
    )
    (data / "booley.toml").write_text(toml or "[project]\n", encoding="utf-8")
    return root


_BUILD_7S = "[flows.sim]\nbuild_timeout_ms = 7000\n"
_NINE = ",".join(f"asic_{idx}" for idx in range(9))
# id: (tool, arguments, booley.toml text, a Project with Tests, expected kill timer in seconds)
_BUDGETS: dict[str, tuple[str, dict[str, Any], str | None, bool, int]] = {
    "sim-trace": ("sim", {"timeout_ms": 10_000, "trace": True}, None, False, 6130),
    "sim-no-trace": ("sim", {"timeout_ms": 10_000}, None, False, 4240),
    "sim-config-knob": (
        "sim",
        {"work_dir": True},
        "[flows.sim]\ntimeout_ms = 1800000\n",
        False,
        6030,
    ),
    "sim-default": ("sim", {"work_dir": True}, None, False, 4830),
    "sim-build-timeout-exact": (
        "sim",
        {"target": "a,b", "timeout_ms": 10_000},
        _BUILD_7S,
        True,
        2484,
    ),
    "sim-coverage-exact": (
        "sim",
        {"target": "a,b", "timeout_ms": 10_000, "coverage": True},
        _BUILD_7S,
        True,
        4104,
    ),
    "sim-coverage-trace-per-unit": (
        "sim",
        {"target": "a", "timeout_ms": 10_000, "coverage": True, "trace": True},
        None,
        True,
        9260,
    ),
    "sim-units": ("sim", {"target": "a,b", "timeout_ms": 600_000}, None, True, 12030),
    "sim-trace-units": (
        "sim",
        {"target": "a", "test": ["first@x3"], "timeout_ms": 600_000, "trace": True},
        None,
        True,
        12900,
    ),
    "sim-elab-only": (
        "sim",
        {"target": "a,b", "mode": "elab_only", "timeout_ms": 10_000},
        None,
        False,
        7230,
    ),
    "sim-elab-only-standalone": (
        "sim",
        {"target": "a,b", "mode": "elab_only_standalone", "timeout_ms": 10_000},
        None,
        False,
        7240,
    ),
    "sim-seed": (
        "sim",
        {"target": "coco", "seed": "random", "timeout_ms": 10_000},
        None,
        True,
        5460,
    ),
    "lint-floor": ("lint", {"target": "x", "timeout_ms": 10_000}, None, False, 600),
    "lint-matrix": ("lint", {"target": "a,b,c,d,e", "timeout_ms": 120_000}, None, False, 630),
    "lint-cwd-config": (
        "lint",
        {"target": "x"},
        "[flows.lint]\ntimeout_ms = 900000\n",
        False,
        930,
    ),
    "synth-call-timeout": (
        "synth",
        {"target": "core", "timeout_ms": 8_000_000},
        None,
        False,
        8180,
    ),
    "synth-matrix": ("synth", {"target": _NINE, "timeout_ms": 1_800_000}, None, False, 16860),
    "synth-baseline": (
        "synth",
        {"target": "a,b", "timeout_ms": 4_000_000, "baseline": "main"},
        None,
        False,
        16360,
    ),
    "mutation-default": ("mutation", {"target": "core"}, None, False, 1800),
    "mutation-raised": (
        "mutation",
        {"target": "core", "timeout_ms": 3_600_000},
        None,
        False,
        3600,
    ),
    "mutation-smaller": ("mutation", {"target": "core", "timeout_ms": 60_000}, None, False, 1800),
}


@pytest.mark.parametrize(
    ("tool", "arguments", "config", "project", "expected"), _BUDGETS.values(), ids=list(_BUDGETS)
)
def test_tool_call_kill_timer_follows_the_flow_budget(
    monkeypatch, tmp_path, tool, arguments, config, project, expected
):
    """The outer kill timer covers every stage the Flow budgets, and never cuts a larger budget."""
    if project:
        sim_project(tmp_path, config or "")
    elif config:
        (tmp_path / ".booley").mkdir()
        (tmp_path / ".booley" / "booley.toml").write_text(config, encoding="utf-8")
    reset_cache()
    calls = fake.install(monkeypatch)
    app = fake.application(monkeypatch, tmp_path)
    if arguments.get("work_dir") is True or project:
        arguments = {**arguments, "work_dir": str(tmp_path)}

    payload = fake.call(app, tool, arguments)

    assert not payload.is_error, payload.content[0].text
    assert calls[-1].kwargs["timeout"] == expected


@pytest.mark.parametrize(
    ("tool", "criterion", "arguments", "expected"),
    [
        ("synth", "synthesis_ok_core", {"target": "core", "timeout_ms": 8_000_000}, 8180),
        ("fpga", "fpga_impl_ok_core", {"target": "core", "timeout_ms": 8_000_000}, 8180),
        ("sim", "cycle_count_core", {"target": "core", "timeout_ms": 10_000}, 4240),
    ],
    ids=["synth", "fpga", "sim"],
)
def test_criterion_baseline_ref_never_changes_the_budget(
    monkeypatch, tmp_path, tool, criterion, arguments, expected
):
    """A baseline revision pinned by a state-file Criterion does not double the budget."""
    state = tmp_path / "state.json"
    state.write_text(
        json.dumps({"criteria": {criterion: {"params": {"_baseline_ref": "a" * 40}}}}),
        encoding="utf-8",
    )
    calls = fake.install(monkeypatch)
    app = fake.application(monkeypatch, tmp_path)
    monkeypatch.setenv("BOOLEY_STATE_FILE", str(state))

    fake.call(app, tool, arguments)

    assert calls[-1].kwargs["timeout"] == expected


@pytest.mark.parametrize("value", [True, 0, -1, 1.5, "7000"])
def test_invalid_build_timeout_is_refused_before_spawn(monkeypatch, tmp_path, value):
    rendered = "true" if value is True else repr(value)
    sim_project(tmp_path, f"[flows.sim]\nbuild_timeout_ms = {rendered}\n")
    reset_cache()
    calls = fake.install(monkeypatch)
    app = fake.application(monkeypatch, tmp_path)

    payload = fake.call(app, "sim", {"target": "a", "work_dir": str(tmp_path)})

    assert payload.is_error
    assert payload.content[0].text.startswith("ERROR: invalid Flow timeout:")
    assert "build_timeout_ms" in payload.content[0].text
    assert calls == []


def test_legacy_timeout_is_rejected_for_mcp(monkeypatch, tmp_path):
    calls = fake.install(monkeypatch)
    app = fake.application(monkeypatch, tmp_path)

    payload = fake.call(app, "lint", {"target": "core", "timeout": 2000})

    assert payload.is_error
    assert "timeout" in payload.content[0].text
    assert calls == []
    # Private seam: the advertised schema rejects `timeout` before dispatch, so only a direct
    # call reaches the budget planner's own refusal.
    with pytest.raises(ValueError, match="removed; use timeout_ms"):
        server._mcp_tool_timeout_seconds(
            "lint", {"target": "core", "timeout": 2000}, {"default_timeout": 600}
        )


def test_mutation_watchdog_follows_resolved_timeout(tmp_path):
    # Private seam: dispatch always passes a resolved call context; this keeps the
    # context-free path `tests/mcp_tools/test_call_context.py` uses.
    watchdog = server._mcp_tool_timeout_seconds
    mutation_def = {"default_timeout": 1800}
    project = {"work_dir": str(tmp_path)}
    # No override: the run's 30-minute default, exactly the outer wait it always had.
    assert watchdog("mutation", project, mutation_def) == 1800
    # A larger budget (which also raises the --count auto cap) is not cut off at 30 minutes.
    assert watchdog("mutation", {**project, "timeout_ms": 3_600_000}, mutation_def) == 3600
    # A smaller one never shrinks the wait below the Flow's default.
    assert watchdog("mutation", {**project, "timeout_ms": 60_000}, mutation_def) == 1800


# ---------------------------------------------------------------------------
# Inline calls and Jobs: the report a call shows
# ---------------------------------------------------------------------------

_INLINE_LINT = {
    "name": "lint",
    "module": "lint",
    "description": "Lint",
    "default_timeout": 600,
    "non_persisting_dry_run": True,
    "schema": {
        "type": "object",
        "properties": {"dry_run": {"type": "boolean"}, "target": {"type": "string"}},
    },
}


_PASS = '{"status": "pass", "summary": "ok"}'
_RUN = {"target": "x"}
# id: (report.json text, unset the runtime and logs directories, arguments, report attached)
_INLINE_REPORTS = {
    "report": (_PASS, False, _RUN, True),
    "malformed": ("NOT JSON", False, _RUN, False),
    "no-logs-dir": (_PASS, True, _RUN, False),
    "non-persisting-dry-run": (_PASS, False, {**_RUN, "dry_run": True}, False),
}


@pytest.mark.parametrize(
    ("report", "unset_env", "arguments", "attached"),
    _INLINE_REPORTS.values(),
    ids=list(_INLINE_REPORTS),
)
def test_inline_call_attaches_latest_readable_report(
    monkeypatch, tmp_path, report, unset_env, arguments, attached
):
    """A dry run that persists nothing shows no historical verdict (e71b16253)."""
    fake.install(monkeypatch, (2, '{"flow": "lint"}', "Dry-run planning failed", False))
    app = fake.application(monkeypatch, tmp_path, definitions=[_INLINE_LINT])
    step = tmp_path / "runtime" / "flow-reports" / "sim" / "1"
    step.mkdir(parents=True)
    (step / "report.json").write_text(report, encoding="utf-8")
    if unset_env:
        monkeypatch.delenv("BOOLEY_RUNTIME_DIR")
        monkeypatch.delenv("BOOLEY_LOGS_DIR")

    payload = fake.call(app, "lint", arguments)

    text = payload.content[0].text
    assert payload.is_error
    assert "Dry-run planning failed" in text
    assert ("status: pass" in text) is attached
    assert (payload.structured_content is not None) is attached


def test_job_result_shows_a_captured_verdict_once(monkeypatch, tmp_path):
    """The stderr verdict and the fresh report render one verdict (324ad230d)."""

    async def child(cmd, *, env, **_kwargs):
        step = tmp_path / "runtime" / "flow-reports" / "lint" / "1"
        step.mkdir(parents=True, exist_ok=True)
        report = {"flow": "lint", "run_id": env["BOOLEY_RUN_ID"], "report_text": "RESULT: FAIL"}
        (step / "report.json").write_text(json.dumps(report), encoding="utf-8")
        return 1, "", "RESULT: FAIL\n", False

    fake.install(monkeypatch, child)
    app = fake.application(monkeypatch, tmp_path)

    payload = fake.call(app, "lint", {"target": "x"})

    text = payload.content[0].text
    assert text.count("RESULT: FAIL") == 1
    assert "report_text:" not in text
    assert payload.structured_content is not None
    # Private seam: `result_text` renders a terminal Job for standalone callers (tests/mcp_tools);
    # a call reaches only the card it builds. It must also show the verdict once.
    run_id = payload.structured_content["reports"][0]["run_id"]
    standalone = server._JobManager(server._McpLifetime(None, None))
    assert standalone.result_text(run_id).count("RESULT: FAIL") == 1


def test_specialist_calls_get_numbered_transcript_dirs(monkeypatch, tmp_path):
    calls = fake.install(monkeypatch)
    app = fake.application(monkeypatch, tmp_path)

    def transcript_dir() -> Path:
        fake.call(app, "reviewer", {"scope": "a.sv"})
        argv = calls[-1].cmd
        return Path(argv[argv.index("--transcript-dir") + 1])

    first, second = transcript_dir(), transcript_dir()

    runtime = tmp_path / "runtime"
    assert first == runtime / "transcripts" / "reviewer" / "1"
    assert second == runtime / "transcripts" / "reviewer" / "2"
    assert first.is_dir() and second.is_dir()
    # With only a logs directory the transcripts live under its runtime directory.
    monkeypatch.delenv("BOOLEY_RUNTIME_DIR")
    assert transcript_dir() == tmp_path / "logs" / ".runtime" / "transcripts" / "reviewer" / "3"
    # Without a configured runtime or logs directory the call still gets a directory.
    monkeypatch.delenv("BOOLEY_LOGS_DIR")
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    fallback = transcript_dir()
    assert fallback.is_dir()
    assert "transcripts" in str(fallback)


# ---------------------------------------------------------------------------
# MCP exposure filtering
# ---------------------------------------------------------------------------

_DEV = {"BOOLEY_MCP_DEVELOPER_AGENT": "1"}


_NESTED = {
    "BOOLEY_NESTED_AGENT": "1",
    "BOOLEY_NESTED_MCP_TOOLS": "sim",
    "BOOLEY_MCP_TOOLS": "reviewer",
}
# id: (environment, visible tools, hidden tools, status tool visible)
_EXPOSURES = {
    "default": ({}, ["mutation", "reviewer", "sim", "submit_run_report"], [], False),
    "interactive": (_DEV, ["sim", "mutation", "reviewer"], [], True),
    "allowlist": (
        {**_DEV, "BOOLEY_MCP_TOOLS": "reviewer,bwave,sim"},
        ["reviewer", "sim", "bwave"],
        ["lint"],
        True,
    ),
    "nested": (_NESTED, ["sim"], ["reviewer"], False),
}


@pytest.mark.parametrize(
    ("mapping", "visible", "hidden", "status"), _EXPOSURES.values(), ids=list(_EXPOSURES)
)
def test_exposure_selects_visible_tools(mapping, visible, hidden, status):
    exposure = catalog.Exposure.from_mapping(mapping)

    assert all(catalog.tool_visible(name, exposure) for name in visible)
    assert not any(catalog.tool_visible(name, exposure) for name in hidden)
    assert catalog.status_visible(exposure) is status


class TestCoverageEvidenceTool:
    def test_bound_nested_server_exposes_only_coverage_evidence(self):
        exposure = catalog.Exposure.from_mapping(
            {
                "BOOLEY_NESTED_AGENT": "1",
                "BOOLEY_NESTED_MCP_TOOLS": "coverage_evidence",
                "BOOLEY_COVERAGE_RECORD": "/reports/coverage.json",
                "BOOLEY_COVERAGE_PROJECT": "/project",
            }
        )

        tools = catalog.visible_definitions(exposure, [])

        assert [tool["name"] for tool in tools] == ["coverage_evidence"]
        assert tools[0]["schema"]["additionalProperties"] is False
        assert tools[0]["schema"]["properties"]["point_refs"]["maxItems"] == 10
        assert "point_ids" not in tools[0]["schema"]["properties"]
        assert "point_refs" in tools[0]["description"]
        assert "point_ids" not in tools[0]["description"]

    def test_nested_servers_keep_utilities_and_hide_unbound_coverage(self):
        exposure = catalog.Exposure.from_mapping(
            {**_NESTED, "BOOLEY_NESTED_MCP_TOOLS": "coverage_evidence"}
        )

        assert catalog.utilities_visible(exposure)
        assert not catalog.coverage_visible(exposure)


def test_bwave_definition_states_the_investigation_contract():
    definitions = catalog.visible_definitions(catalog.Exposure.from_mapping(_DEV), [])
    description = next(tool for tool in definitions if tool["name"] == "bwave")["description"]

    assert "RTL debug helper" in description
    # Investigation contract: trace before hypothesizing, walk backwards from
    # the wrong value, and check that a passing reproducer really triggered.
    assert "rerun `sim` with `trace: true`" in description
    assert "before acting on an RTL hypothesis" in description
    assert "backwards cycle by cycle" in description
    assert "suspected trigger actually occurred" in description
    assert "before editing RTL" in description
    # Detailed syntax and presentation guidance are discovered through these entry points.
    assert 'extra_args=["skill"]' in description
    assert 'extra_args=["--help"]' in description


# ---------------------------------------------------------------------------
# booley_status, health warnings, booley_sleep, booley_targets
# ---------------------------------------------------------------------------


class TestBooleyStatus:
    def test_status_mcp_tool_list_entry_is_appended(self, monkeypatch):
        # Private patch: empties the B-Wave list, so the listing shows only meta tools and Flows.
        monkeypatch.setattr(catalog, "_BWAVE_MCP_TOOLS", [])
        monkeypatch.delenv("BOOLEY_NESTED_AGENT", raising=False)
        monkeypatch.delenv("BOOLEY_MCP_TOOLS", raising=False)
        monkeypatch.setenv("BOOLEY_MCP_DEVELOPER_AGENT", "1")

        mcp_tools = catalog.visible_definitions(
            catalog.Exposure.from_mapping(os.environ),
            [
                {
                    "name": "sim",
                    "description": "Run simulation",
                    "schema": {"type": "object"},
                },
            ],
        )

        assert [mcp_tool["name"] for mcp_tool in mcp_tools] == [
            "booley_cancel",
            "booley_measure",
            "booley_poll",
            "booley_report",
            "booley_status",
            "booley_targets",
            "sim",
        ]

    @pytest.mark.parametrize(
        ("case", "identity"),
        [
            (
                "no-attribution",
                "Session: unknown (Booley could not attribute this call)\n"
                "Branch: unknown\n"
                "Checkout: unknown\n",
            ),
            (
                "unrecorded",
                "Session: unknown (Booley has not recorded this session)\n"
                "Branch: unknown\n"
                "Checkout: unknown\n",
            ),
            (
                "state-error",
                "Session: unknown (Booley could not read its session state)\n"
                "Branch: unknown\n"
                "Checkout: unknown\n",
            ),
            ("no-handle", "Session: unknown\nBranch: main\nCheckout: {repo} (main checkout)\n"),
            (
                "unreadable-checkout",
                "Session: S1\nBranch: main\nCheckout: {repo} (checkout kind unknown)\n",
            ),
            ("recorded", "Session: S1\nBranch: main\nCheckout: {repo} (main checkout)\n"),
        ],
        ids=[
            "no-attribution",
            "unrecorded",
            "state-error",
            "no-handle",
            "unreadable-checkout",
            "recorded",
        ],
    )
    def test_format_status_card(self, monkeypatch, tmp_path, case, identity):
        from booley.mcp.session_registry import Attribution, SessionRegistry
        from booley.runtime.handles import HandleStore
        from booley.runtime.pid import ProcessIdentity
        from tests.git_support import git

        app = fake.application(monkeypatch, tmp_path)
        repo = tmp_path / "repo"
        repo.mkdir()
        if case == "unreadable-checkout":
            (repo / ".git").write_bytes(b"garbage")
        else:
            git(repo, "init", "-q", "-b", "main")
            git(repo, "commit", "-q", "--allow-empty", "-m", "base")
        repo = repo.resolve()
        booley = tmp_path / "booley"
        booley.mkdir()
        monkeypatch.setenv("BOOLEY_DIR", str(booley))
        monkeypatch.setattr(server.socket, "gethostname", lambda: "4f3c2a1b")
        attribution = Attribution(
            key="process:x",
            kind="process",
            work_dir=str(repo),
            worktree_key="",
            branch="main",
            process=ProcessIdentity(200, "pid:[17]", 7),
        )
        if case not in ("no-attribution", "unrecorded"):
            SessionRegistry(booley).upsert(attribution, "booley_status", "started", now=1.0)
        if case == "no-handle":
            HandleStore(booley).path.unlink()
        if case == "state-error":
            # No on-disk state makes the snapshot raise; the card must survive an unexpected
            # registry error.
            def unreadable(_registry):
                raise RuntimeError("unreadable")

            monkeypatch.setattr(SessionRegistry, "snapshot", unreadable)

        payload = fake.call(
            app,
            "booley_status",
            {},
            request_context=McpRequestContext(
                attribution=None if case == "no-attribution" else attribution
            ),
        )

        head = "```text\nBooley ready. Sandbox container 4f3c2a1b is running.\n" + identity.format(
            repo=repo
        )
        card = payload.content[0].text
        assert card.startswith(head)
        version, tools, health, closing = card[len(head) :].split("\n")
        assert version.startswith("Booley: ")
        assert tools.startswith("Available MCP tools: ") and "sim" in tools
        assert health.startswith("Health: ")
        assert closing == "```"


class TestBooleySleep:
    def test_hidden_by_default(self, monkeypatch):
        monkeypatch.delenv("BOOLEY_MCP_DEBUG_TOOLS", raising=False)

        assert not catalog.Exposure.from_mapping(os.environ).debug

    def test_falsey_flag_values_stay_hidden(self, monkeypatch):
        for raw in ("", "0", "false", "no", " FALSE "):
            monkeypatch.setenv("BOOLEY_MCP_DEBUG_TOOLS", raw)
            assert not catalog.Exposure.from_mapping(os.environ).debug

    def test_allowlists_do_not_filter_it(self, monkeypatch):
        monkeypatch.delenv("BOOLEY_NESTED_AGENT", raising=False)
        monkeypatch.setenv("BOOLEY_MCP_DEBUG_TOOLS", "1")
        monkeypatch.setenv("BOOLEY_MCP_TOOLS", "lint")

        mcp_tools = catalog.visible_definitions(catalog.Exposure.from_mapping(os.environ), [])

        assert "booley_sleep" in [mcp_tool["name"] for mcp_tool in mcp_tools]

    def test_list_entry_appended_when_enabled(self, monkeypatch):
        # Private patch: empties the B-Wave list, so the listing shows only meta tools and Flows.
        monkeypatch.setattr(catalog, "_BWAVE_MCP_TOOLS", [])
        monkeypatch.delenv("BOOLEY_NESTED_AGENT", raising=False)
        monkeypatch.delenv("BOOLEY_MCP_TOOLS", raising=False)
        monkeypatch.setenv("BOOLEY_MCP_DEVELOPER_AGENT", "1")
        monkeypatch.setenv("BOOLEY_MCP_DEBUG_TOOLS", "1")

        mcp_tools = catalog.visible_definitions(
            catalog.Exposure.from_mapping(os.environ),
            [
                {
                    "name": "sim",
                    "description": "Run simulation",
                    "schema": {"type": "object"},
                },
            ],
        )

        assert [mcp_tool["name"] for mcp_tool in mcp_tools] == [
            "booley_cancel",
            "booley_measure",
            "booley_poll",
            "booley_report",
            "booley_sleep",
            "booley_status",
            "booley_targets",
            "sim",
        ]


@pytest.mark.parametrize(
    ("tool", "answer", "exit_code"),
    [("bwave", (0, "ok", "", False), 0), ("lint", (2, "", "boom", False), 2)],
    ids=["bwave-ok", "flow-error"],
)
def test_changed_health_warning_leads_the_result(monkeypatch, tmp_path, tool, answer, exit_code):
    """A health warning keeps the call's error disposition (687c5ba87)."""
    from booley.audit.diagnostic_results import DiagnosticFinding, DiagnosticReport, Severity
    from booley.runtime import health

    booley = tmp_path / ".booley"
    booley.mkdir()
    (booley / "booley.toml").write_text('[project]\nname = "health-test"\n', encoding="utf-8")
    reset_cache()
    fake.install(monkeypatch, answer)
    app = fake.application(monkeypatch, tmp_path, definitions=[_INLINE_LINT])
    failing = DiagnosticFinding(Severity.FAIL, "probe unavailable", "reviewed", "test.health", "x")
    health.publish_result(
        tmp_path,
        trigger="test",
        duration_s=0,
        diagnostics=DiagnosticReport((failing,)),
        transcript="probe text\n",
    )

    payload = fake.call(
        app, tool, {"extra_args": ["--help"]} if tool == "bwave" else {"target": "x"}
    )

    assert payload.content[0].text.startswith("HEALTH WARNING:")
    assert payload.content[1].text.startswith(f"EXIT_CODE: {exit_code}")
    assert payload.is_error is (exit_code != 0)
    reset_cache()


def test_health_warning_rejects_invalid_content_shape():
    # Private seam: only a Flow or bwave call carries content, and the application always
    # builds a list or a tuple; this guards the type no public call produces.
    block = SimpleNamespace(type="text", text="HEALTH WARNING")

    with pytest.raises(TypeError, match="content that is not a list"):
        server._prepend_health_block(object(), block)


_SLEEPS = {
    "nan-rejected": ({"seconds": float("nan")}, "Provide 'seconds' as a non-negative number."),
    "zero-completes": ({"seconds": 0}, "SLEEP_COMPLETE: requested=0.0s"),
}


@pytest.mark.parametrize(("arguments", "start"), _SLEEPS.values(), ids=list(_SLEEPS))
def test_sleep_tool(monkeypatch, tmp_path, arguments, start):
    app = fake.application(monkeypatch, tmp_path)

    payload = fake.call(app, "booley_sleep", arguments)

    assert payload.content[0].text.startswith(start)


class TestBooleyTargets:
    @staticmethod
    def targets_project(root: Path) -> Path:
        (root / "alpha.core").write_text(
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
        return root

    @pytest.mark.parametrize("case", ["all", "synth", "fpga-axis", "bad-work-dir"])
    def test_targets_tool(self, monkeypatch, tmp_path, case):
        app = fake.application(monkeypatch, tmp_path)
        self.targets_project(tmp_path)
        arguments = {
            "all": {},
            "synth": {"for_flow": "synth"},
            "fpga-axis": {"for_flow": "fpga"},
            "bad-work-dir": {"work_dir": str(tmp_path / "not-a-worktree")},
        }[case]

        payload = fake.call(app, "booley_targets", arguments)

        text = payload.content[0].text
        if case == "bad-work-dir":
            assert text.startswith("ERROR: work_dir")
            return
        targets = [t for core in json.loads(text)["cores"] for t in core["targets"]]
        if case == "all":
            assert sorted(t["name"] for t in targets) == ["fpga_core", "sim", "synth"]
            synth = next(t for t in targets if t["name"] == "synth")
            assert synth["synth_mode"] == "physical"
            assert synth["eda_tools"] == ["sv2v", "yosys", "openroad"]
        elif case == "synth":
            assert [t["name"] for t in targets] == ["synth"]
        else:
            assert [t["name"] for t in targets] == ["fpga_core"]
            assert targets[0]["eda_tool"] == "verilator"
            assert targets[0]["drivable_by"] == ["fpga"]

    def test_dispatch_rejects_bad_for_flow(self, monkeypatch, tmp_path):
        # Private seam: the advertised schema enum blocks an unknown `for_flow`, so no
        # application call reaches the handler's own error answer.
        monkeypatch.chdir(self.targets_project(tmp_path))

        result = server._dispatch_targets({"for_flow": "reviewer"})

        assert result[0].text.startswith("ERROR:")
        assert "not a target-aware Booley Flow" in result[0].text


# ---------------------------------------------------------------------------
# bwave dispatch
# ---------------------------------------------------------------------------


def test_tool_calls_route_to_their_command(monkeypatch, tmp_path):
    """`bwave` runs the B-Wave CLI and a Flow runs its module: neither reaches the other."""
    calls = fake.install(monkeypatch)
    app = fake.application(monkeypatch, tmp_path)

    wave = fake.call(app, "bwave", {"extra_args": ["register", "sim/work", "--as", "dut"]})
    fake.call(app, "sim", {"target": "smoke"})

    assert "EXIT_CODE: 0" in wave.content[0].text
    assert [call.cmd for call in calls] == [
        [sys.executable, "-m", "booley.bwave.cli", "register", "sim/work", "--as", "dut"],
        ["python", "-m", "booley.flows.sim.flow", "--target", "smoke"],
    ]


def test_dispatch_without_a_server_identity_runs_the_tool(monkeypatch, tmp_path):
    # Private seam: `_build_mcp_application` always resolves a server identity, so only a
    # direct call reaches the identity-less arm.
    fake.install(monkeypatch)
    fake.application(monkeypatch, tmp_path)
    jobs = server._JobManager(server._McpLifetime(None, None))
    arguments = {"extra_args": ["--help"]}

    result = asyncio.run(
        server._dispatch_application_tool(
            "bwave", arguments, {}, jobs=jobs, status_mcp_tool_names=[], mcp_tool_call_counts={}
        )
    )

    assert "EXIT_CODE: 0" in result[0].text


def _display_events(tmp_path: Path) -> list[dict[str, Any]]:
    lines = (tmp_path / "runtime" / "display.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines]


@pytest.mark.parametrize(
    ("answer", "exit_code"),
    [
        pytest.param((0, "ok", "", False), 0, id="ok"),
        pytest.param((9, "", "bad virtual signal", False), 9, id="nonzero-exit"),
        pytest.param(OSError("spawn failed"), 2, id="spawn-error"),
    ],
)
def test_bwave_call_brackets_console_activity(monkeypatch, tmp_path, answer, exit_code):
    """A spawn failure still closes the console activity it opened (d8c107aa2)."""
    fake.install(monkeypatch, answer)
    app = fake.application(monkeypatch, tmp_path)
    arguments = {"extra_args": ["@dut", "wave", "-t", "start:done"]}

    if isinstance(answer, OSError):
        with pytest.raises(OSError, match="spawn failed"):
            fake.call(app, "bwave", arguments)
    else:
        text = fake.call(app, "bwave", arguments).content[0].text
        assert f"EXIT_CODE: {exit_code}" in text
        assert answer[2] in text

    events = _display_events(tmp_path)
    assert [event["type"] for event in events] == ["endpoint_start", "endpoint_end"]
    assert [event["endpoint"] for event in events] == ["bwave", "bwave"]
    assert [event["display_label"] for event in events] == ["@dut wave -t start:done"] * 2
    assert events[1]["exit_code"] == exit_code
    assert events[1]["duration_s"] >= 0


class TestBwaveDispatch:
    def test_routed_call_reads_the_worktree_store(self, tmp_path, monkeypatch):
        """A routed bwave call resolves `@<alias>` from the caller's checkout store (#3483)."""
        import booley

        app = fake.application(monkeypatch, tmp_path)
        main = tmp_path / "main"
        worktree = tmp_path / "worktree"
        for checkout, marker in ((main, "main_mark"), (worktree, "tree_mark")):
            store = checkout / ".booley" / ".runtime" / "bwave"
            store.mkdir(parents=True)
            session = {"dut": {"trace": "trace.fst", "markers": {marker: 7}}}
            (store / "bwave_sessions.json").write_text(json.dumps(session), encoding="utf-8")
        (tmp_path / "private-tmp").mkdir()
        for name in ("TMPDIR", "TEMP", "TMP"):
            monkeypatch.setenv(name, str(tmp_path / "private-tmp"))
        monkeypatch.setenv("BOOLEY_CONTAINER", "1")
        monkeypatch.setenv("PYTHONPATH", str(Path(booley.__file__).resolve().parents[1]))
        monkeypatch.delenv("RTL_PROJECT_ROOT", raising=False)
        monkeypatch.chdir(main)

        def call(request_context):
            arguments = {"extra_args": ["markers", "@dut", "list"]}
            return fake.call(app, "bwave", arguments, request_context=request_context)

        routed = call(McpRequestContext(checkout=worktree)).content[0].text
        unrouted = call(None).content[0].text

        assert "tree_mark" in routed and "main_mark" not in routed
        assert "main_mark" in unrouted and "tree_mark" not in unrouted


@pytest.mark.parametrize("name", ["bwave_nope", "bwave"])
def test_bwave_handler_declines_unknown_or_hidden_names(monkeypatch, name):
    # Private seam: the application lists only visible tools, so no public call reaches a
    # hidden or unknown name; the guard stops a catalog change between listing and dispatch
    # from running B-Wave.
    calls = fake.install(monkeypatch)
    monkeypatch.setenv("BOOLEY_MCP_TOOLS", "sim")

    assert asyncio.run(server._dispatch_bwave(name, {"extra_args": ["markers"]})) is None
    assert calls == []


# ---------------------------------------------------------------------------
# The Sandbox honors [agent] in booley.toml
# ---------------------------------------------------------------------------


class TestLoadAgentSettingsFromToml:
    """The Sandbox MCP server must preload agent settings from booley.toml.

    Regression guard: without this, Runtime may lazily use defaults even when
    the Project selected another provider.
    """

    # Private seams: `main()` runs this loader at startup before it serves, and the tests read
    # back the process-wide backend config it fills; no MCP call returns either.
    _load = staticmethod(server._load_agent_settings_from_toml)

    @pytest.fixture(autouse=True)
    def _clean_backend_config(self):
        from booley.runtime.agent_config import get_backend_config, set_backend_config

        self._get, self._set = get_backend_config, set_backend_config
        self._set(None)  # start from a clean global so the real load path runs
        try:
            yield
        finally:
            self._set(None)

    def _write_project(self, tmp_path: Path, body: str) -> Path:
        bp = tmp_path / ".booley"
        bp.mkdir()
        (bp / "booley.toml").write_text(body)
        return tmp_path

    def test_claude_provider_from_booley_dir(self, tmp_path, monkeypatch):
        # BOOLEY_DIR points at the .booley dir; the loader must
        # resolve the *parent* as the Project Root.
        root = self._write_project(
            tmp_path,
            '[agent]\nprovider = "claude"\n',
        )
        monkeypatch.setenv("BOOLEY_DIR", str(root / ".booley"))

        self._load()

        assert self._get().settings.default_cli == "claude"

    def test_nested_relocated_booley_dir_is_read_in_place(self, tmp_path, monkeypatch):
        # ``dir = "cfg/state"``: the Booley Directory's parent is not the Project Root,
        # so the loader must read BOOLEY_DIR itself rather than ``<parent>/.booley``.
        (tmp_path / "booley.toml").write_text('[project]\ndir = "cfg/state"\n')
        state = tmp_path / "cfg" / "state"
        state.mkdir(parents=True)
        (state / "booley.toml").write_text('[agent]\nprovider = "codex"\n')
        monkeypatch.setenv("BOOLEY_DIR", str(state))

        self._load()

        assert self._get().settings.default_cli == "codex"

    def test_falls_back_to_cwd_when_env_unset(self, tmp_path, monkeypatch):
        root = self._write_project(
            tmp_path,
            '[agent]\nprovider = "claude"\n',
        )
        monkeypatch.delenv("BOOLEY_DIR", raising=False)
        monkeypatch.chdir(root)

        self._load()

        assert self._get().settings.default_cli == "claude"

    def test_missing_toml_does_not_raise(self, tmp_path, monkeypatch):
        # No .booley/booley.toml at all — must not crash server startup.
        monkeypatch.delenv("BOOLEY_DIR", raising=False)
        monkeypatch.chdir(tmp_path)

        self._load()  # should be a no-op-ish, not an exception


# ---------------------------------------------------------------------------
# Built-in discovery
# ---------------------------------------------------------------------------


class TestBuiltinDiscoveryGate:
    def test_mcp_endpoint_reaches_import_stage(self):
        from booley.mcp.registry import McpToolInfo

        info = McpToolInfo(
            name="shiny_mcp_tool",
            description="an MCP endpoint",
            path="mcp_tools/shiny_mcp_tool.py",
        )
        results, errors = catalog.discover_builtin_definitions(
            [info], catalog.Exposure.from_mapping({})
        )

        assert results == []
        assert len(errors) == 1
        assert "IMPORT FAILED" in errors[0]

    def test_real_endpoint_dirs_discover_cleanly(self):
        # Golden: over the real Flow/MCP packages the import scan must agree with the AST scan and
        # produce zero errors, so no current or future helper module surfaces as NO MCP ENDPOINT
        # CLASS FOUND. Synth and FPGA derive from ImplementationFlow, not BuiltinFlow directly:
        # the import-free scan must still list them, or their CLI and MCP tools vanish.
        from booley.mcp.registry import discover_mcp_tools

        discovered = discover_mcp_tools()

        results, errors = catalog.discover_builtin_definitions(
            discovered, catalog.Exposure.from_mapping({})
        )

        assert errors == []
        assert {r["name"] for r in results} == {endpoint.name for endpoint in discovered}
        kinds = {info.name: info.kind for info in discovered}
        assert kinds["synth"] == "flow"
        assert kinds["fpga"] == "flow"


# ---------------------------------------------------------------------------
# main(): proxy env self-heal and scratch sweep
# ---------------------------------------------------------------------------


class TestMainProxySelfHeal:
    """Codex replaces the MCP child env per config.toml [env]; a config that
    drops the proxy vars leaves the server with no egress. main() must
    self-heal via venue.ensure_proxy_env() so the whole MCP tool subtree under
    the server inherits a working proxy path."""

    @pytest.fixture(autouse=True)
    def _import(self, monkeypatch):
        self.mcp_server = server
        # Sandbox venue with a clean (Codex-replaced) env: marker present,
        # no proxy vars delivered.
        monkeypatch.setenv("BOOLEY_CONTAINER", "1")
        for var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "NO_PROXY"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.delenv("BOOLEY_EGRESS", raising=False)
        monkeypatch.delenv("BOOLEY_MCP_TRANSPORT", raising=False)

    def test_main_defaults_proxy_env_in_sandbox(self, monkeypatch):
        import os

        from booley.runtime import runtime_context

        # Private patch: neuters the server itself; only the entry path is under test.
        monkeypatch.setattr(self.mcp_server, "_main", MagicMock())
        monkeypatch.setattr(self.mcp_server, "asyncio", MagicMock())
        monkeypatch.setattr(sys, "argv", ["booley-mcp"])

        self.mcp_server.main()

        for var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            assert os.environ[var] == runtime_context._PROXY_URL
        assert os.environ["NO_PROXY"] == "localhost,127.0.0.1"

    def test_main_leaves_unproxied_sandbox_unproxied(self, monkeypatch):
        import os

        monkeypatch.setenv("BOOLEY_EGRESS", "unproxied")
        # Private patch: neuters the server itself; only the entry path is under test.
        monkeypatch.setattr(self.mcp_server, "_main", MagicMock())
        monkeypatch.setattr(self.mcp_server, "asyncio", MagicMock())
        monkeypatch.setattr(sys, "argv", ["booley-mcp"])

        self.mcp_server.main()

        assert not any(
            var in os.environ for var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")
        )

    def test_main_refuses_invalid_egress_mode(self, monkeypatch, capsys):
        monkeypatch.setenv("BOOLEY_EGRESS", "bogus")
        # Private patch: neuters the server itself; only the entry path is under test.
        monkeypatch.setattr(self.mcp_server, "_main", MagicMock())
        monkeypatch.setattr(self.mcp_server, "asyncio", MagicMock())
        monkeypatch.setattr(sys, "argv", ["booley-mcp"])

        with pytest.raises(SystemExit) as refusal:
            self.mcp_server.main()

        assert refusal.value.code == 2
        assert "`unproxied`" in capsys.readouterr().err

    def test_main_respects_delivered_proxy_env(self, monkeypatch):
        """A config that DOES forward the proxy vars must win untouched."""
        import os

        monkeypatch.setenv("HTTPS_PROXY", "http://corp-proxy:3128")
        # Private patch: neuters the server itself; only the entry path is under test.
        monkeypatch.setattr(self.mcp_server, "_main", MagicMock())
        monkeypatch.setattr(self.mcp_server, "asyncio", MagicMock())
        monkeypatch.setattr(sys, "argv", ["booley-mcp"])

        self.mcp_server.main()

        assert os.environ["HTTPS_PROXY"] == "http://corp-proxy:3128"
        assert "HTTP_PROXY" not in os.environ


# ---------------------------------------------------------------------------
# Report transport and live checkpoints
# ---------------------------------------------------------------------------


def test_simulation_count_transport_omits_only_full_mapping():
    from copy import deepcopy

    report = {
        "$schema": "booley.simulation-report/v3",
        "flow": "sim",
        "passed": True,
        "detail": {
            "records": {
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
    result = ReportEncoder.structured_report(report)
    assert report == before
    expected = {key: value for key, value in report.items() if key != "cycle_counts"}
    assert result["reports"] == [expected]
    report["flow"] = "synth"
    assert ReportEncoder.structured_report(report)["truncated"] is True


@pytest.mark.parametrize("completion_error", [False, True])
def test_cycle_card_fits_exact_stream_boundary_until_completion_append(
    tmp_path, monkeypatch, completion_error
):
    # Private seam: the card's line builder has no public entry that returns it before the
    # transport appends the completion error, which is the boundary under test.
    from booley.flows.sim.record_presentation import _simulation_record_report_lines
    from booley.flows.test_runs import build_test_run
    from booley.runtime.endpoint_execution import EndpointOutcome, normalize_completion_error
    from tests.flows.sim.simulation_record_test_support import _recorded_simulation_record

    manifest = Path("/manifest.json")
    pointer_bytes = len(f"  manifest: {manifest.resolve()}\n".encode())
    budget = 4000 + pointer_bytes
    monkeypatch.setenv("BOOLEY_MCP_MAX_STDOUT_BYTES", str(budget))
    monkeypatch.setenv("BOOLEY_MCP_MAX_STDERR_BYTES", str(budget))
    smoke = build_test_run("smoke")
    observation = _recorded_simulation_record(tmp_path, (smoke,)).fact(
        smoke, test="smoke", cycle_count=7, detail={"reason": "x" * 3870}
    )
    outcome = SimpleNamespace(
        target={"selector": "sim"},
        manifest_path=manifest,
        aggregate_grade="fail",
        observations=[observation],
    )
    card = "\n".join(_simulation_record_report_lines([outcome]))
    assert len(card.encode()) + 2 <= budget
    result = EndpointOutcome(exit_code=1, report_text=card)
    if completion_error:
        normalize_completion_error(result, OSError("y" * 200), "publish")
    stderr = "earlier output\n" * 300 + "\n" + result.report_text + "\n"
    rendered = ReportEncoder().text_result(
        result.exit_code, "", stderr, {"report_text": result.report_text}
    )
    assert rendered.count("sim: FAIL") == 1
    assert ("--- report ---" in rendered) is completion_error


def test_fetched_simulation_card_never_expands_top_level_counts(monkeypatch, tmp_path):
    app = fake.application(monkeypatch, tmp_path)
    reports = tmp_path / "runtime" / "flow-reports"
    reports.mkdir(parents=True)
    report = {
        "flow": "sim",
        "detail": {"records": {}},
        "cycle_counts": {"sim": [{"test": "hidden-full-row", "cycle_count": 7}]},
    }
    (reports / "sim.json").write_text(json.dumps(report), encoding="utf-8")

    card = fake.call(app, "booley_report", {"endpoint": "sim"}).content[0].text

    assert "flow: sim" in card
    assert "hidden-full-row" not in card


@pytest.mark.parametrize("response_kind", ["handoff", "attach", "poll", "adopted_poll"])
def test_live_sim_checkpoint_survives_mcp_background_responses(
    tmp_path, monkeypatch, response_kind
):
    """A gated real Job keeps its current checkpoint through every MCP handoff."""
    # Private seams below: attaching needs a live child whose `/proc` argv matches the Job
    # record, and the stand-in child runs in this process, so the Job manager, the dispatch and
    # the Job root are driven directly; the shared runner fake supplies the gated child.
    from booley.mcp import server
    from booley.mcp.call_context import CallContext

    monkeypatch.setenv("BOOLEY_MCP_JOB_INLINE_WAIT_SECONDS", "0")

    # Private patch: this process's real health state must not prefix the Job responses.
    monkeypatch.setattr(server, "_prepend_changed_health_alert", lambda result: result)
    context = CallContext(tmp_path, tmp_path / "jobs", None, tmp_path, {}, None)
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
                        "log": {"path": "build/dut/run.log", "live": True, "complete": False},
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
        await asyncio.wait_for(release.wait(), timeout=hang_guard_s())
        return 0, "", "", False

    fake.install(monkeypatch, gated_child)
    jobs = server._JobManager(server._McpLifetime(None, None))
    # Yield inside the real inline-wait boundary until the child published.
    original_wait = jobs.wait

    async def inline_wait(run_id, seconds):
        await asyncio.wait_for(entered.wait(), timeout=hang_guard_s())
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
        await asyncio.wait_for(asyncio.gather(*jobs._tasks.values()), timeout=hang_guard_s())


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


def test_registered_poll_observes_blocked_simulation_record(tmp_path, monkeypatch):
    """A record launch blocked in the simulator shows as one live, running target."""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from booley.runtime.timefmt import utc_now_rfc3339
    from tests.flows.sim.simulation_record_boundary import PASS_PROCESS, run_simulation_record_sim

    run_id = "gated-record-sim"
    application = fake.application(monkeypatch, tmp_path)
    monkeypatch.setenv("BOOLEY_RUN_ID", run_id)
    entered, release = Event(), Event()

    def blocked_launch(_launch):
        entered.set()
        assert release.wait(hang_guard_s()), "test failed to release simulator launch"
        return PASS_PROCESS

    server.jobrec.write_record(
        server.jobrec.JobRecord(
            run_id=run_id,
            endpoint="sim",
            started_at=utc_now_rfc3339(),
            timeout_s=60,
            pid=os.getpid(),
            argv=[],
        ),
        root=tmp_path / "runtime/jobs",
    )
    # Private seam: the Flow under test writes its reports to the directory the test gives it;
    # the record points the Job at that directory, which no Job record field carries.
    monkeypatch.setattr(
        job_results, "_endpoint_report_dirs", lambda *_args, **_kwargs: (tmp_path / "reports",)
    )
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            run_simulation_record_sim,
            tmp_path,
            monkeypatch,
            {"sim": ["smoke"]},
            blocked_launch,
            target="sim",
        )
        try:
            assert entered.wait(hang_guard_s()), "simulator launch was not reached"
            payload = asyncio.run(
                application.call_tool("booley_poll", {"run_id": run_id, "wait_seconds": 0})
            )
            report = payload.structured_content["reports"][0]
            assert report["run_id"] == run_id
            assert report["phase"] == "running"
            assert report["partial"] is True and report["complete"] is False
            assert report["completed_targets"] == []
            assert report["pending_targets"] == ["sim"]
            assert len(report["active"]) == 1
            active = report["active"][0]
            assert active["stage"] == "executing"
            assert active["log"]["live"] is True
            assert Path(active["log"]["path"]).parts[-2:] == ("execution-evidence", "run.log")
            assert "passed" not in payload.structured_content
        finally:
            release.set()
            # The released run must finish cleanly, not merely stop blocking.
            assert future.result(timeout=hang_guard_s()).result.exit_code == 0


def test_oversized_live_checkpoint_preserves_owned_active_log_pointer():
    # Private seam: the transport's size bound is a module limit with no public reader.
    active = {
        "target": "sim",
        "stage": "executing",
        "role": "candidate",
        "log": {"path": "build/current/run.log", "live": True, "complete": False},
    }
    report = {
        "flow": "sim",
        "run_id": "current",
        "phase": "running",
        "complete": False,
        "partial": True,
        "active": [active],
        "detail": {"completed": "large diagnostic" * report_encoding._MAX_STRUCTURED_REPORT_BYTES},
    }
    payload = ReportEncoder.structured_report(report)
    assert payload["truncated"] is True
    assert payload["reports"] == []
    checkpoint = payload
    assert checkpoint["active"] == [active]
    assert checkpoint["run_id"] == "current"
    assert checkpoint["complete"] is False and checkpoint["partial"] is True
    assert "passed" not in payload and "exit_code" not in checkpoint
    assert report_encoding._payload_size(payload) <= report_encoding._MAX_STRUCTURED_REPORT_BYTES


class TestMainSweepsStaleScratch:
    """MCP server start reclaims Booley scratch left by SIGKILL-ed runs."""

    @pytest.mark.skipif(sys.platform == "win32", reason="needs SIGKILL and the Linux Sandbox")
    def test_main_reclaims_killed_run_scratch(self, tmp_path, monkeypatch):
        import shutil
        import tempfile

        from tests.owned_scratch_support import kill_child_with_scratch, make_repo

        mcp_server = server
        monkeypatch.setenv("BOOLEY_CONTAINER", "1")
        monkeypatch.setenv("BOOLEY_EGRESS", "unproxied")
        monkeypatch.delenv("BOOLEY_MCP_TRANSPORT", raising=False)
        scratch = tmp_path / "scratch"
        scratch.mkdir()
        monkeypatch.setattr(tempfile, "tempdir", str(scratch))
        repo = make_repo(tmp_path / "repo")
        killed = kill_child_with_scratch(repo, scratch, tmp_path / "home")
        # Private patch: neuters the server itself; only the entry path is under test.
        monkeypatch.setattr(mcp_server, "_main", MagicMock())
        monkeypatch.setattr(mcp_server, "asyncio", MagicMock())
        monkeypatch.setattr(sys, "argv", ["booley-mcp"])
        try:
            mcp_server.main()
            assert [path for path in killed if path.exists()] == []
        finally:
            for path in killed:  # unfixed code leaves the nested home in the real /tmp
                shutil.rmtree(path, ignore_errors=True)

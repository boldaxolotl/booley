"""Tests for MCP server dispatch helpers (params_to_argv, format_endpoint_result)."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

# mcp_server.py lives at src/ root, outside the endpoints package
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

try:
    from booley.mcp import server as mcp_server
    from booley.mcp.server import (
        _available_report_endpoints,
        _dispatch_async_job,
        _dispatch_cancel,
        _dispatch_poll,
        _dispatch_report,
        _format_mcp_tool_result,
        _format_report_card,
        _job_phase,
        _JobManager,
        _latest_report,
        _params_to_argv,
        _poll_mcp_tool_def,
        _poll_mcp_tool_visible,
        _report_mcp_tool_def,
        _report_mcp_tool_visible,
        _validate_work_dir,
    )
except ImportError:
    pytest.skip("mcp package not installed", allow_module_level=True)

from booley.runtime import job_records as jobrec
from booley.runtime import job_slots


class TestValidateWorkDir:
    """Fail-closed gate for the agent-facing work_dir argument: only the
    session workspace itself or the root of a linked git worktree passes."""

    def test_absent_is_valid(self):
        assert _validate_work_dir(None) is None

    def test_cwd_spelled_explicitly_is_valid(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert _validate_work_dir(str(tmp_path)) is None

    def test_linked_worktree_root_is_valid(self, tmp_path):
        wt = tmp_path / "wt"
        wt.mkdir()
        # A linked worktree marks its root with a .git *file* (gitdir pointer).
        (wt / ".git").write_text("gitdir: /repo/.git/worktrees/wt\n", encoding="utf-8")
        assert _validate_work_dir(str(wt)) is None

    def test_relative_path_resolves_against_cwd(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        wt = tmp_path / "wt"
        wt.mkdir()
        (wt / ".git").write_text("gitdir: /repo/.git/worktrees/wt\n", encoding="utf-8")
        assert _validate_work_dir("wt") is None

    def test_missing_dir_rejected_with_guidance(self, tmp_path):
        err = _validate_work_dir(str(tmp_path / "nope"))
        assert err is not None and "does not exist" in err
        assert "worktree_create.sh" in err

    def test_plain_dir_without_git_pointer_rejected(self, tmp_path):
        plain = tmp_path / "plain"
        plain.mkdir()
        err = _validate_work_dir(str(plain))
        assert err is not None and "linked git worktree" in err

    def test_main_repo_clone_rejected(self, tmp_path):
        # A full clone has a .git *directory* — not a linked worktree, and not
        # the session workspace, so it is refused.
        clone = tmp_path / "clone"
        (clone / ".git").mkdir(parents=True)
        err = _validate_work_dir(str(clone))
        assert err is not None and "linked git worktree" in err


class TestParamsToArgv:
    def test_simple_string(self):
        argv = _params_to_argv({"target": "sim"})
        assert argv == ["--target", "sim"]

    def test_integer(self):
        argv = _params_to_argv({"count": 42})
        assert argv == ["--count", "42"]

    def test_bool_true(self):
        argv = _params_to_argv({"trace": True})
        assert argv == ["--trace"]

    def test_bool_false_omitted(self):
        argv = _params_to_argv({"trace": False})
        assert argv == []

    def test_list(self):
        argv = _params_to_argv({"tag": ["a", "b"]})
        assert argv == ["--tag", "a", "--tag", "b"]

    def test_underscore_to_hyphen(self):
        argv = _params_to_argv({"tb_top": "tb"})
        assert argv == ["--tb-top", "tb"]

    def test_mixed_params(self):
        argv = _params_to_argv(
            {
                "target": "sim",
                "trace": True,
                "verbose": False,
                "count": 5,
            }
        )
        assert "--target" in argv
        assert "--trace" in argv
        assert "--verbose" not in argv
        assert "--count" in argv

    def test_empty_params(self):
        assert _params_to_argv({}) == []


class TestFormatMcpToolResult:
    def test_success_with_stdout(self):
        result = _format_mcp_tool_result(0, "all tests passed\n", "")
        assert "EXIT_CODE: 0" in result
        assert "all tests passed" in result

    def test_failure_with_stderr(self):
        result = _format_mcp_tool_result(1, "", "Error: file not found\n")
        assert "EXIT_CODE: 1" in result
        assert "Error: file not found" in result

    def test_with_report(self):
        report = {"status": "pass", "summary": "3/3 tests pass", "errors": []}
        result = _format_mcp_tool_result(0, "ok", "", report)
        assert "--- report ---" in result
        assert "status: pass" in result
        assert "summary: 3/3 tests pass" in result

    def test_short_stdout_not_truncated(self):
        short_stdout = "x" * 5_000
        result = _format_mcp_tool_result(0, short_stdout, "")
        assert "truncated" not in result

    def test_long_stdout_truncated(self):
        long_stdout = "x" * 15_000
        result = _format_mcp_tool_result(0, long_stdout, "")
        assert "truncated" in result

    def test_empty_outputs(self):
        result = _format_mcp_tool_result(0, "", "")
        assert result == "EXIT_CODE: 0"

    def test_report_text_duplicated_in_stdout_is_deduped(self):
        # Heavy endpoints print report_text to stdout AND return it in report.json;
        # when the stdout copy survives truncation, the report copy is skipped.
        report_text = "RESULT: PASS (3/3 targets)\nsee build/report.html"
        stdout = f"compiling...\nrunning...\n{report_text}\n"
        report = {"status": "pass", "summary": "3/3", "report_text": report_text}
        result = _format_mcp_tool_result(0, stdout, "", report)
        assert result.count("RESULT: PASS (3/3 targets)") == 1
        assert "report_text:" not in result
        # The other report keys still render.
        assert "--- report ---" in result
        assert "status: pass" in result
        assert "summary: 3/3" in result

    def test_report_text_truncated_out_of_stdout_is_kept(self):
        # If truncation cut the report_text out of the shown stdout, the
        # containment check must fail so the summary survives in the report
        # section — exactly when it is needed.
        report_text = "RESULT: PASS (3/3 targets)"
        stdout = report_text + "\n" + "x" * 20_000  # tail-cap eats the front
        report = {"status": "pass", "report_text": report_text}
        result = _format_mcp_tool_result(0, stdout, "", report)
        assert "truncated" in result
        assert f"report_text: {report_text}" in result

    def test_report_text_trailing_newline_still_dedupes(self):
        # Whitespace-stripped comparison: a trailing newline on either side
        # must not defeat the containment check.
        report = {"status": "pass", "report_text": "RESULT: PASS\n"}
        result = _format_mcp_tool_result(0, "RESULT: PASS", "", report)
        assert "report_text:" not in result
        assert "status: pass" in result


class TestOutputCapEnvKnobs:
    """BOOLEY_MCP_MAX_STDOUT_BYTES / BOOLEY_MCP_MAX_STDERR_BYTES overrides."""

    def test_stdout_cap_override_applies(self, monkeypatch):
        monkeypatch.setenv("BOOLEY_MCP_MAX_STDOUT_BYTES", "100")
        result = _format_mcp_tool_result(0, "x" * 300, "")
        assert "truncated, showing last 100 bytes" in result

    def test_stderr_cap_override_applies(self, monkeypatch):
        monkeypatch.setenv("BOOLEY_MCP_MAX_STDERR_BYTES", "100")
        result = _format_mcp_tool_result(1, "", "z" * 300)
        # stderr has no truncation banner; only the 100-byte tail is shown.
        assert result.count("z") == 100

    def test_stream_ring_buffer_derives_from_stdout_cap(self, monkeypatch):
        monkeypatch.setenv("BOOLEY_MCP_MAX_STDOUT_BYTES", "100")
        monkeypatch.setenv("BOOLEY_MCP_MAX_STDERR_BYTES", "25")
        assert mcp_server._stdout_cap_bytes() == 400  # 4x resolved formatter cap
        assert mcp_server._stderr_cap_bytes() == 100

    def test_defaults_without_env(self, monkeypatch):
        monkeypatch.delenv("BOOLEY_MCP_MAX_STDOUT_BYTES", raising=False)
        monkeypatch.delenv("BOOLEY_MCP_MAX_STDERR_BYTES", raising=False)
        assert mcp_server._max_stdout_bytes() == 12_000
        assert mcp_server._max_stderr_bytes() == 4_000

    def test_invalid_and_nonpositive_values_fall_back(self, monkeypatch):
        monkeypatch.setenv("BOOLEY_MCP_MAX_STDOUT_BYTES", "not-a-number")
        monkeypatch.setenv("BOOLEY_MCP_MAX_STDERR_BYTES", "0")
        assert mcp_server._max_stdout_bytes() == 12_000
        assert mcp_server._max_stderr_bytes() == 4_000


def _seed_report(tmp_path: Path, endpoint: str, report: dict) -> Path:
    """Write a flat + numbered endpoint report under a fresh flow-reports tree."""
    reports = tmp_path / "flow-reports"
    (reports / endpoint / "1").mkdir(parents=True, exist_ok=True)
    text = json.dumps(report)
    (reports / f"{endpoint}.json").write_text(text, encoding="utf-8")
    (reports / endpoint / "1" / "report.json").write_text(text, encoding="utf-8")
    return reports


def _utc_stamp(offset_s: float = 0.0) -> str:
    """A record/report-format UTC stamp offset from now (for freshness tests)."""
    from datetime import UTC, datetime, timedelta

    return (datetime.now(UTC) + timedelta(seconds=offset_s)).strftime(
        "%Y-%m-%dT%H:%M:%SZ",
    )


@pytest.fixture()
def _report_env(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BOOLEY_LOGS_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("BOOLEY_RUNTIME_DIR", str(tmp_path))
    # Slot store is project-scoped in production; pin it to tmp for tests.
    monkeypatch.setenv("BOOLEY_SLOTS_DIR", str(tmp_path / "jobs" / "slots"))
    return tmp_path


class TestReportFetch:
    """Out-of-band report retrieval (recover a run after a client timeout)."""

    def test_latest_report_by_endpoint(self, _report_env):
        _seed_report(
            _report_env,
            "sim",
            {"flow": "sim", "passed": True, "report_text": "RESULT: PASS"},
        )
        report = _latest_report("sim")
        assert report is not None
        assert report["flow"] == "sim"
        assert report["report_text"] == "RESULT: PASS"

    def test_latest_report_missing_endpoint_returns_none(self, _report_env):
        _seed_report(_report_env, "sim", {"flow": "sim"})
        assert _latest_report("lint") is None

    def test_latest_report_no_reports_dir(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BOOLEY_LOGS_DIR", str(tmp_path / "logs"))
        monkeypatch.setenv("BOOLEY_RUNTIME_DIR", str(tmp_path))
        assert _latest_report("sim") is None

    def test_available_report_endpoints(self, _report_env):
        _seed_report(_report_env, "sim", {"flow": "sim"})
        _seed_report(_report_env, "lint", {"flow": "lint"})
        assert _available_report_endpoints() == ["lint", "sim"]

    def test_format_report_card_with_body(self):
        card = _format_report_card(
            {
                "flow": "sim",
                "target": "lite",
                "passed": False,
                "report_text": "RESULT: FAIL (0/1 targets)",
            },
            "sim",
        )
        assert "flow: sim" in card
        assert "target: lite" in card
        assert "--- report ---" in card
        assert "RESULT: FAIL" in card

    def test_format_report_card_not_found_hints_available(self, _report_env):
        _seed_report(_report_env, "sim", {"flow": "sim"})
        card = _format_report_card(None, "lint")
        assert "No completed run report found" in card
        assert "sim" in card  # hint lists what IS on disk

    def test_dispatch_report_reads_endpoint_arg(self, _report_env):
        _seed_report(_report_env, "sim", {"flow": "sim", "report_text": "RESULT: PASS"})
        out = _dispatch_report({"endpoint": "sim"})
        blocks = out[0] if isinstance(out, tuple) else out
        assert len(blocks) == 1
        assert "RESULT: PASS" in blocks[0].text

    def test_dispatch_report_blank_endpoint_is_global(self, _report_env):
        _seed_report(_report_env, "sim", {"flow": "sim"})
        # Blank string is treated as "no filter" (most recent across all endpoints).
        out = _dispatch_report({"endpoint": "   "})
        blocks = out[0] if isinstance(out, tuple) else out
        assert "flow: sim" in blocks[0].text

    def test_report_mcp_tool_visible_interactive(self, monkeypatch):
        monkeypatch.setenv("BOOLEY_MCP_MODE", "interactive")
        monkeypatch.delenv("BOOLEY_MCP_TOOLS", raising=False)
        assert _report_mcp_tool_visible() is True
        assert _report_mcp_tool_def() is not None

    def test_report_mcp_tool_def_hidden_when_not_visible(self, monkeypatch):
        monkeypatch.delenv("BOOLEY_MCP_MODE", raising=False)
        monkeypatch.setenv("BOOLEY_MCP_TOOLS", "sim")  # explicit allowlist
        assert _report_mcp_tool_visible() is False
        assert _report_mcp_tool_def() is None


class TestStructuredContent:
    """structuredContent attachment via the SDK's (blocks, dict) tuple return.

    Additive and best-effort: the text card the agent already parses stays
    byte-identical, the structured dict rides alongside; any failure to
    build it degrades to text-only, never an error.
    """

    def test_dispatch_report_returns_tuple_with_structured(self, _report_env):
        _seed_report(
            _report_env,
            "sim",
            {"flow": "sim", "passed": True, "exit_code": 0},
        )
        out = _dispatch_report({"endpoint": "sim"})
        assert isinstance(out, tuple)
        blocks, structured = out
        assert structured["passed"] is True
        assert structured["reports"][0]["flow"] == "sim"
        # Text card is byte-identical to the plain (pre-structured) rendering.
        assert blocks[0].text == _format_report_card(_latest_report("sim"), "sim")

    def test_dispatch_report_without_report_stays_text_only(self, _report_env):
        out = _dispatch_report({"endpoint": "lint"})  # nothing seeded for lint
        assert isinstance(out, list)
        assert "No completed run report found" in out[0].text

    def test_oversized_report_reduced_to_scalar_verdict(self):
        big = {
            "flow": "sim",
            "target": "default",
            "passed": False,
            "exit_code": 1,
            "report_text": "x" * (70 * 1024),  # past the 64KB attach cap
        }
        payload = mcp_server._structured_from_report(big)
        assert payload == {
            "reports": [],
            "truncated": True,
            "flow": "sim",
            "target": "default",
            "exit_code": 1,
            "passed": False,
        }

    def test_oversized_report_keeps_the_artifact_pointers(self):
        """Truncation must not strip the paths.

        It fires on exactly the big, noisy runs where the agent most needs to
        open the log; keeping the verdict and dropping the pointers leaves it
        with a bare FAIL and nowhere to go. The block is a few hundred bytes.
        """
        big = {
            "flow": "sim",
            "target": "default",
            "passed": False,
            "exit_code": 1,
            "artifacts": {"log": "build/run.log"},
            "report_text": "x" * (70 * 1024),
        }
        payload = mcp_server._structured_from_report(big)
        assert payload["truncated"] is True
        assert payload["reports"] == []
        assert payload["artifacts"] == {"log": "build/run.log"}

    def test_oversized_report_finds_artifacts_nested_in_detail(self):
        """The flat report the MCP layer usually reads carries the block under
        ``detail`` (that is where ``base.write_report`` puts a endpoint's detail)."""
        big = {
            "flow": "lint",
            "exit_code": 1,
            "detail": {"artifacts": {"report": "reports/lint_report.json"}},
            "report_text": "x" * (70 * 1024),
        }
        payload = mcp_server._structured_from_report(big)
        assert payload["artifacts"] == {"report": "reports/lint_report.json"}

    def test_oversized_report_finds_artifacts_nested_one_level_in_detail(self):
        """A multi-target endpoint keys its detail by target and hangs the block
        off each entry (asic_synthesize's aggregate shape). Without the
        one-level scan the rescue found nothing for exactly the endpoints whose
        reports grow big enough to be truncated."""
        big = {
            "flow": "synth",
            "exit_code": 1,
            "detail": {
                "targets": ["lite", "heavy"],
                "lite": {"passed": True, "artifacts": {"log": "a/run.log"}},
                "heavy": {"passed": False, "artifacts": {"log": "b/run.log"}},
            },
            "report_text": "x" * (70 * 1024),
        }
        payload = mcp_server._structured_from_report(big)
        assert payload["artifacts"] == {
            "lite": {"log": "a/run.log"},
            "heavy": {"log": "b/run.log"},
        }

    def test_report_artifacts_ignores_nested_entries_without_a_block(self):
        """``detail`` holds plenty of non-artifact dicts (per_clock, evidence,
        complexity); none of them may leak into the rescue payload."""
        report = {
            "detail": {
                "per_clock": {"clk": {"fmax_mhz": 250.0}},
                "lite": {"artifacts": {"log": "a/run.log"}},
            }
        }
        assert mcp_server._report_artifacts(report) == {"lite": {"log": "a/run.log"}}

    def test_oversized_report_without_artifacts_omits_the_key(self):
        big = {"flow": "sim", "exit_code": 1, "report_text": "x" * (70 * 1024)}
        payload = mcp_server._structured_from_report(big)
        assert "artifacts" not in payload

    def test_report_artifacts_survives_a_malformed_report(self):
        """Runs on an already-oversized payload — an exception here would cost
        the agent the text card too."""
        assert mcp_server._report_artifacts({"detail": "not-a-dict"}) == {}
        assert mcp_server._report_artifacts({"artifacts": ["not", "a", "dict"]}) == {}
        assert mcp_server._report_artifacts({}) == {}

    def test_unserializable_report_degrades_to_text_only(self):
        content = [mcp_server.TextContent(type="text", text="CARD")]
        out = mcp_server._with_structured_report(content, {"flow": object()})
        assert out is content  # untouched list — never raises, never a tuple

    def test_none_and_empty_reports_attach_nothing(self):
        assert mcp_server._structured_from_report(None) is None
        assert mcp_server._structured_from_report({}) is None

    def test_job_result_attaches_run_id_fresh_report(self, _report_env, monkeypatch):
        # The fake child stamps its report with BOOLEY_RUN_ID like a real
        # endpoint run — identity freshness, no time-window race in the test.
        async def _run(cmd, *, timeout, on_spawn=None, env=None, **_kwargs):
            if on_spawn:
                on_spawn(4242)
            _seed_report(
                _report_env,
                "sim",
                {"flow": "sim", "passed": True, "run_id": env["BOOLEY_RUN_ID"]},
            )
            return (0, "SIM OK", "", False)

        monkeypatch.setattr(mcp_server, "_run_subprocess", _run)
        monkeypatch.setattr(mcp_server, "_job_inline_wait_seconds", lambda: 5.0)

        async def scenario():
            jobs = _JobManager(_FakeLifetime())
            return await _dispatch_async_job("sim", ["c"], 60, jobs)

        import asyncio

        out = asyncio.run(scenario())
        assert isinstance(out, tuple)
        assert out[1]["passed"] is True
        assert out[1]["reports"][0]["run_id"]
        # Same text card as before structured attachment existed.
        assert "EXIT_CODE: 0" in _text(out)
        assert "SIM OK" in _text(out)

    def test_job_result_with_stale_report_stays_text_only(self, _report_env, monkeypatch):
        # A report predating the job (no run_id, old timestamp) is not fresh:
        # the structured verdict must vouch for THIS run, so nothing attaches.
        _seed_report(
            _report_env,
            "sim",
            {"flow": "sim", "passed": True, "timestamp": _utc_stamp(-3600)},
        )

        async def _run(cmd, *, timeout, on_spawn=None, **_kwargs):
            if on_spawn:
                on_spawn(4242)
            return (0, "SIM OK", "", False)

        monkeypatch.setattr(mcp_server, "_run_subprocess", _run)
        monkeypatch.setattr(mcp_server, "_job_inline_wait_seconds", lambda: 5.0)

        async def scenario():
            jobs = _JobManager(_FakeLifetime())
            return await _dispatch_async_job("sim", ["c"], 60, jobs)

        import asyncio

        out = asyncio.run(scenario())
        assert isinstance(out, list)


class _FakeLifetime:
    """Minimal stand-in tracking the busy counter the JobManager holds."""

    def __init__(self) -> None:
        self.inflight = 0

    def mark_mcp_endpoint_start(self) -> None:
        self.inflight += 1

    def mark_mcp_endpoint_end(self) -> None:
        self.inflight -= 1


def _blocks(result) -> list:
    """Text blocks of a dispatch result — plain list or (blocks, structured)."""
    return result[0] if isinstance(result, tuple) else result


def _structured(result) -> dict | None:
    """structuredContent dict of a dispatch result, or None when text-only."""
    return result[1] if isinstance(result, tuple) else None


def _text(result) -> str:
    return _blocks(result)[0].text


def _run_id_from(text: str) -> str:
    """Extract the run_id a detach/attach handoff message carries.

    ``_JobManager.active_run_id`` is gone (ADR 0028: no in-server admission
    slot), so tests recover the id the way an agent does — from the message.
    """
    match = re.search(r"run_id=([A-Za-z0-9_-]+)", text)
    assert match is not None, f"no run_id in message: {text!r}"
    return match.group(1)


class TestAsyncJobDispatch:
    """The submit/poll job model for heavy endpoints (ADR 0027)."""

    def _fast_subprocess(self, exit_code=0, stdout="SIM OK"):
        async def _run(cmd, *, timeout, on_spawn=None, **_kwargs):
            if on_spawn:
                on_spawn(4242)
            return (exit_code, stdout, "", False)

        return _run

    def test_submit_finishing_inline_returns_full_result(
        self,
        _report_env,
        monkeypatch,
    ):
        # A run that completes within the inline window behaves exactly like the
        # old synchronous path: full EXIT_CODE + stdout, no run_id handoff.
        monkeypatch.setattr(mcp_server, "_run_subprocess", self._fast_subprocess())
        monkeypatch.setattr(mcp_server, "_job_inline_wait_seconds", lambda: 5.0)

        async def scenario():
            jobs = _JobManager(_FakeLifetime())
            out = await _dispatch_async_job("sim", ["c"], 60, jobs)
            return _text(out)

        import asyncio

        text = asyncio.run(scenario())
        assert "EXIT_CODE: 0" in text
        assert "SIM OK" in text
        assert "RUNNING" not in text

    def test_slow_submit_detaches_then_poll_resolves(
        self,
        _report_env,
        monkeypatch,
    ):
        import asyncio

        release = asyncio.Event()

        async def _slow(cmd, *, timeout, on_spawn=None, **_kwargs):
            if on_spawn:
                on_spawn(4242)
            await release.wait()
            return (0, "DONE", "", False)

        _seed_report(_report_env, "sim", {"flow": "sim", "report_text": "RESULT: PASS"})
        monkeypatch.setattr(mcp_server, "_run_subprocess", _slow)
        monkeypatch.setattr(mcp_server, "_job_inline_wait_seconds", lambda: 0.0)
        monkeypatch.setattr(mcp_server, "_job_poll_wait_seconds", lambda: 0.05)

        async def scenario():
            life = _FakeLifetime()
            jobs = _JobManager(life)
            submit_out = await _dispatch_async_job("sim", ["c"], 60, jobs)
            # Detached: RUNNING handoff, server held busy for the background run.
            assert "RUNNING" in _text(submit_out)
            run_id = _run_id_from(_text(submit_out))
            assert life.inflight == 1

            poll_running = await _dispatch_poll({"run_id": run_id}, jobs)
            assert "RUNNING" in _text(poll_running)

            release.set()
            poll_done = await _dispatch_poll({"run_id": run_id}, jobs)
            assert life.inflight == 0
            return _text(poll_done)

        text = asyncio.run(scenario())
        assert "EXIT_CODE: 0" in text
        assert "DONE" in text

    def test_asic_synthesize_slow_submit_detaches_then_poll_resolves(
        self,
        _report_env,
        monkeypatch,
    ):
        # Issue 2 (booley_asic_synthesize_issues): a big synth run outlasts the
        # MCP client budget. It must detach (RUNNING + run_id) and resolve via
        # booley_poll, redeeming this run's freshness-stamped report — not just
        # simulate. Guards the report/timestamp seam for asic_synthesize.
        import asyncio

        release = asyncio.Event()

        async def _slow(cmd, *, timeout, on_spawn=None, **_kwargs):
            if on_spawn:
                on_spawn(4242)
            await release.wait()
            return (0, "SYNTH DONE", "", False)

        # A synth report stamped inside the job's lifetime window, so the poll's
        # freshness gate vouches for it (report_ts >= started_at).
        _seed_report(
            _report_env,
            "synth",
            {
                "flow": "synth",
                "exit_code": 0,
                "timestamp": _utc_stamp(5),
                "report_text": "RESULT: PASS",
            },
        )
        monkeypatch.setattr(mcp_server, "_run_subprocess", _slow)
        monkeypatch.setattr(mcp_server, "_job_inline_wait_seconds", lambda: 0.0)
        monkeypatch.setattr(mcp_server, "_job_poll_wait_seconds", lambda: 0.05)

        async def scenario():
            life = _FakeLifetime()
            jobs = _JobManager(life)
            submit_out = await _dispatch_async_job(
                "synth",
                ["--target", "lite"],
                60,
                jobs,
            )
            assert "RUNNING" in _text(submit_out)
            run_id = _run_id_from(_text(submit_out))
            assert life.inflight == 1

            release.set()
            poll_done = await _dispatch_poll({"run_id": run_id}, jobs)
            assert life.inflight == 0
            return _text(poll_done)

        text = asyncio.run(scenario())
        assert "EXIT_CODE: 0" in text
        assert "SYNTH DONE" in text
        assert "RESULT: PASS" in text  # this run's freshness-gated report

    def test_second_heavy_endpoint_spawns_and_detaches(
        self,
        _report_env,
        monkeypatch,
    ):
        # ADR 0028: admission moved into the spawned child (on-disk slot
        # store), so the server no longer refuses a second heavy submit — it
        # spawns it too and the child queues for its class slot. The agent
        # gets a real run_id handoff, never BLOCKED.
        import asyncio

        release = asyncio.Event()

        async def _slow(cmd, *, timeout, on_spawn=None, **_kwargs):
            if on_spawn:
                on_spawn(4242)
            await release.wait()
            return (0, "DONE", "", False)

        monkeypatch.setattr(mcp_server, "_run_subprocess", _slow)
        monkeypatch.setattr(mcp_server, "_job_inline_wait_seconds", lambda: 0.0)

        async def scenario():
            jobs = _JobManager(_FakeLifetime())
            first = await _dispatch_async_job("sim", ["c"], 60, jobs)
            assert "RUNNING" in _text(first)
            second = await _dispatch_async_job("fpga", ["c"], 60, jobs)
            release.set()
            # Drain both jobs so the manager releases its lifetime holds.
            await jobs.wait(_run_id_from(_text(first)), 1.0)
            await jobs.wait(_run_id_from(_text(second)), 1.0)
            return _text(first), _text(second)

        first_text, second_text = asyncio.run(scenario())
        assert "BLOCKED" not in second_text
        assert "RUNNING" in second_text
        # A genuinely new job, not an attach onto the first one.
        assert _run_id_from(second_text) != _run_id_from(first_text)

    def test_duplicate_submit_attaches_to_inflight_run(
        self,
        _report_env,
        monkeypatch,
    ):
        # Idempotent attach: an identical re-submit (agent lost the handle to
        # its first call) joins the in-flight job via its durable record
        # (_find_attachable_job) instead of spawning a duplicate.
        # derive_status is pinned to the recorded status because a real
        # PID/argv identity match needs a live child we don't spawn here.
        import asyncio

        release = asyncio.Event()

        async def _slow(cmd, *, timeout, on_spawn=None, **_kwargs):
            if on_spawn:
                on_spawn(4242)
            await release.wait()
            return (0, "DONE", "", False)

        monkeypatch.setattr(mcp_server, "_run_subprocess", _slow)
        monkeypatch.setattr(mcp_server, "_job_inline_wait_seconds", lambda: 0.0)
        monkeypatch.setattr(
            mcp_server.jobrec,
            "derive_status",
            lambda rec, alive, **kw: rec.status,
        )

        async def scenario():
            jobs = _JobManager(_FakeLifetime())
            first = await _dispatch_async_job("sim", ["c"], 60, jobs)
            assert "RUNNING" in _text(first)
            run_id = _run_id_from(_text(first))

            dup = await _dispatch_async_job("sim", ["c"], 60, jobs)
            text = _text(dup)
            assert "BLOCKED" not in text
            assert "attached" in text
            assert run_id in text  # same job, no fresh run_id

            release.set()
            await jobs.wait(run_id, 1.0)
            return text

        asyncio.run(scenario())

    def test_duplicate_submit_different_argv_spawns_new_job(
        self,
        _report_env,
        monkeypatch,
    ):
        # Attach is for *identical* calls only: same endpoint with different argv
        # is a genuinely new run — ADR 0028 spawns it too and lets the child
        # queue for its class slot instead of refusing with BLOCKED.
        import asyncio

        release = asyncio.Event()

        async def _slow(cmd, *, timeout, on_spawn=None, **_kwargs):
            if on_spawn:
                on_spawn(4242)
            await release.wait()
            return (0, "DONE", "", False)

        monkeypatch.setattr(mcp_server, "_run_subprocess", _slow)
        monkeypatch.setattr(mcp_server, "_job_inline_wait_seconds", lambda: 0.0)

        async def scenario():
            jobs = _JobManager(_FakeLifetime())
            first = await _dispatch_async_job("sim", ["c"], 60, jobs)
            second = await _dispatch_async_job("sim", ["--other"], 60, jobs)
            release.set()
            await jobs.wait(_run_id_from(_text(first)), 1.0)
            await jobs.wait(_run_id_from(_text(second)), 1.0)
            return _text(first), _text(second)

        first_text, second_text = asyncio.run(scenario())
        assert "BLOCKED" not in second_text
        assert "RUNNING" in second_text
        assert _run_id_from(second_text) != _run_id_from(first_text)

    def test_poll_wait_seconds_long_poll_returns_result(
        self,
        _report_env,
        monkeypatch,
    ):
        # Long-poll (ADR 0027 amendment 2026-07-06): a poll carrying
        # wait_seconds blocks for the running job and returns the final result
        # when it finishes inside the window — one call instead of a poll-loop.
        import asyncio

        release = asyncio.Event()

        async def _slow(cmd, *, timeout, on_spawn=None, **_kwargs):
            if on_spawn:
                on_spawn(4242)
            await release.wait()
            return (0, "DONE", "", False)

        monkeypatch.setattr(mcp_server, "_run_subprocess", _slow)
        monkeypatch.setattr(mcp_server, "_job_inline_wait_seconds", lambda: 0.0)
        # Server default wait pinned to 0 so any blocking observed below is
        # attributable to the wait_seconds argument alone.
        monkeypatch.setattr(mcp_server, "_job_poll_wait_seconds", lambda: 0.0)

        async def scenario():
            jobs = _JobManager(_FakeLifetime())
            submit_out = await _dispatch_async_job("sim", ["c"], 60, jobs)
            run_id = _run_id_from(_text(submit_out))
            # Job finishes shortly after the long-poll starts waiting.
            asyncio.get_running_loop().call_later(0.05, release.set)
            out = await _dispatch_poll({"run_id": run_id, "wait_seconds": 10}, jobs)
            return _text(out)

        text = asyncio.run(scenario())
        assert "EXIT_CODE: 0" in text
        assert "DONE" in text
        assert "RUNNING" not in text

    def test_poll_wait_seconds_zero_returns_running_immediately(
        self,
        _report_env,
        monkeypatch,
    ):
        # wait_seconds=0 overrides the server default wait and answers with the
        # current status at once — the job releases only 0.2s later, so a leaked
        # default wait (pinned to 5s here) would return the result instead.
        import asyncio

        release = asyncio.Event()

        async def _slow(cmd, *, timeout, on_spawn=None, **_kwargs):
            if on_spawn:
                on_spawn(4242)
            await release.wait()
            return (0, "DONE", "", False)

        monkeypatch.setattr(mcp_server, "_run_subprocess", _slow)
        monkeypatch.setattr(mcp_server, "_job_inline_wait_seconds", lambda: 0.0)
        monkeypatch.setattr(mcp_server, "_job_poll_wait_seconds", lambda: 5.0)

        async def scenario():
            jobs = _JobManager(_FakeLifetime())
            submit_out = await _dispatch_async_job("sim", ["c"], 60, jobs)
            run_id = _run_id_from(_text(submit_out))
            asyncio.get_running_loop().call_later(0.2, release.set)
            out = await _dispatch_poll({"run_id": run_id, "wait_seconds": 0}, jobs)
            text = _text(out)
            # Let the job drain so the JobManager releases its lifetime hold.
            release.set()
            await jobs.wait(run_id, 1.0)
            return text

        text = asyncio.run(scenario())
        assert "RUNNING" in text
        assert "EXIT_CODE" not in text

    def test_requested_poll_wait_seconds_clamps_and_falls_back(self, monkeypatch):
        # Explicit values clamp to [0, 270]; absent/garbage falls back to the
        # server default (env knob), and bool is rejected as a caller bug.
        monkeypatch.setattr(mcp_server, "_job_poll_wait_seconds", lambda: 7.0)
        resolve = mcp_server._requested_poll_wait_seconds
        assert resolve({"wait_seconds": 999}) == 270.0
        assert resolve({"wait_seconds": -3}) == 0.0
        assert resolve({"wait_seconds": 25}) == 25.0
        assert resolve({}) == 7.0
        assert resolve({"wait_seconds": "10"}) == 7.0
        assert resolve({"wait_seconds": True}) == 7.0

    def test_poll_endpoint_schema_advertises_wait_seconds(self, monkeypatch):
        # An agent discovers long-poll through the schema; keep it advertised.
        monkeypatch.setenv("BOOLEY_MCP_MODE", "interactive")
        endpoint_def = _poll_mcp_tool_def()
        assert endpoint_def is not None
        description = endpoint_def["description"]
        assert "Claude and Codex:" in description
        assert "omit 'wait_seconds'" in description
        assert "same running cell" in description
        assert "do not start another booley_poll call" in description
        assert "wait_seconds=240" not in description
        assert "yield_time_ms" not in description
        props = endpoint_def["schema"]["properties"]
        assert props["wait_seconds"]["type"] == "integer"
        assert props["wait_seconds"]["minimum"] == 0
        assert props["wait_seconds"]["maximum"] == 270
        default_wait_seconds = mcp_server._DEFAULT_JOB_POLL_WAIT_SECONDS
        assert f"{default_wait_seconds:g}s" in props["wait_seconds"]["description"]
        assert "wait_seconds" not in endpoint_def["schema"]["required"]

    def test_poll_missing_run_id(self, _report_env):
        import asyncio

        jobs = _JobManager(_FakeLifetime())
        out = asyncio.run(_dispatch_poll({}, jobs))
        assert "Provide a 'run_id'" in _text(out)

    def test_poll_unknown_run_id(self, _report_env):
        import asyncio

        jobs = _JobManager(_FakeLifetime())
        out = asyncio.run(_dispatch_poll({"run_id": "nope-1"}, jobs))
        assert "Unknown run_id" in _text(out)

    def test_poll_reconnect_terminal_from_disk(self, _report_env):
        # A fresh server (new JobManager, empty in-memory registry) still
        # resolves a finished job from its durable on-disk record.
        import asyncio

        jobrec.write_record(
            jobrec.JobRecord(
                run_id="simulate-x-1",
                endpoint="sim",
                started_at="t",
                timeout_s=60,
                pid=4242,
                status=jobrec.STATUS_DONE,
                exit_code=0,
            )
        )
        _seed_report(_report_env, "sim", {"flow": "sim", "report_text": "RESULT: PASS"})
        jobs = _JobManager(_FakeLifetime())
        out = asyncio.run(_dispatch_poll({"run_id": "simulate-x-1"}, jobs))
        text = _text(out)
        assert "EXIT_CODE: 0" in text
        assert "RESULT: PASS" in text

    def test_poll_reconnect_running_dead_pid_is_failed(self, _report_env):
        # A record left 'running' after a restart, whose PID is gone, resolves
        # as failed rather than a ghost that polls forever.
        import asyncio

        jobrec.write_record(
            jobrec.JobRecord(
                run_id="simulate-x-2",
                endpoint="sim",
                started_at="t",
                timeout_s=60,
                pid=2_000_000_000,
                status=jobrec.STATUS_RUNNING,
            )
        )
        jobs = _JobManager(_FakeLifetime())
        out = asyncio.run(_dispatch_poll({"run_id": "simulate-x-2"}, jobs))
        assert "EXIT_CODE: 2" in _text(out)

    def test_poll_orphan_with_fresh_report_adopts_outcome(self, _report_env):
        # Server SIGKILLed mid-run, child survived, finished, wrote its report,
        # then exited: the record never went terminal. The fresh report proves
        # the run completed — its exit code wins over a blanket EXIT_CODE: 2.
        import asyncio

        jobrec.write_record(
            jobrec.JobRecord(
                run_id="simulate-x-5",
                endpoint="sim",
                started_at=_utc_stamp(-30),
                timeout_s=600,
                pid=2_000_000_000,
                status=jobrec.STATUS_RUNNING,
            )
        )
        _seed_report(
            _report_env,
            "sim",
            {
                "flow": "sim",
                "exit_code": 0,
                "timestamp": _utc_stamp(-5),
                "report_text": "RESULT: PASS",
            },
        )
        jobs = _JobManager(_FakeLifetime())
        text = _text(asyncio.run(_dispatch_poll({"run_id": "simulate-x-5"}, jobs)))
        assert "EXIT_CODE: 0" in text
        assert "RESULT: PASS" in text

    def test_poll_drops_report_predating_the_job(self, _report_env):
        # A run that died before writing anything must not be dressed up with
        # the previous run's report of the same endpoint.
        import asyncio

        jobrec.write_record(
            jobrec.JobRecord(
                run_id="simulate-x-6",
                endpoint="sim",
                started_at=_utc_stamp(-50),
                timeout_s=600,
                pid=4242,
                status=jobrec.STATUS_FAILED,
                exit_code=1,
            )
        )
        _seed_report(
            _report_env,
            "sim",
            {
                "flow": "sim",
                "exit_code": 0,
                "timestamp": _utc_stamp(-3600),
                "report_text": "RESULT: PASS",
            },
        )
        jobs = _JobManager(_FakeLifetime())
        text = _text(asyncio.run(_dispatch_poll({"run_id": "simulate-x-6"}, jobs)))
        assert "EXIT_CODE: 1" in text
        assert "RESULT: PASS" not in text

    def test_poll_report_after_job_deadline_cannot_vouch(self, _report_env):
        # A report written long past the job's watchdog window belongs to a
        # LATER run of the same endpoint — it must not rescue this (dead) job to
        # its exit code, nor be attached as this job's result.
        import asyncio

        jobrec.write_record(
            jobrec.JobRecord(
                run_id="simulate-x-8",
                endpoint="sim",
                started_at=_utc_stamp(-7200),
                timeout_s=60,
                pid=2_000_000_000,
                status=jobrec.STATUS_RUNNING,
            )
        )
        _seed_report(
            _report_env,
            "sim",
            {
                "flow": "sim",
                "exit_code": 0,
                "timestamp": _utc_stamp(-5),
                "report_text": "RESULT: PASS",
            },
        )
        jobs = _JobManager(_FakeLifetime())
        text = _text(asyncio.run(_dispatch_poll({"run_id": "simulate-x-8"}, jobs)))
        assert "EXIT_CODE: 2" in text
        assert "RESULT: PASS" not in text

    def test_poll_live_pid_past_deadline_resolves_failed(self, _report_env):
        # PID-reuse ghost: the recorded PID is alive (it's ours!) but the job's
        # watchdog budget is long gone — the poll must go terminal, not RUNNING.
        import asyncio
        import os

        jobrec.write_record(
            jobrec.JobRecord(
                run_id="simulate-x-7",
                endpoint="sim",
                started_at=_utc_stamp(-7200),
                timeout_s=60,
                pid=os.getpid(),
                status=jobrec.STATUS_RUNNING,
            )
        )
        jobs = _JobManager(_FakeLifetime())
        text = _text(asyncio.run(_dispatch_poll({"run_id": "simulate-x-7"}, jobs)))
        assert "RUNNING" not in text
        assert "EXIT_CODE: 2" in text

    def test_poll_endpoint_always_visible(self, monkeypatch):
        # Unlike booley_report, poll must be reachable in Ticket-Mode too.
        monkeypatch.delenv("BOOLEY_MCP_MODE", raising=False)
        monkeypatch.delenv("BOOLEY_NESTED_AGENT", raising=False)
        monkeypatch.setenv("BOOLEY_MCP_TOOLS", "sim")  # explicit allowlist
        assert _poll_mcp_tool_visible() is True
        assert _poll_mcp_tool_def() is not None


class TestAttachSurvivesRestart:
    """A restarted server re-attaches duplicate submits from disk records.

    The in-server admission slot is gone (ADR 0028): what must survive a
    restart is idempotent attach — an identical re-submit joining the still
    live detached child via its durable record (``_find_attachable_job``)
    instead of double-spawning a multi-minute EDA run.
    """

    def test_duplicate_submit_attaches_to_surviving_job(
        self,
        _report_env,
        monkeypatch,
    ):
        # The lost-handle retry across a server restart: identical argv joins
        # the surviving job (resolved from its durable record) instead of
        # spawning a duplicate. derive_status is pinned RUNNING because a
        # real /proc argv-identity match needs a live child we don't spawn.
        import asyncio
        import os

        monkeypatch.setattr(
            mcp_server.jobrec,
            "derive_status",
            lambda rec, alive, **kw: jobrec.STATUS_RUNNING,
        )
        # The adopted-job path now honors the inline wait as a disk long-poll
        # (pinned RUNNING would otherwise block the full 240s default here).
        monkeypatch.setattr(mcp_server, "_job_inline_wait_seconds", lambda: 0.0)
        jobrec.write_record(
            jobrec.JobRecord(
                run_id="simulate-y-5",
                endpoint="sim",
                started_at=_utc_stamp(-10),
                timeout_s=600,
                pid=os.getpid(),
                status=jobrec.STATUS_RUNNING,
                argv=["c"],
            )
        )
        assert mcp_server._find_attachable_job("sim", ["c"]) == "simulate-y-5"
        # Fresh manager = restarted server: no in-memory task for the job.
        jobs = _JobManager(_FakeLifetime())
        out = asyncio.run(_dispatch_async_job("sim", ["c"], 60, jobs))
        text = _text(out)
        assert "BLOCKED" not in text
        assert "RUNNING" in text
        assert "simulate-y-5" in text  # attached to the survivor, no new run_id

    def test_dead_pid_record_is_not_attachable(self, _report_env):
        # A ghost record (dead PID) must not capture the new submit even
        # though its raw status still says running.
        jobrec.write_record(
            jobrec.JobRecord(
                run_id="simulate-y-2",
                endpoint="sim",
                started_at=_utc_stamp(-10),
                timeout_s=600,
                pid=2_000_000_000,
                status=jobrec.STATUS_RUNNING,
                argv=["c"],
            )
        )
        assert mcp_server._find_attachable_job("sim", ["c"]) is None

    def test_expired_deadline_record_is_not_attachable(self, _report_env):
        # PID-reuse ghost: live PID (ours) but the job's watchdog budget is
        # long gone — derive_status filters it out of the attach scan.
        import os

        jobrec.write_record(
            jobrec.JobRecord(
                run_id="simulate-y-3",
                endpoint="sim",
                started_at=_utc_stamp(-7200),
                timeout_s=60,
                pid=os.getpid(),
                status=jobrec.STATUS_RUNNING,
                argv=["c"],
            )
        )
        assert mcp_server._find_attachable_job("sim", ["c"]) is None

    def test_terminal_record_spawns_fresh_run(self, _report_env, monkeypatch):
        # A *finished* identical job is never replayed (the workspace may
        # have changed since) — the submit runs fresh instead of attaching.
        import asyncio
        import os

        jobrec.write_record(
            jobrec.JobRecord(
                run_id="simulate-y-4",
                endpoint="sim",
                started_at=_utc_stamp(-10),
                timeout_s=600,
                pid=os.getpid(),
                status=jobrec.STATUS_DONE,
                exit_code=0,
                argv=["c"],
            )
        )
        assert mcp_server._find_attachable_job("sim", ["c"]) is None

        async def _fast(cmd, *, timeout, on_spawn=None, **_kwargs):
            if on_spawn:
                on_spawn(4242)
            return (0, "FRESH RUN", "", False)

        monkeypatch.setattr(mcp_server, "_run_subprocess", _fast)
        monkeypatch.setattr(mcp_server, "_job_inline_wait_seconds", lambda: 5.0)
        jobs = _JobManager(_FakeLifetime())
        text = _text(asyncio.run(_dispatch_async_job("sim", ["c"], 60, jobs)))
        assert "FRESH RUN" in text  # a new run really executed
        assert "simulate-y-4" not in text  # not attached to the finished job


class TestCancel:
    """The synthetic booley_cancel endpoint stops queued and running jobs."""

    @pytest.fixture(autouse=True)
    def _short_process_grace(self, monkeypatch):
        # These tests own the child process, so a SIGTERM'ed child remains a
        # zombie until the assertion below calls wait().  A PID liveness probe
        # correctly continues to see that unreaped PID and would spend the full
        # five-second production cleanup grace in every case.  Keep the force-
        # kill branch covered without adding a deterministic 15 seconds to the
        # unit suite.
        monkeypatch.setattr(mcp_server, "_CANCEL_GRACE_SECONDS", 0.01)

    @staticmethod
    def _cancel(arguments):
        import asyncio

        jobs = _JobManager(_FakeLifetime())
        return asyncio.run(_dispatch_cancel(arguments, jobs))

    def test_missing_run_id(self, _report_env):
        out = self._cancel({})
        assert "Provide the 'run_id'" in _text(out)

    def test_unknown_run_id(self, _report_env):
        out = self._cancel({"run_id": "nope-1"})
        assert "Unknown run_id" in _text(out)

    def test_terminal_job_nothing_to_cancel(self, _report_env):
        jobrec.write_record(
            jobrec.JobRecord(
                run_id="simulate-c-1",
                endpoint="sim",
                started_at=_utc_stamp(-10),
                timeout_s=600,
                pid=4242,
                status=jobrec.STATUS_DONE,
                exit_code=0,
            )
        )
        out = self._cancel({"run_id": "simulate-c-1"})
        assert "already finished" in _text(out)

    def test_running_job_without_slot_entry_is_cancelled(self, _report_env):
        import subprocess

        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(300)"],
            start_new_session=sys.platform != "win32",
        )
        try:
            jobrec.write_record(
                jobrec.JobRecord(
                    run_id="simulate-c-2",
                    endpoint="sim",
                    started_at=_utc_stamp(-10),
                    timeout_s=600,
                    pid=child.pid,
                    status=jobrec.STATUS_RUNNING,
                )
            )
            out = self._cancel({"run_id": "simulate-c-2"})
            assert "CANCELLED: running job" in _text(out)
            child.wait(timeout=10)
            got = jobrec.read_record("simulate-c-2")
            assert got is not None and got.status == jobrec.STATUS_CANCELLED
            assert got.exit_code == 130
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)

    def test_slot_holder_is_cancelled(self, _report_env):
        import subprocess

        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(300)"],
            start_new_session=sys.platform != "win32",
        )
        try:
            jobrec.write_record(
                jobrec.JobRecord(
                    run_id="simulate-c-4",
                    endpoint="sim",
                    started_at=_utc_stamp(-10),
                    timeout_s=600,
                    pid=child.pid,
                    status=jobrec.STATUS_RUNNING,
                )
            )
            root = job_slots.slots_dir()
            assert root is not None
            store = job_slots.SlotStore(root)
            token = store.submit(job_slots.CLASS_HEAVY, pid=child.pid, argv=[])
            assert store.refresh(token).state == job_slots.HOLDING
            out = self._cancel({"run_id": "simulate-c-4"})
            assert "CANCELLED: running job" in _text(out)
            child.wait(timeout=10)
            got = jobrec.read_record("simulate-c-4")
            assert got is not None and got.status == jobrec.STATUS_CANCELLED
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)

    def test_queued_job_is_cancelled(self, _report_env):
        # A real queued child: a live sleeping process holding a waiter entry
        # in the slot store. Cancel SIGTERMs it and marks it cancelled.
        import signal as _signal
        import subprocess
        import sys

        # Cross-platform idle child: ``sleep`` is not a Windows command.
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(300)"],
            start_new_session=sys.platform != "win32",
        )
        try:
            jobrec.write_record(
                jobrec.JobRecord(
                    run_id="simulate-c-3",
                    endpoint="sim",
                    started_at=_utc_stamp(-10),
                    timeout_s=600,
                    pid=child.pid,
                    status=jobrec.STATUS_RUNNING,
                )
            )
            root = job_slots.slots_dir()
            assert root is not None
            # Waiter entry, never refreshed → stays queued at position 0.
            # argv=[] skips the /proc identity check for the fake claim.
            job_slots.SlotStore(root).submit(
                job_slots.CLASS_HEAVY,
                pid=child.pid,
                argv=[],
            )
            out = self._cancel({"run_id": "simulate-c-3"})
            text = _text(out)
            assert "CANCELLED" in text
            assert "withdrawn" in text
            # The queued child really stopped. POSIX exposes the terminating
            # signal; Windows taskkill uses its own non-zero exit status.
            returncode = child.wait(timeout=10)
            if sys.platform == "win32":
                assert returncode != 0
            else:
                assert returncode == -_signal.SIGTERM
            got = jobrec.read_record("simulate-c-3")
            assert got is not None
            assert got.status == jobrec.STATUS_CANCELLED
            assert got.exit_code == 130
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)

    def test_in_process_running_job_cancels_supervisor_and_persists_outcome(
        self,
        _report_env,
        monkeypatch,
    ):
        import asyncio

        async def scenario():
            started = asyncio.Event()

            async def _slow(_cmd, *, timeout, on_spawn=None, **_kwargs):
                if on_spawn:
                    on_spawn(4242)
                started.set()
                await asyncio.Event().wait()

            monkeypatch.setattr(mcp_server, "_run_subprocess", _slow)
            monkeypatch.setattr(
                jobrec,
                "derive_status",
                lambda _rec, _alive, **_kwargs: jobrec.STATUS_RUNNING,
            )
            jobs = _JobManager(_FakeLifetime())
            run_id = jobs.submit("sim", ["fake-endpoint"], 600)
            await started.wait()
            out = await _dispatch_cancel({"run_id": run_id}, jobs)
            return run_id, out, jobs

        run_id, out, jobs = asyncio.run(scenario())
        assert "CANCELLED: running job" in _text(out)
        record = jobrec.read_record(run_id)
        assert record is not None and record.status == jobrec.STATUS_CANCELLED
        assert "CANCELLED" in jobs.result_text(run_id)

    def test_server_shutdown_cancellation_is_not_mislabeled_as_user_cancel(
        self,
        _report_env,
        monkeypatch,
    ):
        import asyncio

        async def scenario():
            started = asyncio.Event()

            async def _slow(_cmd, *, timeout, on_spawn=None, **_kwargs):
                if on_spawn:
                    on_spawn(4242)
                started.set()
                await asyncio.Event().wait()

            monkeypatch.setattr(mcp_server, "_run_subprocess", _slow)
            jobs = _JobManager(_FakeLifetime())
            run_id = jobs.submit("sim", ["fake-endpoint"], 600)
            await started.wait()
            task = jobs._tasks[run_id]
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            return run_id

        import contextlib

        run_id = asyncio.run(scenario())
        record = jobrec.read_record(run_id)
        assert record is not None and record.status == jobrec.STATUS_RUNNING

    def test_cancel_endpoint_visible_wherever_poll_is(self, monkeypatch):
        # Like poll, cancel must be reachable in Ticket-Mode too: any server
        # that hands back a queued run_id must let the caller withdraw it.
        monkeypatch.delenv("BOOLEY_MCP_MODE", raising=False)
        monkeypatch.delenv("BOOLEY_NESTED_AGENT", raising=False)
        monkeypatch.setenv("BOOLEY_MCP_TOOLS", "sim")  # explicit allowlist
        endpoint_def = mcp_server._cancel_mcp_tool_def()
        assert endpoint_def is not None
        assert endpoint_def["name"] == "booley_cancel"
        assert endpoint_def["schema"]["required"] == ["run_id"]


class TestJobPhase:
    """Phase narration: a slot-store waiter reads QUEUED, everything else RUNNING."""

    def test_waiter_entry_reads_queued_with_position(self, _report_env):
        import os

        jobrec.write_record(
            jobrec.JobRecord(
                run_id="simulate-p-1",
                endpoint="sim",
                started_at=_utc_stamp(-5),
                timeout_s=600,
                pid=os.getpid(),
                status=jobrec.STATUS_RUNNING,
            )
        )
        root = job_slots.slots_dir()
        assert root is not None
        job_slots.SlotStore(root).submit(
            job_slots.CLASS_HEAVY,
            pid=os.getpid(),
            argv=[],
        )
        assert _job_phase("simulate-p-1") == "QUEUED (position 1)"

    def test_no_slot_entry_reads_running(self, _report_env):
        import os

        jobrec.write_record(
            jobrec.JobRecord(
                run_id="simulate-p-2",
                endpoint="sim",
                started_at=_utc_stamp(-5),
                timeout_s=600,
                pid=os.getpid(),
                status=jobrec.STATUS_RUNNING,
            )
        )
        assert _job_phase("simulate-p-2") == "RUNNING"

    def test_holder_entry_reads_running(self, _report_env):
        import os

        jobrec.write_record(
            jobrec.JobRecord(
                run_id="simulate-p-3",
                endpoint="sim",
                started_at=_utc_stamp(-5),
                timeout_s=600,
                pid=os.getpid(),
                status=jobrec.STATUS_RUNNING,
            )
        )
        root = job_slots.slots_dir()
        assert root is not None
        store = job_slots.SlotStore(root)
        token = store.submit(job_slots.CLASS_HEAVY, pid=os.getpid(), argv=[])
        assert store.refresh(token).state == job_slots.HOLDING
        assert _job_phase("simulate-p-3") == "RUNNING"


class TestAsyncJobToolMembership:
    """Which endpoints route through the submit/poll model (ADR 0027).

    Membership in ``_ASYNC_JOB_MCP_TOOLS`` is the *only* thing that makes a heavy
    endpoint detach instead of blocking the MCP client. Dropping a endpoint here
    silently reintroduces the client-budget timeout the model was built to fix
    (booley_asic_synthesize_issues #2), with no other test failing — so pin the
    set explicitly.
    """

    def test_heavy_tools_route_async(self):
        assert "synth" in mcp_server._ASYNC_JOB_MCP_TOOLS
        assert "fpga" in mcp_server._ASYNC_JOB_MCP_TOOLS
        assert "sim" in mcp_server._ASYNC_JOB_MCP_TOOLS
        # Added 2026-07-05: multi-minute Xcelium elaborates hit the same
        # client-cap → file-lock cascade as the original three.
        assert "elab" in mcp_server._ASYNC_JOB_MCP_TOOLS
        # Added 2026-07-06: the LLM specialists each drive a 20-30 min sub-agent
        # loop, so an interactive call came back as a bare client-side timeout
        # (no run_id, no poll) — recoverable only via booley_report.
        assert "reviewer" in mcp_server._ASYNC_JOB_MCP_TOOLS
        assert "mutation_tester" in mcp_server._ASYNC_JOB_MCP_TOOLS
        assert "coverage_analyst" in mcp_server._ASYNC_JOB_MCP_TOOLS

    def test_poll_endpoint_advertises_the_async_tools(self):
        # The booley_poll description names the endpoints it redeems; keep it and the
        # routing set from drifting apart. Read the constant directly so this
        # doesn't hinge on the endpoint's ambient visibility.
        desc = mcp_server._POLL_MCP_TOOL_DESCRIPTION
        for endpoint in mcp_server._ASYNC_JOB_MCP_TOOLS:
            assert endpoint in desc


class TestReconcileOrphanedJobs:
    """On server start, ghost 'running' records become terminal (ADR 0027)."""

    @pytest.fixture(autouse=True)
    def _top_level(self, monkeypatch):
        # Reconcile only runs in TOP_LEVEL (outer) bootstrap context.
        monkeypatch.delenv("BOOLEY_MCP_NESTED", raising=False)

    def test_dead_running_record_flipped_to_failed(self, _report_env):
        jobrec.write_record(
            jobrec.JobRecord(
                run_id="simulate-x-1",
                endpoint="sim",
                started_at="t",
                timeout_s=60,
                pid=2_000_000_000,
                status=jobrec.STATUS_RUNNING,
            )
        )
        mcp_server._reconcile_orphaned_jobs()
        got = jobrec.read_record("simulate-x-1")
        assert got is not None
        assert got.status == jobrec.STATUS_FAILED
        assert got.exit_code == 2

    def test_live_running_record_untouched(self, _report_env):
        import os

        jobrec.write_record(
            jobrec.JobRecord(
                run_id="simulate-x-2",
                endpoint="sim",
                started_at="t",
                timeout_s=60,
                pid=os.getpid(),
                status=jobrec.STATUS_RUNNING,
            )
        )
        mcp_server._reconcile_orphaned_jobs()
        got = jobrec.read_record("simulate-x-2")
        assert got is not None and got.status == jobrec.STATUS_RUNNING

    def test_terminal_record_untouched(self, _report_env):
        jobrec.write_record(
            jobrec.JobRecord(
                run_id="simulate-x-3",
                endpoint="sim",
                started_at="t",
                timeout_s=60,
                pid=2_000_000_000,
                status=jobrec.STATUS_DONE,
                exit_code=0,
            )
        )
        mcp_server._reconcile_orphaned_jobs()
        got = jobrec.read_record("simulate-x-3")
        assert got is not None and got.status == jobrec.STATUS_DONE
        assert got.exit_code == 0

    def test_dead_record_with_fresh_report_adopts_outcome(self, _report_env):
        # The run completed (fresh report on disk) but the server died before
        # recording: reconcile adopts the run's real exit code instead of
        # branding a finished run as failed.
        jobrec.write_record(
            jobrec.JobRecord(
                run_id="simulate-x-5",
                endpoint="sim",
                started_at=_utc_stamp(-30),
                timeout_s=600,
                pid=2_000_000_000,
                status=jobrec.STATUS_RUNNING,
            )
        )
        _seed_report(
            _report_env,
            "sim",
            {
                "flow": "sim",
                "exit_code": 0,
                "timestamp": _utc_stamp(-5),
                "report_text": "RESULT: PASS",
            },
        )
        mcp_server._reconcile_orphaned_jobs()
        got = jobrec.read_record("simulate-x-5")
        assert got is not None
        assert got.status == jobrec.STATUS_DONE
        assert got.exit_code == 0

    def test_nested_mode_skips_reconcile(self, _report_env, monkeypatch):
        monkeypatch.setenv("BOOLEY_MCP_NESTED", "1")
        jobrec.write_record(
            jobrec.JobRecord(
                run_id="simulate-x-4",
                endpoint="sim",
                started_at="t",
                timeout_s=60,
                pid=2_000_000_000,
                status=jobrec.STATUS_RUNNING,
            )
        )
        mcp_server._reconcile_orphaned_jobs()
        got = jobrec.read_record("simulate-x-4")
        # Left alone: a nested specialist must not adjudicate shared records.
        assert got is not None and got.status == jobrec.STATUS_RUNNING


class TestQueueCreditWatchdog:
    """The outer watchdog charges only ACTIVE time — queue wait is free."""

    class _NeverExits:
        pid = 4242

        def __init__(self):
            import asyncio

            self._event = asyncio.Event()

        async def wait(self):
            await self._event.wait()
            return 0

        def exit_now(self):
            self._event.set()

    def test_queue_time_is_not_charged(self, monkeypatch):
        # Queued for well past the whole budget, then the process exits as
        # soon as it goes active: must be True (exited), never a timeout.
        import asyncio

        monkeypatch.setattr(mcp_server, "_WATCHDOG_TICK_SECONDS", 0.01)
        proc = self._NeverExits()
        state = {"calls": 0}
        first_active = []

        def is_queued() -> bool:
            state["calls"] += 1
            if state["calls"] > 10:  # ~0.1s queued vs a 0.03s budget
                proc.exit_now()
                return False
            return True

        exited = asyncio.run(
            mcp_server._wait_with_queue_credit(
                proc, 0.03, is_queued, lambda: first_active.append(1)
            )
        )
        assert exited is True
        assert len(first_active) == 1  # stamped once, on the queued→active flip

    def test_active_time_still_times_out(self, monkeypatch):
        import asyncio

        monkeypatch.setattr(mcp_server, "_WATCHDOG_TICK_SECONDS", 0.01)
        proc = self._NeverExits()
        exited = asyncio.run(mcp_server._wait_with_queue_credit(proc, 0.03, lambda: False, None))
        assert exited is False


class TestAttachStripsTranscriptDir:
    def test_strip_removes_flag_and_value_only(self):
        argv = ["python", "-m", "x", "--transcript-dir", "/t/1", "--slug", "s"]
        assert mcp_server._strip_transcript_dir(argv) == ["python", "-m", "x", "--slug", "s"]
        assert mcp_server._strip_transcript_dir(["a", "b"]) == ["a", "b"]

    def test_specialist_resubmit_attaches_despite_fresh_transcript_dir(
        self, _report_env, monkeypatch
    ):
        # The per-call numbered --transcript-dir made specialist argv unique
        # per submit, so the duplicate-submit attach NEVER matched for the
        # exact endpoints whose duplicates each burn a full LLM sub-agent run.
        import os

        monkeypatch.setattr(
            mcp_server.jobrec,
            "derive_status",
            lambda rec, alive, **kw: jobrec.STATUS_RUNNING,
        )
        jobrec.write_record(
            jobrec.JobRecord(
                run_id="reviewer-z-1",
                endpoint="reviewer",
                started_at=_utc_stamp(-10),
                timeout_s=1800,
                pid=os.getpid(),
                status=jobrec.STATUS_RUNNING,
                argv=["python", "-m", "booley.specialists.reviewer", "--transcript-dir", "/t/1"],
            )
        )
        cmd = ["python", "-m", "booley.specialists.reviewer", "--transcript-dir", "/t/2"]
        assert mcp_server._find_attachable_job("reviewer", cmd) == "reviewer-z-1"

    def test_different_real_args_still_do_not_attach(self, _report_env, monkeypatch):
        import os

        monkeypatch.setattr(
            mcp_server.jobrec,
            "derive_status",
            lambda rec, alive, **kw: jobrec.STATUS_RUNNING,
        )
        jobrec.write_record(
            jobrec.JobRecord(
                run_id="reviewer-z-2",
                endpoint="reviewer",
                started_at=_utc_stamp(-10),
                timeout_s=1800,
                pid=os.getpid(),
                status=jobrec.STATUS_RUNNING,
                argv=["python", "-m", "booley.specialists.reviewer", "--slug", "a"],
            )
        )
        cmd = ["python", "-m", "booley.specialists.reviewer", "--slug", "b"]
        assert mcp_server._find_attachable_job("reviewer", cmd) is None


class TestJobReportIdentity:
    """run_id-stamped reports are matched by identity, not time window."""

    def _rec(self, run_id="simulate-i-1", **kw):
        base = {
            "run_id": run_id,
            "endpoint": "sim",
            "started_at": _utc_stamp(-100),
            "timeout_s": 600,
            "status": jobrec.STATUS_RUNNING,
        }
        base.update(kw)
        return jobrec.JobRecord(**base)

    def test_matching_run_id_is_proof(self, _report_env):
        # Identity beats any time-window judgment — even a stamp that would
        # fail the legacy window check (no timestamp at all) is fresh.
        _seed_report(_report_env, "sim", {"flow": "sim", "run_id": "simulate-i-1"})
        report, fresh = mcp_server._job_report(self._rec())
        assert fresh is True
        assert report["run_id"] == "simulate-i-1"

    def test_mismatched_flat_copy_falls_back_to_numbered_dir(self, _report_env):
        # Concurrent same-endpoint run overwrote the flat copy; ours is still in
        # its numbered invocation dir and must be found there.
        import json as _json

        reports = _seed_report(
            _report_env,
            "sim",
            {"flow": "sim", "run_id": "simulate-OTHER", "passed": False},
        )
        ours = reports / "sim" / "2"
        ours.mkdir(parents=True)
        (ours / "report.json").write_text(
            _json.dumps({"flow": "sim", "run_id": "simulate-i-1", "passed": True}),
            encoding="utf-8",
        )
        report, fresh = mcp_server._job_report(self._rec())
        assert fresh is True
        assert report["run_id"] == "simulate-i-1"
        assert report["passed"] is True

    def test_mismatch_without_our_report_shows_nothing(self, _report_env):
        # Never dress this job's EXIT_CODE in another run's report.
        _seed_report(_report_env, "sim", {"flow": "sim", "run_id": "simulate-OTHER"})
        report, fresh = mcp_server._job_report(self._rec())
        assert report is None
        assert fresh is False

    def test_killed_matrix_falls_back_to_run_scoped_progress(self, _report_env):
        import json as _json

        reports = _seed_report(
            _report_env,
            "sim",
            {"flow": "sim", "run_id": "simulate-OTHER"},
        )
        ours = reports / "sim" / "2"
        ours.mkdir(parents=True)
        (ours / "progress.json").write_text(
            _json.dumps(
                {
                    "flow": "sim",
                    "run_id": "simulate-i-1",
                    "complete": False,
                    "completed_targets": ["a"],
                    "pending_targets": ["b"],
                }
            ),
            encoding="utf-8",
        )
        report, fresh = mcp_server._job_report(self._rec())
        assert fresh is True
        assert report["partial"] is True
        assert report["completed_targets"] == ["a"]

    def test_stale_legacy_flat_report_does_not_hide_current_progress(self, _report_env):
        import json as _json

        reports = _seed_report(
            _report_env,
            "sim",
            {
                "flow": "sim",
                "timestamp": _utc_stamp(-3600),
                "passed": True,
            },
        )
        ours = reports / "sim" / "2"
        ours.mkdir(parents=True)
        (ours / "progress.json").write_text(
            _json.dumps(
                {
                    "flow": "sim",
                    "run_id": "simulate-i-1",
                    "complete": False,
                    "completed_targets": ["a"],
                    "pending_targets": ["b"],
                }
            ),
            encoding="utf-8",
        )

        report, fresh = mcp_server._job_report(self._rec())

        assert fresh is True
        assert report["partial"] is True
        assert report["completed_targets"] == ["a"]

    def test_running_poll_exposes_nonterminal_checkpoint(self, _report_env, monkeypatch):
        import asyncio
        import json as _json

        rec = self._rec()
        jobrec.write_record(rec)
        reports = Path(_report_env) / "flow-reports"
        ours = reports / "sim" / "2"
        ours.mkdir(parents=True)
        (ours / "progress.json").write_text(
            _json.dumps(
                {
                    "flow": "sim",
                    "run_id": rec.run_id,
                    "phase": "current",
                    "complete": False,
                    "completed_targets": ["a"],
                    "pending_targets": ["b"],
                }
            ),
            encoding="utf-8",
        )
        jobs = _JobManager(_FakeLifetime())

        async def _still_running(_run_id, _timeout):
            return False

        monkeypatch.setattr(jobs, "wait", _still_running)
        out = asyncio.run(_dispatch_poll({"run_id": rec.run_id}, jobs))

        assert isinstance(out, tuple)
        assert "completed targets ['a']" in _text(out)
        assert "pending targets ['b']" in _text(out)
        assert "EXIT_CODE:" not in _text(out)
        assert out[1]["reports"][0]["partial"] is True

    def test_legacy_report_window_anchors_at_run_start(self, _report_env):
        # No run_id (legacy writer): the time window's upper bound anchors at
        # run_started_at, so a job that queued past its own budget still owns
        # the report it wrote after finally running.
        rec = self._rec(
            started_at=_utc_stamp(-4000),  # submitted 4000s ago, budget 600
            run_started_at=_utc_stamp(-300),  # but only ran for the last 300s
        )
        _seed_report(
            _report_env,
            "sim",
            {"flow": "sim", "timestamp": _utc_stamp(-10), "passed": True},
        )
        report, fresh = mcp_server._job_report(rec)
        assert fresh is True
        assert report["passed"] is True


class TestDiskLongPoll:
    def test_poll_from_disk_returns_result_mid_wait(self, _report_env, monkeypatch):
        # An adopted job (no in-memory task) finishing during the wait budget
        # must resolve to its result, not bounce RUNNING back instantly.
        import asyncio
        import os

        monkeypatch.setattr(mcp_server, "_DISK_POLL_TICK_SECONDS", 0.01)
        rec = jobrec.JobRecord(
            run_id="simulate-d-1",
            endpoint="sim",
            started_at=_utc_stamp(-10),
            timeout_s=600,
            pid=os.getpid(),
            status=jobrec.STATUS_RUNNING,
        )
        jobrec.write_record(rec)
        statuses = iter([jobrec.STATUS_RUNNING, jobrec.STATUS_RUNNING, jobrec.STATUS_DONE])
        monkeypatch.setattr(
            mcp_server.jobrec,
            "derive_status",
            lambda r, alive, **kw: next(statuses),
        )
        jobs = _JobManager(_FakeLifetime())
        text = asyncio.run(mcp_server._poll_from_disk("simulate-d-1", jobs, wait_seconds=5.0))
        assert "still in progress" not in text
        assert "EXIT_CODE" in text

    def test_poll_from_disk_zero_wait_answers_immediately(self, _report_env, monkeypatch):
        import asyncio
        import os

        rec = jobrec.JobRecord(
            run_id="simulate-d-2",
            endpoint="sim",
            started_at=_utc_stamp(-10),
            timeout_s=600,
            pid=os.getpid(),
            status=jobrec.STATUS_RUNNING,
        )
        jobrec.write_record(rec)
        monkeypatch.setattr(
            mcp_server.jobrec,
            "derive_status",
            lambda r, alive, **kw: jobrec.STATUS_RUNNING,
        )
        jobs = _JobManager(_FakeLifetime())
        text = asyncio.run(mcp_server._poll_from_disk("simulate-d-2", jobs, wait_seconds=0.0))
        assert "still in progress" in text


class TestSubmitEnvStamp:
    def test_child_env_carries_run_id_and_slot_budget(self, _report_env, monkeypatch):
        # BOOLEY_RUN_ID → report identity; BOOLEY_SLOT_TIMEOUT_S → the
        # child's holder-deadline anchor. Both must reach the subprocess.
        import asyncio

        captured = {}

        async def _capture(cmd, *, timeout, on_spawn=None, env=None, **_kwargs):
            captured["env"] = env
            captured["timeout"] = timeout
            return 0, "ok", "", False

        monkeypatch.setattr(mcp_server, "_run_subprocess", _capture)

        async def scenario():
            jobs = _JobManager(_FakeLifetime())
            run_id = jobs.submit("sim", ["c"], 60)
            await jobs.wait(run_id, 5.0)
            return run_id

        run_id = asyncio.run(scenario())
        assert captured["env"]["BOOLEY_RUN_ID"] == run_id
        assert captured["env"]["BOOLEY_SLOT_TIMEOUT_S"] == "60"

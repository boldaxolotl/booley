"""Every Booley MCP tool response lists the caller's finished-but-unread Jobs (#2539).

The tests drive the real application through the SDK adapter, the final step every
stdio and HTTP response takes, with Job records written in the container jobs root.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest

try:
    from mcp.types import CallToolRequestParams, CallToolResult, TextContent

    from booley.mcp import server
except ImportError:
    pytest.skip("mcp package not installed", allow_module_level=True)

from booley.core.booley_dir import reset_cache
from booley.mcp.application import McpDispatchResult
from booley.mcp.session_registry import Attribution
from booley.runtime import job_records as jobrec
from booley.runtime.session_paths import session_jobs_dir
from booley.runtime.timefmt import format_clock, format_duration, parse_timestamp, utc_now_rfc3339

SERVER = "mcp-stdio:developer:pid:test:1:1"
NESTED_SERVER = "mcp-stdio:nested:pid:test:2:2"
ME = f"{SERVER}|codex:t1"
OTHER_TAB = f"{SERVER}|codex:t2"
NOTICE = "UNREAD JOB RESULT:"
# Far-future stamps end after any server start; 2026-01-01 ends before it.
LATER = "2999-01-01T00:00:00Z"
EARLIER = "2026-01-01T00:00:00Z"


class Observer:
    """Session observer: thread attribution from ``_meta.threadId`` and a canned warning."""

    def __init__(self, warning: str = "") -> None:
        self.warning = warning

    async def attribution(
        self, arguments, metadata, client_name, *, request=None, tool="request", caller=None
    ):
        return Attribution("codex:" + metadata.get("threadId", "unknown"), "thread", "/fx", "fx")

    async def record(self, facts, tool, outcome):
        return None

    async def resolved(self, facts):
        return facts

    async def shared(self, _facts):
        return (), self.warning


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    for name in ("BOOLEY_NESTED_AGENT", "BOOLEY_MCP_TOOLS", "BOOLEY_MCP_MODE"):
        monkeypatch.delenv(name, raising=False)
    project = tmp_path / ".booley"
    project.mkdir()
    monkeypatch.setenv("BOOLEY_DIR", str(project))
    monkeypatch.setenv("BOOLEY_LOGS_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("BOOLEY_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("BOOLEY_SLOTS_DIR", str(tmp_path / "jobs" / "slots"))
    monkeypatch.setattr(server, "is_pid_alive", lambda _pid: True)
    reset_cache()
    yield
    reset_cache()


def _application(identity: str = SERVER):
    return server._build_mcp_application(
        [], [], server._McpLifetime(None, None), server_identity=identity
    )


async def _call(
    application, name: str, arguments: dict[str, Any] | None = None, thread: str | None = "t1"
) -> CallToolResult:
    """One tool call through the SDK adapter, as thread *thread* (None: no attribution)."""
    params = CallToolRequestParams(
        name=name, arguments=arguments or {}, _meta={"threadId": thread} if thread else None
    )
    result = await server._call_application_tool(
        application, params, None, Observer() if thread else None
    )
    assert isinstance(result, CallToolResult)
    return result


def _texts(result: CallToolResult) -> list[str]:
    return [block.text for block in result.content if isinstance(block, TextContent)]


def _notice_lines(result: CallToolResult) -> list[str]:
    return [
        line for text in _texts(result) for line in text.splitlines() if line.startswith(NOTICE)
    ]


def _write(run_id: str, *, owner: str | None = ME, **fields: Any) -> str:
    """Persist one finished Job record in the container jobs root."""
    defaults: dict[str, Any] = {
        "endpoint": "sim",
        "started_at": utc_now_rfc3339(),
        "timeout_s": 60,
        "pid": 4242,
        "status": jobrec.STATUS_DONE,
        "exit_code": 0,
        "ended_at": EARLIER,
    }
    jobrec.write_record(
        jobrec.JobRecord(run_id=run_id, owner=owner, **{**defaults, **fields}),
        root=session_jobs_dir(),
    )
    return run_id


def _read(run_id: str) -> jobrec.JobRecord:
    rec = jobrec.read_record(run_id, session_jobs_dir())
    assert rec is not None
    return rec


def _report(run_id: str, report: dict[str, Any]) -> None:
    """Write the exact-run report of a ``sim`` Job, which makes it fresh."""
    directory = session_jobs_dir().parent / "flow-reports" / "sim" / "1"
    directory.mkdir(parents=True)
    (directory / "report.json").write_bytes(json.dumps({"run_id": run_id, **report}).encode())


@pytest.mark.asyncio
async def test_notice_lists_only_own_jobs():
    _write("mine-job")
    _write("other-tab-job", owner=OTHER_TAB)
    _write("nested-job", owner=NESTED_SERVER)
    _write(
        "baseline-job", owner=None, endpoint="sim_baseline", session_key="codex:t1", ended_at=LATER
    )
    _write("lint-job", owner=None, endpoint="lint", session_key="codex:t1", ended_at=LATER)
    _write("before-start-job", owner=None, session_key="codex:t1", ended_at=EARLIER)
    _write("legacy-mine-job", owner=None, session_key="codex:t1", ended_at=LATER)

    result = await _call(_application(), "booley_targets")

    shown = {line.split(" · ")[0].removeprefix(NOTICE).strip() for line in _notice_lines(result)}
    assert shown == {"mine-job", "legacy-mine-job"}


@pytest.mark.asyncio
async def test_nested_specialist_server_sees_only_its_own_jobs():
    _write("developer-job")
    _write("nested-job", owner=NESTED_SERVER)

    developer = await _call(_application(), "booley_targets")
    nested = await _call(_application(NESTED_SERVER), "booley_targets", thread=None)

    assert [line.split(" · ")[0] for line in _notice_lines(developer)] == [
        f"{NOTICE} developer-job"
    ]
    assert [line.split(" · ")[0] for line in _notice_lines(nested)] == [f"{NOTICE} nested-job"]


# (tool, arguments, whether the response is an error or None when either is fine)
RESPONSE_KINDS: list[tuple[str, dict[str, Any], bool | None]] = [
    ("booley_targets", {}, None),
    ("booley_cancel", {"run_id": "no-such-run"}, None),
    ("booley_poll", {"run_id": "foreign-running", "wait_seconds": 0}, None),
    ("booley_poll", {"wait_seconds": "x"}, True),
    ("goal_status", {}, None),
]


@pytest.mark.asyncio
async def test_notice_on_every_response_kind(monkeypatch):
    monkeypatch.setenv("BOOLEY_MCP_MODE", "interactive")
    _write(
        "foreign-running",
        owner=OTHER_TAB,
        status=jobrec.STATUS_RUNNING,
        ended_at=None,
        exit_code=None,
    )
    application = _application()
    baselines = [
        await _call(application, name, arguments) for name, arguments, _ in RESPONSE_KINDS
    ]
    assert all(_notice_lines(baseline) == [] for baseline in baselines)
    _write("mine-job")

    for (name, arguments, is_error), baseline in zip(RESPONSE_KINDS, baselines, strict=True):
        result = await _call(application, name, arguments)

        assert _texts(result)[-1].startswith(f"{NOTICE} mine-job"), name
        assert len(result.content) == len(baseline.content) + 1, name
        assert result.is_error == baseline.is_error, name
        if is_error is not None:
            assert result.is_error is is_error
        assert result.structured_content == baseline.structured_content, name


@pytest.mark.asyncio
async def test_notice_follows_goal_worktree_warning(monkeypatch):
    async def dispatch(*_args, **_kwargs):
        return McpDispatchResult(
            ([TextContent(type="text", text="Goal result")], {"facts": "kept"}),
            False,
            goal_aware=True,
        )

    monkeypatch.setattr(server, "_dispatch_application_tool", dispatch)
    _write("mine-job")
    params = CallToolRequestParams(name="booley_targets", _meta={"threadId": "t1"})

    result = await server._call_application_tool(
        _application(), params, None, Observer("WARNING: another session shares this worktree")
    )

    assert isinstance(result, CallToolResult)
    assert _texts(result)[:2] == ["Goal result", "WARNING: another session shares this worktree"]
    assert _texts(result)[-1].startswith(f"{NOTICE} mine-job")
    assert len(result.content) == 3
    assert result.structured_content == {"facts": "kept"}


@pytest.mark.asyncio
async def test_notice_is_bounded():
    for index in range(1, 13):
        _write(f"job-{index:02d}", ended_at=f"2026-01-01T00:{index:02d}:00Z")

    result = await _call(_application(), "booley_targets")

    lines = _texts(result)[-1].splitlines()
    assert [line.split(" · ")[0] for line in lines[:10]] == [
        f"{NOTICE} job-{index:02d}" for index in range(1, 11)
    ]
    assert len(lines) == 11
    assert lines[10].startswith("… and 2 more finished Jobs you have not read")
    assert "'booley_poll' with no run_id" in lines[10]
    assert "job-11" not in "\n".join(lines)


@pytest.mark.asyncio
async def test_notice_line_facts():
    _write("pass-job", argv=["--target", "tb_top"])
    _report("pass-job", {"flow": "sim", "detail": {"targets": 1, "targets_passed": 1}})
    _write("fail-job", status=jobrec.STATUS_FAILED, exit_code=1, ended_at="2026-01-01T00:01:00Z")
    _write(
        "cancel-job",
        status=jobrec.STATUS_CANCELLED,
        exit_code=None,
        ended_at="2026-01-01T00:02:00Z",
    )

    lines = _notice_lines(await _call(_application(), "booley_targets"))

    assert lines == [
        f"{NOTICE} pass-job · sim · Target tb_top · PASSED · 1/1 Targets passed"
        " · call 'booley_poll' with run_id=pass-job for the full result.",
        f"{NOTICE} fail-job · sim · Target default · FAILED"
        " · call 'booley_poll' with run_id=fail-job for the full result.",
        f"{NOTICE} cancel-job · sim · Target default · CANCELLED"
        " · call 'booley_poll' with run_id=cancel-job for the full result.",
    ]


@pytest.mark.asyncio
async def test_notice_never_marks_read():
    _write("mine-job")
    application = _application()

    first = await _call(application, "booley_targets")
    second = await _call(application, "booley_targets")

    assert len(_notice_lines(first)) == len(_notice_lines(second)) == 1
    assert _read("mine-job").read_at is None


@pytest.mark.asyncio
async def test_poll_by_run_id_clears_notice():
    _write("mine-job")
    application = _application()
    assert len(_notice_lines(await _call(application, "booley_targets"))) == 1

    polled = await _call(application, "booley_poll", {"run_id": "mine-job"})
    later = await _call(application, "booley_targets")

    assert "EXIT_CODE: 0" in "\n".join(_texts(polled))
    assert _notice_lines(polled) == []
    assert _notice_lines(later) == []
    assert _read("mine-job").read_at is not None


@pytest.mark.asyncio
async def test_argument_free_poll_lists_others_once():
    _write("first-job", ended_at="2026-01-01T00:01:00Z")
    _write("second-job", ended_at="2026-01-01T00:02:00Z")

    result = await _call(_application(), "booley_poll")

    text = "\n".join(_texts(result))
    assert "Result of job first-job" in text
    assert "Also finished, not yet read" not in text
    assert [line.split(" · ")[0] for line in _notice_lines(result)] == [f"{NOTICE} second-job"]


@pytest.mark.asyncio
async def test_no_unread_leaves_response_unchanged():
    application = _application()
    before = await _call(application, "booley_targets")
    _write("other-tab-job", owner=OTHER_TAB)
    _write("nested-job", owner=NESTED_SERVER)

    after = await _call(application, "booley_targets")

    assert after == before


@pytest.mark.asyncio
async def test_non_owner_poll_keeps_owner_notice():
    _write("mine-job")
    application = _application()

    await _call(application, "booley_poll", {"run_id": "mine-job"}, thread="t2")
    mine = await _call(application, "booley_targets", thread="t1")

    assert len(_notice_lines(mine)) == 1
    assert _read("mine-job").read_at is None


@pytest.mark.asyncio
async def test_notice_failure_keeps_result(monkeypatch, caplog):
    application = _application()
    baseline = await _call(application, "booley_targets")
    _write("mine-job")

    def broken(_self, _caller):
        raise RuntimeError("scan failed")

    monkeypatch.setattr(server.job_lifecycle.JobLifecycle, "unread_owned", broken, raising=False)
    with caplog.at_level(logging.WARNING):
        result = await _call(application, "booley_targets")

    assert result == baseline
    assert any(
        record.levelno == logging.WARNING and "scan failed" in caplog.text
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_first_read_time_recorded_and_latency_visible():
    from booley.harness.dashboard.app import job_detail
    from booley.harness.dashboard.jobs import JobView

    _write("mine-job", ended_at=EARLIER)
    application = _application()

    await _call(application, "booley_poll", {"run_id": "mine-job"})
    await _call(application, "booley_poll", {"run_id": "mine-job"})

    rec = _read("mine-job")
    assert rec.read_at is not None
    waited = (parse_timestamp(rec.read_at) - parse_timestamp(EARLIER)).total_seconds()
    detail = job_detail(JobView(Path(session_jobs_dir()), rec, "completed"))
    assert f"Read: {format_clock(rec.read_at)} · {format_duration(waited)} after end" in detail
    # The second poll did not move the first-read time.
    assert _read("mine-job").read_at == rec.read_at

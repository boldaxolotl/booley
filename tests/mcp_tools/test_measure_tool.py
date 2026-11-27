"""``booley_measure``: ``booley measure`` as an MCP Job with submit, poll and cancel (#4134).

The process of an MCP ``measure`` Job is the Job: it writes no record of its own and keeps its
progress and result beside the server's record. The children are scripted here (the real ones
are in ``tests/harness/test_measure_command_flows.py``); the rest is real: the command, the
Job files, the server's poll and its recovery after a restart.
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from booley.harness.measure_children import ChildrenOutcome
from booley.mcp import catalog, endpoint_process
from booley.mcp import server as mcp_server
from booley.mcp.agent_thread import AgentThread
from booley.mcp.application import McpRequestContext
from booley.mcp.server import _JobManager
from booley.measurement.metrics_file import TableRules
from booley.runtime import job_records as jobrec
from booley.runtime.session_paths import session_jobs_dir
from tests.harness.test_measure_cli import (  # noqa: F401  (the autouse fixture)
    COMMAND,
    TWO_LINT_TABLES,
    _measure,
    _measuring_environment,
    committed_project,
    outcome_of,
)
from tests.mcp_tools.test_mcp_dispatch import _FakeLifetime, _run_id_from
from tests.mcp_tools.test_owned_job_poll import (  # noqa: F401  (the autouse fixture)
    _FAKE_CHILD_PID,
    _all_text,
    _caller,
    _env,
    _poll,
    _write,
)
from tests.time_budget import hang_guard_s

RUN_ID = "measure-20260101T000000Z-1"


def measure_tool():
    """The tool module, imported inside the test so a missing module fails the test body."""
    try:
        from booley.mcp import measure_tool as module
    except ImportError:
        pytest.fail("booley.mcp.measure_tool does not exist")
    return module


@pytest.mark.parametrize(
    "name", ["booley_measure", "booley_review"], ids=["booley_measure", "booley_review"]
)
def test_the_tool_is_offered_to_the_developer_agent_only(name: str) -> None:
    visible = getattr(catalog, name.replace("booley_", "") + "_visible")
    developer = catalog.Exposure.from_mapping({"BOOLEY_MCP_DEVELOPER_AGENT": "1"})
    nested = catalog.Exposure.from_mapping(
        {
            "BOOLEY_MCP_DEVELOPER_AGENT": "1",
            "BOOLEY_NESTED_AGENT": "1",
            "BOOLEY_NESTED_MCP_TOOLS": "",
        }
    )
    named = catalog.Exposure.from_mapping({"BOOLEY_MCP_TOOLS": name})
    on = frozenset({"reviewed-commits"})
    developer, nested, named = (replace(e, features=on) for e in (developer, nested, named))

    assert visible(developer) and visible(named)
    assert not visible(nested)
    assert not visible(catalog.Exposure.from_mapping({}))
    assert name in [tool["name"] for tool in catalog.visible_definitions(developer, [])]
    assert name not in [tool["name"] for tool in catalog.visible_definitions(nested, [])]


def test_measure_returns_its_run_id_at_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.measurement.mcp_job import COMMAND_JOB_TIMEOUT_S

    tool = measure_tool()
    monkeypatch.setenv("BOOLEY_MCP_DEVELOPER_AGENT", "1")
    monkeypatch.setattr(mcp_server, "_validate_work_dir", lambda _value: None)
    monkeypatch.setattr(mcp_server, "_job_inline_wait_seconds", hang_guard_s)  # must not apply
    started, release = asyncio.Event(), asyncio.Event()
    submitted: list[tuple[list[str], int, dict[str, Any]]] = []

    async def process(cmd, *, timeout, on_spawn=None, **kwargs):
        submitted.append((cmd, timeout, kwargs))
        started.set()
        await release.wait()
        return 0, "", "", False

    monkeypatch.setattr(mcp_server, "_run_subprocess", process)
    arguments = {
        "revision": "--validate",  # a revision is never read as an option
        "base": "main",
        "target": "sim_core",
        "all": True,
        "again": True,
        "validate": True,
        "work_dir": str(tmp_path),
    }

    async def scenario() -> tuple[Any, bool]:
        jobs = _JobManager(_FakeLifetime())
        result = await asyncio.wait_for(
            mcp_server._dispatch_special_mcp_tool(
                tool.MEASURE_TOOL_NAME, arguments, jobs, [], None, _caller()
            ),
            timeout=hang_guard_s(),
        )
        await asyncio.wait_for(started.wait(), timeout=hang_guard_s())
        returned_before_the_process_ended = not release.is_set()
        release.set()
        await asyncio.gather(*jobs._tasks.values())
        return result, returned_before_the_process_ended

    result, at_once = asyncio.run(scenario())

    assert result is not None, "booley_measure is not a routed tool"
    text = _all_text(result)
    assert at_once and "RUNNING" in text
    run_id = _run_id_from(text)
    ((cmd, timeout, kwargs),) = submitted
    assert cmd == [
        sys.executable,
        "-m",
        "booley.harness.booley",
        "--project",
        str(tmp_path),
        "measure",
        "--base=main",
        "--target=sim_core",
        "--all",
        "--again",
        "--validate",
        "--",
        "--validate",
    ]
    assert timeout == COMMAND_JOB_TIMEOUT_S
    assert kwargs["stop_grace"] == endpoint_process.MEASURE_STOP_GRACE_SECONDS
    record = jobrec.read_record(run_id, session_jobs_dir())
    assert record is not None and record.endpoint == "measure"
    assert record.owner == _caller().owner


def test_a_second_measure_never_attaches_to_a_live_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """HEAD moves while a measurement runs, so the same argv can name another commit."""
    tool = measure_tool()
    monkeypatch.setenv("BOOLEY_MCP_DEVELOPER_AGENT", "1")
    monkeypatch.setattr(mcp_server, "_validate_work_dir", lambda _value: None)
    release = asyncio.Event()
    spawned: list[list[str]] = []

    async def process(cmd, *, timeout, on_spawn=None, **kwargs):
        spawned.append(cmd)
        await release.wait()
        return 0, "", "", False

    monkeypatch.setattr(mcp_server, "_run_subprocess", process)

    async def scenario() -> list[str]:
        jobs = _JobManager(_FakeLifetime())
        texts = [
            _all_text(
                await mcp_server._dispatch_special_mcp_tool(
                    tool.MEASURE_TOOL_NAME, {"work_dir": str(tmp_path)}, jobs, [], None, _caller()
                )
            )
            for _ in range(2)
        ]
        await asyncio.sleep(0.05)
        release.set()
        await asyncio.gather(*jobs._tasks.values())
        return texts

    first, second = asyncio.run(scenario())

    assert _run_id_from(first) != _run_id_from(second)
    assert "attached" not in second.lower() and len(spawned) == 2


def test_the_measure_job_records_the_calling_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#5310: the Dashboard files the Job under the agent that called, not as Standalone."""
    monkeypatch.setenv("BOOLEY_MCP_DEVELOPER_AGENT", "1")
    monkeypatch.setattr(mcp_server, "_validate_work_dir", lambda _value: None)

    async def process(cmd, *, timeout, on_spawn=None, **kwargs):
        return 0, "", "", False

    monkeypatch.setattr(mcp_server, "_run_subprocess", process)
    name = measure_tool().MEASURE_TOOL_NAME
    agent = AgentThread("codex", "t1")

    async def scenario() -> str:
        jobs = _JobManager(_FakeLifetime())
        result = await mcp_server._dispatch_application_tool(
            name,
            {"work_dir": str(tmp_path)},
            {"name": name},
            jobs=jobs,
            status_mcp_tool_names=[],
            mcp_tool_call_counts={},
            request_context=McpRequestContext(agent=agent),
            server_identity="server",
        )
        await asyncio.gather(*jobs._tasks.values())
        return _run_id_from(_all_text(result))

    record = jobrec.read_record(asyncio.run(scenario()), session_jobs_dir())
    assert record is not None and (record.client, record.thread) == ("codex", "t1")


def test_the_measurement_flow_jobs_carry_the_agent_of_its_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#5310: each Flow Job of the measurement records the measuring agent too."""
    from booley.harness import measure_children

    project = committed_project(tmp_path, TWO_LINT_TABLES)
    mcp_job_environment(monkeypatch, client="codex", thread="t1")
    script_progress(monkeypatch, lambda: None, [0, 0])
    requests: list[Any] = []

    def prepare(request: Any) -> SimpleNamespace:
        requests.append(request)
        return SimpleNamespace(discard=lambda: None)

    # A real child preflight refuses outside the Sandbox, so it is replaced; the requests
    # it receives are built by the real measurement, which is what this test checks.
    monkeypatch.setattr(f"{COMMAND}.prepare_children", measure_children.prepare_children)
    monkeypatch.setattr(measure_children, "prepare_checkout_flow", prepare)

    assert _measure(monkeypatch, project) == 0
    assert [(request.client, request.thread) for request in requests] == [("codex", "t1")] * 2


def test_an_unsupplied_option_adds_nothing_to_the_command() -> None:
    argv = measure_tool().measure_argv({"all": False, "again": None, "base": ""})

    assert argv == [sys.executable, "-m", "booley.harness.booley", "measure"]


def mcp_job_environment(
    monkeypatch: pytest.MonkeyPatch, run_id: str = RUN_ID, endpoint: str = "measure", **record: Any
) -> None:
    """The process of the *endpoint* Job the server recorded: its record, run id and origin."""
    _write(
        run_id,
        owner=_caller().owner,
        status=jobrec.STATUS_RUNNING,
        endpoint=endpoint,
        exit_code=None,
        **record,
    )
    monkeypatch.setenv("BOOLEY_RUN_ID", run_id)
    monkeypatch.setenv("_BOOLEY_CLI_INVOCATION_ORIGIN", "transport")


def script_progress(
    monkeypatch: pytest.MonkeyPatch,
    between: Callable[[], None],
    warnings: Sequence[int | None],
) -> None:
    """Children that report progress, call *between* with one Flow running, then finish."""

    def run_children(
        _prepared: object,
        tables: Sequence[TableRules],
        *,
        progress: Callable[[int, int, tuple[str, ...]], None],
        **_options: object,
    ) -> ChildrenOutcome:
        first, second = (f"{table.target}.{table.flow}" for table in tables)
        progress(0, 2, ())
        progress(0, 2, (first, second))
        progress(1, 2, (second,))
        between()
        progress(2, 2, ())
        return outcome_of(tables, warnings)

    monkeypatch.setattr(f"{COMMAND}.prepare_children", lambda *_a, **_o: [])
    monkeypatch.setattr(f"{COMMAND}.run_children", run_children)


def test_a_poll_shows_flows_finished_of_total_and_the_running_flow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = committed_project(tmp_path, TWO_LINT_TABLES)
    mcp_job_environment(monkeypatch)
    seen: list[str] = []

    def poll_twice() -> None:
        jobs = _JobManager(_FakeLifetime())
        for arguments in ({"run_id": RUN_ID}, {}):
            seen.append(_all_text(asyncio.run(_poll(jobs, _caller(), wait=0, **arguments))))

    script_progress(monkeypatch, poll_twice, [0, 0])

    assert _measure(monkeypatch, project) == 0

    by_run_id, argument_free = seen
    for text in seen:
        assert "1 of 2 Flows finished; running: sim_two.lint" in text
        assert "sim_core.lint" not in text  # finished
    assert "RUNNING" in by_run_id or "still in progress" in by_run_id
    assert "Still running" in argument_free
    # The process is the Job: it wrote no Standalone record of its own.
    assert not (project / ".booley" / "job_logs" / "standalone" / ".runtime").exists()


def test_the_final_result_carries_verdict_failing_rows_paths_and_exit_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from booley.measurement.measuring_rules import UNMET_RULE

    project = committed_project(tmp_path, TWO_LINT_TABLES)
    mcp_job_environment(monkeypatch)
    script_progress(monkeypatch, lambda: None, [3, 0])

    code = _measure(monkeypatch, project)

    captured = capsys.readouterr()
    assert code == 1
    record = jobrec.read_record(RUN_ID, session_jobs_dir())
    assert record is not None
    record.status, record.exit_code, record.ended_at = (
        jobrec.STATUS_FAILED,
        code,
        "2026-01-01T00:00:00Z",
    )
    jobrec.write_record(record, session_jobs_dir())
    jobs = _JobManager(_FakeLifetime())
    jobs._results[RUN_ID] = (code, captured.out, captured.err, False)  # as the server holds it

    text = _all_text(asyncio.run(_poll(jobs, _caller(), wait=0, run_id=RUN_ID)))

    lines = text.splitlines()
    assert lines[0] == "EXIT_CODE: 1"
    assert lines[1] == "1 unmet of 2 enforced"
    assert lines[2].endswith(
        ": failing"
    )  # the header line, as the agent's green-header rule needs
    assert "sim_core lint warnings_max: 3 vs ≤ 0, unmet" in text
    markdown = next(line for line in lines if line.startswith("Markdown: "))
    json_path = next(line for line in lines if line.startswith("JSON: "))
    assert Path(markdown.removeprefix("Markdown: ")).is_file()
    assert Path(json_path.removeprefix("JSON: ")).is_file()
    assert text.rstrip().endswith(UNMET_RULE)
    assert text.count("1 unmet of 2 enforced") == 1  # the summary, not the whole report again


def test_the_result_is_published_before_the_report_is_printed_to_a_gone_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.measurement.mcp_job import read_result

    project = committed_project(tmp_path, TWO_LINT_TABLES)
    mcp_job_environment(monkeypatch)
    script_progress(monkeypatch, lambda: None, [3, 0])

    def gone(*_args: object, **_kwargs: object) -> None:
        raise BrokenPipeError  # the server's pipe reader is gone

    monkeypatch.setattr(f"{COMMAND}._print_report", gone)

    assert _measure(monkeypatch, project) == 2  # the process fails, the published result stays

    result = read_result(session_jobs_dir() / RUN_ID)
    assert result is not None and result["exit_code"] == 1
    assert result["summary"].splitlines()[0] == "1 unmet of 2 enforced"


def test_a_server_that_lost_the_measurement_recovers_its_exit_code_and_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from booley.measurement.mcp_job import McpCommandJob

    jobs_root = session_jobs_dir()
    for exit_code, status in ((1, jobrec.STATUS_FAILED), (0, jobrec.STATUS_DONE)):
        run_id = f"measure-lost-{exit_code}"
        _write(
            run_id,
            owner=_caller().owner,
            status=jobrec.STATUS_RUNNING,
            endpoint="measure",
            exit_code=None,
        )
        McpCommandJob(run_id, jobs_root / run_id, "measure").publish_result(
            exit_code, f"SUMMARY {exit_code}"
        )
        monkeypatch.setattr(mcp_server, "is_pid_alive", lambda _pid: False)

        restarted = _JobManager(_FakeLifetime())
        restarted.reconcile_orphaned()
        text = _all_text(asyncio.run(_poll(restarted, _caller(), wait=0, run_id=run_id)))

        record = jobrec.read_record(run_id, jobs_root)
        assert record is not None and (record.status, record.exit_code) == (status, exit_code)
        assert text.splitlines()[:2] == [f"EXIT_CODE: {exit_code}", f"SUMMARY {exit_code}"]


def test_a_refusal_or_a_validation_renders_as_any_tool_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool = measure_tool()

    refused = tool.result_text(2, "", "ERROR: no\nExit 2: stop and report the error.", None)
    stale = tool.result_text(
        1, "out", "", {"endpoint": "measure", "exit_code": 0, "summary": "SUMMARY"}
    )

    assert refused.startswith("EXIT_CODE: 2\n") and "ERROR: no" in refused
    # A summary of another exit status is not laid out as this result's own.
    assert stale.splitlines()[1] != "SUMMARY" and "out" in stale


def test_a_progress_file_that_is_missing_or_torn_reads_as_no_progress(tmp_path: Path) -> None:
    from booley.measurement.mcp_job import read_progress, read_result

    (tmp_path / "result.json").write_text("{torn", encoding="utf-8")
    (tmp_path / "progress.json").write_text(json.dumps([1]), encoding="utf-8")

    assert read_progress(tmp_path) is None and read_result(tmp_path) is None
    assert read_progress(tmp_path / "absent") is None


def test_the_measure_stop_grace_outlasts_the_child_stop_window() -> None:
    from booley.harness import checkout_flow

    assert endpoint_process.stop_grace_seconds("measure") > checkout_flow.STOP_GRACE_S
    assert (
        endpoint_process.stop_grace_seconds("review")
        == endpoint_process.MEASURE_STOP_GRACE_SECONDS
    )
    assert endpoint_process.stop_grace_seconds("sim") is None
    assert endpoint_process.cancel_grace_seconds("sim") == endpoint_process.CANCEL_GRACE_SECONDS


def test_a_process_outside_an_mcp_measure_job_finds_no_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from booley.measurement.mcp_job import McpCommandJob

    monkeypatch.setenv("BOOLEY_RUN_ID", RUN_ID)
    monkeypatch.setenv("_BOOLEY_CLI_INVOCATION_ORIGIN", "transport")
    assert McpCommandJob.find("measure") is None  # no record
    _write(RUN_ID, owner=None, status=jobrec.STATUS_RUNNING, endpoint="sim", exit_code=None)
    assert McpCommandJob.find("measure") is None  # another endpoint's record
    monkeypatch.delenv("_BOOLEY_CLI_INVOCATION_ORIGIN")
    assert McpCommandJob.find("measure") is None  # not a transport child

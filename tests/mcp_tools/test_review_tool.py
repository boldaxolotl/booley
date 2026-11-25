"""``booley_review``: ``booley review`` as an MCP Job with submit, poll and cancel (#5599).

The Reviewer is the shared ``ScriptedAgent``; the command, its Job files, the export and the
server's poll are real. The tool's own argv is run through the real CLI entry point, so the
tool and the CLI cannot give different results for the same inputs.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from booley.mcp import server as mcp_server
from booley.mcp.server import _JobManager
from booley.runtime import job_records as jobrec
from booley.runtime.session_paths import session_jobs_dir
from tests import mcp_runner_fake
from tests.conftest import FeatureFlagHelper
from tests.mcp_tools.reviewer_support import ScriptedAgent
from tests.mcp_tools.test_mcp_dispatch import _FakeLifetime, _run_id_from
from tests.mcp_tools.test_measure_tool import mcp_job_environment
from tests.mcp_tools.test_owned_job_poll import (  # noqa: F401  (the autouse fixture)
    _all_text,
    _caller,
    _env,
    _poll,
)
from tests.review.test_review_command import (
    A_V2,
    _environment,  # noqa: F401  (the autouse fixture)
    agent,  # noqa: F401  (the fixture)
    finding,
    project_on_main,
    review,
    stored_runs,
)
from tests.review.test_review_verify import verdict
from tests.runtime.test_commit_export import commit_all, write
from tests.time_budget import hang_guard_s

TWO_REVIEWS = '[sim_core.review]\nrtl = ["bugs", "code_style"]\nresolve = "major"\n'
HOOK = """\
import os, pathlib, time
pid = pathlib.Path({pid!r})
pid.with_suffix(".tmp").write_text(str(os.getpid()))
os.replace(pid.with_suffix(".tmp"), pid)
time.sleep({seconds})
"""


def review_tool():
    """The tool module, imported inside the test so a missing module fails the test body."""
    try:
        from booley.mcp import review_tool as module
    except ImportError:
        pytest.fail("booley.mcp.review_tool does not exist")
    return module


def argv_of(arguments: dict[str, Any]) -> list[str]:
    """What follows ``booley review`` on the tool's command line."""
    argv = review_tool().review_argv(arguments)
    return argv[argv.index("review") + 1 :]


@pytest.mark.parametrize(
    "case",
    [
        pytest.param("every-option", id="every-option"),
        pytest.param("no-option", id="no-option"),
        pytest.param("refused-work-dir", id="refused-work-dir"),
    ],
)
def test_review_returns_its_run_id_at_once_and_maps_every_option(
    case: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tool = review_tool()
    from booley.measurement.mcp_job import COMMAND_JOB_TIMEOUT_S

    monkeypatch.setenv("BOOLEY_MCP_DEVELOPER_AGENT", "1")
    monkeypatch.chdir(tmp_path)  # so str(tmp_path) is the explicit spelling of the default
    started, release = asyncio.Event(), asyncio.Event()

    async def process(_cmd: list[str], **_kwargs: Any) -> tuple[int, str, str, bool]:
        started.set()
        await release.wait()
        return 0, "", "", False

    calls = mcp_runner_fake.install(monkeypatch, answer=process)
    arguments: dict[str, Any] = {
        "every-option": {
            "revision": "--status",  # a revision is never read as an option
            "base": "main",
            "target": "sim_core",
            "json": True,
            "verify": True,
            "status": True,
            "waive": [
                {"finding_id": "F1", "justification": "fixed upstream"},
                {"finding_id": "F2", "justification": "cosmetic"},
            ],
            "work_dir": str(tmp_path),
        },
        "no-option": {
            "verify": False,
            "status": False,
            "json": False,
            "waive": [],
            "base": "",
            "revision": "",
        },
        "refused-work-dir": {"work_dir": str(tmp_path / "absent")},
    }[case]

    async def scenario() -> tuple[Any, bool]:
        jobs = _JobManager(_FakeLifetime())
        result = await asyncio.wait_for(
            mcp_server._dispatch_special_mcp_tool(
                tool.REVIEW_TOOL_NAME, arguments, jobs, [], None, _caller()
            ),
            timeout=hang_guard_s(),
        )
        if case == "refused-work-dir":
            return result, True
        await asyncio.wait_for(started.wait(), timeout=hang_guard_s())
        returned_before_the_process_ended = not release.is_set()
        release.set()
        await asyncio.gather(*jobs._tasks.values())
        return result, returned_before_the_process_ended

    result, at_once = asyncio.run(scenario())

    assert result is not None, "booley_review is not a routed tool"
    text = _all_text(result)
    if case == "refused-work-dir":
        assert "does not exist" in text and calls == []
        return
    assert at_once and "RUNNING" in text
    (call,) = calls
    head = [sys.executable, "-m", "booley.harness.booley"]
    expected = {
        "every-option": [
            *head,
            *("--project", str(tmp_path), "review", "--base=main", "--target=sim_core"),
            *("--json", "--verify", "--status", "--waive", "F1", "fixed upstream"),
            *("--waive", "F2", "cosmetic", "--", "--status"),
        ],
        "no-option": [*head, "review"],
    }[case]
    assert call.cmd == expected
    assert call.kwargs["timeout"] == COMMAND_JOB_TIMEOUT_S
    record = jobrec.read_record(_run_id_from(text), session_jobs_dir())
    assert record is not None and record.endpoint == "review"
    assert record.owner == _caller().owner


def poll_both_ways(seen: list[list[str]], run_id: str) -> Callable[[Any], None]:
    """A Reviewer hook that polls the Job by run id and without one, in a thread of its own.

    The Reviewer calls the hook inside its running event loop, so the poll cannot run there.
    """

    async def poll() -> list[str]:
        jobs = _JobManager(_FakeLifetime())
        return [
            _all_text(await _poll(jobs, _caller(), wait=0, **arguments))
            for arguments in ({"run_id": run_id}, {})
        ]

    def hook(_params: Any) -> None:
        with ThreadPoolExecutor(1) as pool:
            seen.append(pool.submit(asyncio.run, poll()).result())

    return hook


def run_as_job(
    monkeypatch: pytest.MonkeyPatch,
    agent: ScriptedAgent,  # noqa: F811
    root: Path,
    run_id: str,
    arguments: dict[str, Any],
) -> tuple[int, list[list[str]]]:
    """Run the tool's command as the Job *run_id*; return its exit code and each progress poll."""
    seen: list[list[str]] = []
    mcp_job_environment(monkeypatch, run_id=run_id, endpoint="review")
    agent.on_call = poll_both_ways(seen, run_id)
    return review(monkeypatch, root, *argv_of(arguments)), seen


def test_a_review_job_polls_progress_then_the_cli_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    feature_flags: FeatureFlagHelper,
    agent: ScriptedAgent,  # noqa: F811
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = project_on_main(tmp_path, feature_flags, TWO_REVIEWS)
    write(root / "rtl" / "a.sv", A_V2)
    commit = commit_all(root, "Edit a")
    agent.answer({"bugs-hunt": {"issues": [finding()]}, "*": {"issues": []}})

    code, seen = run_as_job(monkeypatch, agent, root, "review-1", {})

    captured = capsys.readouterr()
    assert code == 1  # a finding the commit introduced is open
    *bugs, code_style = seen  # the bugs review asks the model twice, code_style once
    assert bugs
    for polls in bugs:
        assert all("0 of 2 reviews finished; running: rtl bugs" in text for text in polls)
    assert all("1 of 2 reviews finished; running: rtl code_style" in text for text in code_style)
    assert "Still running" in bugs[0][1]  # the argument-free poll
    record = jobrec.read_record("review-1", session_jobs_dir())
    assert record is not None
    record.status, record.exit_code = jobrec.STATUS_FAILED, code
    record.ended_at = "2026-01-01T00:00:00Z"
    jobrec.write_record(record, session_jobs_dir())
    jobs = _JobManager(_FakeLifetime())
    jobs._results["review-1"] = (code, captured.out, captured.err, False)  # as the server holds it

    final = _all_text(asyncio.run(_poll(jobs, _caller(), wait=0, run_id="review-1")))

    assert final.splitlines()[0] == "EXIT_CODE: 1"
    assert "Review Report" in final and "\nReview Run " in final
    assert "reviews finished" not in final  # the progress file is not the report

    [discovery] = stored_runs(root, commit)
    (rtl_bugs, _code_style) = discovery["reviews"]
    waiver = {"finding_id": rtl_bugs["introduced"][0]["finding_id"], "justification": "ok"}
    ruling = verdict(waiver["finding_id"], "waived", "the reason holds")
    agent.answer([{"verdicts": [ruling]}])
    code, seen = run_as_job(
        monkeypatch, agent, root, "review-2", {"verify": True, "waive": [waiver]}
    )
    assert code == 0
    assert all("0 of 1 reviews finished; running: rtl bugs" in text for text in seen[0])

    capsys.readouterr()
    code, seen = run_as_job(monkeypatch, agent, root, "review-3", {"status": True})
    assert (code, seen) == (0, [])
    assert "Reviewed" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        pytest.param(
            {"waive": [{"finding_id": "F1", "justification": "why"}]},
            "--waive needs --verify",
            id="waive-without-verify",
        ),
        pytest.param(
            {"verify": True, "status": True}, "not allowed with argument", id="verify-and-status"
        ),
    ],
)
def test_a_bad_combination_is_refused_by_the_cli(
    arguments: dict[str, Any],
    message: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    feature_flags: FeatureFlagHelper,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = project_on_main(tmp_path, feature_flags, TWO_REVIEWS)

    assert review(monkeypatch, root, *argv_of(arguments)) == 2

    assert message in capsys.readouterr().err
    assert stored_runs(root) == []


@pytest.mark.skipif(sys.platform == "win32", reason="signals its own process")
def test_a_cancelled_review_stops_its_post_setup_hook(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    feature_flags: FeatureFlagHelper,
    agent: ScriptedAgent,  # noqa: F811
) -> None:
    root = project_on_main(tmp_path, feature_flags, TWO_REVIEWS)
    pid_file = tmp_path / "hook.pid"
    hook = root / ".booley" / "hooks" / "post-setup.py"
    write(hook, HOOK.format(pid=str(pid_file), seconds=2 * hang_guard_s()))
    write(root / "rtl" / "a.sv", A_V2)
    commit_all(root, "Edit a, add a hook")

    def not_interrupted(_signum: int, _frame: object) -> None:
        raise RuntimeError("SIGTERM was not turned into an interrupt")

    def cancel() -> None:  # the server's cancel: SIGTERM once the hook runs
        deadline = time.monotonic() + hang_guard_s()
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        os.kill(os.getpid(), signal.SIGTERM)

    previous = signal.signal(signal.SIGTERM, not_interrupted)
    canceller = threading.Thread(target=cancel, daemon=True)
    canceller.start()
    try:
        with pytest.raises(KeyboardInterrupt):
            review(monkeypatch, root)
    finally:
        signal.signal(signal.SIGTERM, previous)
        canceller.join(hang_guard_s())

    hook_pid = int(pid_file.read_text(encoding="utf-8"))
    deadline = time.monotonic() + hang_guard_s()
    while time.monotonic() < deadline and _alive(hook_pid):
        time.sleep(0.05)
    assert not _alive(hook_pid), "the cancelled review left its post-setup hook running"
    assert list((tmp_path / "scratch-root").iterdir()) == []  # the export is removed


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True

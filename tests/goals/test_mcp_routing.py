"""Exercise immutable admission, MCP results, and durable cross-root job operations."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.goals.binding import GoalBindingError
from booley.goals.model import GoalState
from booley.goals.paths import record_paths
from booley.goals.store import GoalStore, GoalStoreError
from booley.mcp import server
from booley.mcp.call_context import binding_from_environment, resolve_call_context
from booley.mcp.goal_tools import dispatch_goal_tool
from booley.runtime import job_records
from booley.runtime.timefmt import utc_now_rfc3339
from tests.goals.conftest import git, update_record


@pytest.fixture(autouse=True)
def preview(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOOLEY_TOP_LEVEL", "1")


def test_binding_and_environment(
    goal_mode: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = resolve_call_context({"work_dir": str(goal_mode.worktree)})
    assert context.binding is not None
    assert context.binding.record_id == goal_mode.record.id
    paths = record_paths(goal_mode.control, goal_mode.record.id)
    assert (context.jobs_root, context.state_path, context.logs_dir, context.runtime_dir) == (
        paths.jobs_dir,
        paths.state_file,
        paths.logs_dir,
        paths.runtime_dir,
    )
    for key, value in context.subprocess_env_overrides.items():
        monkeypatch.setenv(key, value)
    assert binding_from_environment() == context.binding
    command = server._endpoint_command(
        "lint",
        {"work_dir": str(goal_mode.worktree)},
        {"module": "lint", "is_flow": True},
        {},
        context,
    )
    assert command[:4] == ["python", "-m", "booley.mcp.goal_flow_runner", "lint"]
    from booley.goals.flow_execution import GoalFlowExecution
    from booley.specialists.reviewer import ReviewerSpecialist

    assert isinstance(ReviewerSpecialist()._acceptance_recorder, GoalFlowExecution)


def test_missing_work_dir_both_paths(goal_mode: SimpleNamespace) -> None:
    with pytest.raises(GoalBindingError, match="Pass work_dir"):
        resolve_call_context({})
    result = asyncio.run(dispatch_goal_tool("goal_status", {}, project_dir=goal_mode.control))
    assert result.is_error and "Pass work_dir" in result.value[0].text


@pytest.mark.parametrize("state", [GoalState.ENTERING, GoalState.FINISHING])
def test_nonactive_records_refused(goal_mode: SimpleNamespace, state: GoalState) -> None:
    # Write a retained lifecycle snapshot at the external JSON trust boundary.
    path = record_paths(goal_mode.control, goal_mode.record.id).record_file
    raw = json.loads(path.read_text())
    raw["state"] = state.value
    path.write_text(json.dumps(raw))
    with pytest.raises(GoalBindingError, match=state.value):
        resolve_call_context({"work_dir": str(goal_mode.worktree)})


@pytest.mark.parametrize("explicit", [False, True])
def test_corrupt_record_refused(
    goal_mode: SimpleNamespace, tmp_path: Path, explicit: bool
) -> None:
    record_paths(goal_mode.control, goal_mode.record.id).record_file.write_bytes(b"{bad")
    with pytest.raises(GoalStoreError, match="corrupt Goal Record"):
        resolve_call_context({"work_dir": str(tmp_path)} if explicit else {})


def test_wrong_branch_refuses(goal_mode: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    git(goal_mode.worktree, "checkout", "--detach")
    with pytest.raises(GoalBindingError, match="Goal Branch"):
        resolve_call_context({"work_dir": str(goal_mode.worktree)})
    with pytest.raises(GoalBindingError, match="Pass work_dir"):
        resolve_call_context({})


def test_multi_root_retained_records_and_ambiguity(
    goal_mode: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    context = resolve_call_context({"work_dir": str(goal_mode.worktree)})
    container = tmp_path / "runtime" / "jobs"
    monkeypatch.setenv("BOOLEY_RUNTIME_DIR", str(container.parent))
    monkeypatch.setenv("BOOLEY_LOGS_DIR", str(tmp_path / "logs"))
    record = job_records.JobRecord(
        "legacy",
        "lint",
        utc_now_rfc3339(),
        30,
        status="done",
        exit_code=0,
        binding=context.binding.to_json(),
    )
    job_records.write_record(record, context.jobs_root)
    update_record(goal_mode, state=GoalState.ABANDONED)
    assert context.jobs_root in server.job_roots()
    found = server._locate_job("legacy")
    assert found is not None and found.root == context.jobs_root
    assert found.record.binding == record.binding
    assert "EXIT_CODE: 0" in server._JobManager(server._McpLifetime(None, None)).result_text(
        "legacy"
    )
    job_records.write_record(record, container)
    with pytest.raises(job_records.JobRecordError, match="Ambiguous"):
        server._locate_job("legacy")


def test_attach_requires_same_admission(goal_mode: SimpleNamespace) -> None:
    context = resolve_call_context({"work_dir": str(goal_mode.worktree)})
    rec = job_records.JobRecord(
        "running", "lint", utc_now_rfc3339(), 30, argv=["same"], binding=context.binding.to_json()
    )
    job_records.write_record(rec, context.jobs_root)
    assert server._find_attachable_job("lint", ["same"], context=context) == "running"
    assert server._find_attachable_job("lint", ["same"]) is None
    raw = dict(rec.binding)
    raw["spec_revisions"] = {"lint_clean_top": 999}
    rec.binding = raw
    job_records.write_record(rec, context.jobs_root)
    assert server._find_attachable_job("lint", ["same"], context=context) is None


def test_status_rules(goal_mode: SimpleNamespace) -> None:
    result = asyncio.run(
        dispatch_goal_tool(
            "goal_status",
            {"work_dir": str(goal_mode.worktree), "rules": True},
            project_dir=goal_mode.control,
        )
    )
    assert result.is_error is False
    assert "lint_clean_top" in result.value[0].text
    assert "Only Booley Flows" in result.value[0].text
    assert "confirm the test fails" in result.value[0].text


def test_two_goal_worktrees_and_interactive_restart_poll_cancel(goal_mode, monkeypatch, tmp_path):
    from tests.goals.conftest import enter_goals, write_project_files

    second = goal_mode.control / "worktrees" / "other"
    git(goal_mode.main, "worktree", "add", "-q", "-b", "other", str(second))
    write_project_files(second / ".booley_project")
    other = SimpleNamespace(main=goal_mode.main, control=goal_mode.control, worktree=second)
    from booley.goals import entry as entry_module

    original_id = entry_module.compact_utc_now
    monkeypatch.setattr(entry_module, "compact_utc_now", lambda: "20261007T120000Z")
    other.record = enter_goals(other)
    monkeypatch.setattr(entry_module, "compact_utc_now", original_id)
    contexts = [
        resolve_call_context({"work_dir": str(path)}) for path in (goal_mode.worktree, second)
    ]
    container = tmp_path / "interactive" / "jobs"
    monkeypatch.setenv("BOOLEY_LOGS_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("BOOLEY_RUNTIME_DIR", str(container.parent))
    monkeypatch.setattr(server, "is_pid_alive", lambda pid: pid == 123)

    async def terminate(_pid):
        return None

    monkeypatch.setattr(server, "_cancel_adopted_process_group", terminate)
    monkeypatch.setattr(server, "_job_inline_wait_seconds", lambda: 0)
    records = []
    for index, context in enumerate([*contexts, None]):
        root = container if context is None else context.jobs_root
        binding = None if context is None else context.binding.to_json()
        rec = job_records.JobRecord(
            f"multi-{index}", "lint", utc_now_rfc3339(), 60, pid=123, binding=binding
        )
        job_records.write_record(rec, root)
        records.append((rec, root))
    jobs = server._JobManager(server._McpLifetime(None, None))
    for rec, root in records:
        result = asyncio.run(
            server._dispatch_poll({"run_id": rec.run_id, "wait_seconds": 0}, jobs)
        )
        if rec.binding is not None:
            assert isinstance(result, server.McpDispatchResult) and result.goal_aware
            result = result.value
        assert "RUNNING" in result[0].text
        result = asyncio.run(server._dispatch_cancel({"run_id": rec.run_id}, jobs))
        assert "CANCELLED" in result[0].text
        assert job_records.read_record(rec.run_id, root).status == "cancelled"
    for rec, root in records:
        rec.status, rec.pid, rec.exit_code = "running", 456, None
        job_records.write_record(rec, root)
    server._reconcile_orphaned_jobs()
    for rec, root in records:
        assert job_records.read_record(rec.run_id, root).status == "failed"


def test_warning_and_discard_prefix_preserve_structured_result(goal_mode):
    from tests.goals.conftest import edit_protected

    context = resolve_call_context({"work_dir": str(goal_mode.worktree)})
    edit_protected(goal_mode)
    report = {"detail": {"evidence_discarded": "protected input changed"}}
    rendered = server._format_mcp_tool_result(0, "", "", report)
    assert rendered.startswith("evidence discarded: protected input changed\n")
    structured = ([server.TextContent(type="text", text=rendered)], {"facts": "retained"})
    result = server._goal_warning_result(structured, context.binding, context.session_key)
    assert result.goal_aware
    assert result.value[0][0].text.startswith("WARNING:")
    assert result.value[1] == {"facts": "retained"}


def test_shared_worktree_and_missing_path_warning(goal_mode):
    from booley.goals.warnings import goal_warnings

    warning = goal_warnings(
        goal_mode.record,
        goal_mode.control,
        work_dir=None,
        session_key="one",
        other_sessions=("one", "two"),
    )
    assert "another session" in warning
    assert "pass work_dir" in warning


def test_spec_from_declared_goal_is_forwarded(layout):
    from tests.goals.conftest import enter_goals

    layout.record = enter_goals(
        layout,
        [{"family": "review", "review": "rtl_spec", "verdict": "done", "spec": "docs/spec.md"}],
    )
    context = resolve_call_context({"work_dir": str(layout.worktree)})
    cmd = server._endpoint_command(
        "reviewer",
        {"category": "rtl", "focus": "spec"},
        {"module": "reviewer", "is_specialist": True},
        __import__("collections").defaultdict(int),
        context,
    )
    assert cmd[cmd.index("--spec") + 1] == "docs/spec.md"
    assert str(context.runtime_dir / "transcripts") in cmd[cmd.index("--transcript-dir") + 1]


def test_module_entrypoint_defines_dispatch_before_main(goal_mode):
    import os
    import subprocess
    import sys

    # Execute the actual module entrypoint in another interpreter. Replace only
    # the outer event loop so the test checks definitions before service startup.
    script = """import asyncio
import runpy
import booley.runtime.runtime_context as runtime
runtime.container_only_error = lambda name: None
def run(coro):
    globals_ = coro.cr_frame.f_globals
    assert all(name in globals_ for name in (
        "_goal_warning_result", "_dispatch_poll", "_dispatch_cancel"))
    coro.close()
asyncio.run = run
runpy.run_module("booley.mcp.server", run_name="__main__")
"""
    env = dict(os.environ)
    env.pop("CLAUDECODE", None)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "src")
    result = subprocess.run(
        [sys.executable, "-c", script],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("edit_after_stamp", [False, True])
def test_custom_tool_stamp_uses_declared_target_without_recapturing(
    goal_mode, monkeypatch, edit_after_stamp
):
    from booley.criteria.state import DevelopmentState
    from booley.flows.endpoint_state import EndpointState
    from booley.goals.flow_execution import GoalFlowExecution
    from booley.goals.status import build_status
    from tests.goals.conftest import bind

    adapter = GoalFlowExecution(bind(goal_mode))
    endpoint = SimpleNamespace(
        args=SimpleNamespace(work_dir=goal_mode.worktree), _acceptance_recorder=adapter
    )
    key = "lint_clean_top"
    # Custom MCP tools omit source_target; the producer must resolve it before
    # sampling, instead of stamping all Project sources and recapturing later.
    stamped = EndpointState._stamp_source_fingerprint(
        endpoint, key, True, {"warnings": 0}, source_target=None
    )
    assert stamped["_source_fingerprint"]["target"] == "top"
    fingerprint = json.dumps(stamped["_source_fingerprint"], sort_keys=True)
    if edit_after_stamp:
        (goal_mode.worktree / "rtl.v").write_bytes(b"module top; wire changed; endmodule\n")
    state = DevelopmentState.load(adapter.state_file, adapter.state_persistence())
    changes = state.set_criterion(key, True, detail=stamped)
    adapter.record_changes(state, changes, invocation_id="ignored", producer="custom")
    state.save()
    stored = state.criteria[key].detail["_source_fingerprint"]
    # The recorder may add target_surface, but the actual source sample is kept.
    previous = json.loads(fingerprint)
    assert stored["fingerprint"]["rtl"] == previous["fingerprint"]["rtl"]
    view = build_status(GoalStore(goal_mode.control), goal_mode.record)
    assert view.goals[0].status == ("stale" if edit_after_stamp else "met")


def test_terminal_write_uses_binding_even_if_manager_root_changes(
    goal_mode, monkeypatch, tmp_path
):
    context = resolve_call_context({"work_dir": str(goal_mode.worktree)})
    rec = job_records.JobRecord("bound-terminal", "lint", utc_now_rfc3339(), 60)
    jobs = server._JobManager(server._McpLifetime(None, None))
    jobs._stamp_context(rec, context)
    jobs._job_roots[rec.run_id] = tmp_path / "wrong"
    monkeypatch.setattr(
        server, "_locate_job", lambda run_id: server.LocatedJob(rec, context.jobs_root)
    )
    jobs._record_terminal(rec, 0, timed_out=False)
    assert job_records.read_record(rec.run_id, context.jobs_root).status == "done"
    assert not (tmp_path / "wrong" / f"{rec.run_id}.json").exists()


@pytest.mark.parametrize("binding", [[], {"schema": "bad"}])
def test_malformed_retained_job_refuses_admission_and_poll(goal_mode, binding):
    root = record_paths(goal_mode.control, goal_mode.record.id).jobs_dir
    root.mkdir(parents=True)
    raw = job_records.JobRecord("invalid-binding", "unrelated", utc_now_rfc3339(), 60).to_dict()
    raw["binding"] = binding
    (root / "invalid-binding.json").write_text(json.dumps(raw))
    context = resolve_call_context({"work_dir": str(goal_mode.worktree)})
    jobs = server._JobManager(server._McpLifetime(None, None))
    admission = asyncio.run(
        server._dispatch_async_job("lint", ["fake"], 60, jobs, context=context)
    )
    assert admission.is_error
    assert "binding" in admission.value[0].text
    poll = asyncio.run(
        server._dispatch_poll({"run_id": "invalid-binding", "wait_seconds": 0}, jobs)
    )
    assert poll.is_error
    assert "binding" in poll.value[0].text


@pytest.mark.timeout(120)
@pytest.mark.parametrize("mode", ["done", "clean"])
def test_reviewer_uses_goal_freshness_without_rewriting_producer_evidence(
    layout, monkeypatch, mode
):
    from unittest.mock import MagicMock

    from booley.criteria.state import DevelopmentState
    from booley.goals.flow_execution import GoalFlowExecution
    from booley.goals.status import build_status
    from booley.specialists.reviewer import ReviewerSpecialist
    from tests.goals.conftest import bind, enter_goals

    layout.record = enter_goals(
        layout,
        [
            {"family": "lint", "target": "top"},
            {"family": "review", "review": "rtl_bugs", "verdict": mode},
        ],
    )
    producer = GoalFlowExecution(bind(layout))
    state = DevelopmentState.load(producer.state_file, producer.state_persistence())
    changes = state.set_criterion("lint_clean_top", True, detail={"warnings": 0})
    producer.record_changes(state, changes, invocation_id="ignored", producer="lint")
    state.save()
    original = json.loads(producer.state_file.read_text())["criteria"]["lint_clean_top"]
    context = resolve_call_context({"work_dir": str(layout.worktree)})
    for key, value in context.subprocess_env_overrides.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("BOOLEY_TICKET_FILE", raising=False)
    endpoint = ReviewerSpecialist()
    endpoint.parse_args(
        [
            "--work-dir",
            str(layout.worktree),
            "--scope",
            "rtl.v",
            "--category",
            "rtl",
            "--focus",
            "bugs",
        ]
    )
    endpoint.read_state()
    provider = MagicMock(
        return_value=MagicMock(
            output=json.dumps({"issues": []}), captured_agent_capability_calls={}
        )
    )
    monkeypatch.setattr(endpoint, "_invoke_agent", provider)
    assert endpoint._run().exit_code == 0
    assert json.loads(producer.state_file.read_text())["criteria"]["lint_clean_top"] == original
    view = build_status(GoalStore(layout.control), layout.record)
    assert all(goal.status == "met" for goal in view.goals)
    assert provider.call_count == 1
    assert endpoint._run().exit_code == 0
    assert provider.call_count == 1  # fresh Goal receipt is replayed
    (layout.worktree / "rtl.v").write_bytes(b"module top; wire changed; endmodule\n")
    assert endpoint._run().exit_code == 0
    assert provider.call_count >= 2  # stale receipt runs verify/discovery as appropriate
    assert json.loads(producer.state_file.read_text())["criteria"]["lint_clean_top"] == original
    assert build_status(GoalStore(layout.control), layout.record).goals[0].status == "stale"


def _goal_reviewer_endpoint(layout, monkeypatch):
    from unittest.mock import MagicMock

    from booley.specialists.reviewer import ReviewerSpecialist

    context = resolve_call_context({"work_dir": str(layout.worktree)})
    for key, value in context.subprocess_env_overrides.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("BOOLEY_TICKET_FILE", raising=False)
    endpoint = ReviewerSpecialist()
    endpoint.parse_args(
        [
            "--work-dir",
            str(layout.worktree),
            "--scope",
            "rtl.v",
            "--category",
            "rtl",
            "--focus",
            "bugs",
        ]
    )
    endpoint.read_state()
    provider = MagicMock(
        return_value=MagicMock(
            output=json.dumps({"issues": []}),
            captured_agent_capability_calls={},
        )
    )
    monkeypatch.setattr(endpoint, "_invoke_agent", provider)
    return endpoint, provider


@pytest.mark.timeout(120)
@pytest.mark.parametrize("mode", ["done", "clean"])
@pytest.mark.parametrize("after_freshness_check", [False, True])
def test_reviewer_interleaving_does_not_replay_or_save_obsolete_goal_receipt(
    layout, monkeypatch, mode, after_freshness_check
):
    from booley.goals.status import build_status
    from tests.goals.conftest import enter_goals

    layout.record = enter_goals(
        layout, [{"family": "review", "review": "rtl_bugs", "verdict": mode}]
    )
    initial, _ = _goal_reviewer_endpoint(layout, monkeypatch)
    assert initial._run().exit_code == 0
    old, old_provider = _goal_reviewer_endpoint(layout, monkeypatch)
    key = f"review_rtl_bugs_{mode}"
    path = record_paths(layout.control, layout.record.id).state_file
    old_receipt = json.loads(path.read_text())["criteria"][key]
    newer_receipt = None

    def publish_newer():
        nonlocal newer_receipt
        (layout.worktree / "rtl.v").write_bytes(b"module top; wire changed; endmodule\n")
        newer, provider = _goal_reviewer_endpoint(layout, monkeypatch)
        assert newer._run().exit_code == 0
        assert provider.call_count >= 1
        newer_receipt = json.loads(path.read_text())["criteria"][key]
        assert (
            newer_receipt["detail"]["_source_fingerprint"]
            != old_receipt["detail"]["_source_fingerprint"]
        )
        assert build_status(GoalStore(layout.control), layout.record).goals[0].status == "met"

    if after_freshness_check:
        method = "_replay_done_verdict" if mode == "done" else "_already_clean_result"
        replay = getattr(old, method)

        def interleave(crit_key):
            publish_newer()
            return replay(crit_key)

        monkeypatch.setattr(old, method, interleave)
    else:
        publish_newer()
    assert old._run().exit_code == 0
    actual = json.loads(path.read_text())["criteria"][key]
    assert build_status(GoalStore(layout.control), layout.record).goals[0].status == "met"
    if after_freshness_check:
        assert old_provider.call_count == 0
        assert actual == newer_receipt  # replay audit cannot replace a newer receipt
    else:
        assert old_provider.call_count >= 1  # the obsolete in-memory receipt was not replayed
        assert (
            actual["detail"]["_source_fingerprint"]
            == newer_receipt["detail"]["_source_fingerprint"]
        )


def test_goal_status_mcp_from_subdirectory_is_identical(goal_mode):
    nested = goal_mode.worktree / "firmware"
    nested.mkdir()
    from tests.goals.test_status import publish

    publish(goal_mode)
    root = asyncio.run(
        dispatch_goal_tool(
            "goal_status", {"work_dir": str(goal_mode.worktree)}, project_dir=goal_mode.control
        )
    )
    below = asyncio.run(
        dispatch_goal_tool("goal_status", {"work_dir": str(nested)}, project_dir=goal_mode.control)
    )
    assert below == root


def test_public_report_recovery_selects_goal_root(goal_mode, monkeypatch, tmp_path):
    from tests.goals.conftest import enter_goals, write_project_files

    second = goal_mode.control / "worktrees" / "report-other"
    git(goal_mode.main, "worktree", "add", "-q", "-b", "report-other", str(second))
    write_project_files(second / ".booley_project")
    other = SimpleNamespace(main=goal_mode.main, control=goal_mode.control, worktree=second)
    from booley.goals import entry as entry_module

    monkeypatch.setattr(entry_module, "compact_utc_now", lambda: "20261007T120000Z")
    other.record = enter_goals(other)
    interactive = tmp_path / "interactive"
    monkeypatch.setenv("BOOLEY_LOGS_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("BOOLEY_RUNTIME_DIR", str(interactive))
    monkeypatch.setenv("BOOLEY_MCP_MODE", "interactive")
    for runtime, marker in (
        (interactive, "old-interactive"),
        (record_paths(goal_mode.control, goal_mode.record.id).runtime_dir, "first-goal"),
        (record_paths(goal_mode.control, other.record.id).runtime_dir, "second-goal"),
    ):
        reports = runtime / "flow-reports"
        reports.mkdir(parents=True)
        (reports / "sim.json").write_text(
            json.dumps({"flow": "sim", "exit_code": 0, "report_text": marker})
        )
    assert "work_dir" in server._report_mcp_tool_def()["schema"]["properties"]
    omitted = server._dispatch_report({"endpoint": "sim"})
    assert omitted.is_error and "Pass work_dir" in omitted.value[0].text
    for worktree, marker in ((goal_mode.worktree, "first-goal"), (second, "second-goal")):
        result = server._dispatch_report({"endpoint": "sim", "work_dir": str(worktree)})
        content = result[0] if isinstance(result, tuple) else result
        assert marker in content[0].text
        assert "old-interactive" not in content[0].text
    goal_reports = (
        record_paths(goal_mode.control, goal_mode.record.id).runtime_dir / "flow-reports"
    )
    (goal_reports / "sim.json").unlink()
    (goal_reports / "lint.json").write_text(json.dumps({"flow": "lint"}))
    missing = server._dispatch_report({"endpoint": "sim", "work_dir": str(goal_mode.worktree)})
    assert "Reports on disk: lint." in missing[0].text
    assert "sim" not in missing[0].text.split("Reports on disk:")[1]
    assert "work_dir" in server._report_mcp_tool_def()["schema"]["properties"]
    still_omitted = server._dispatch_report({"endpoint": "sim"})
    assert still_omitted.is_error and "Pass work_dir" in still_omitted.value[0].text


def test_non_git_ordinary_directory_is_not_bound_to_an_unrelated_goal(goal_mode, tmp_path):
    ordinary = tmp_path / "ordinary"
    ordinary.mkdir()
    context = resolve_call_context({"work_dir": str(ordinary)})
    assert context.binding is None
    assert context.work_dir == ordinary


def test_goal_path_with_missing_git_metadata_refuses(goal_mode, monkeypatch):
    from booley.goals import store as store_module
    from booley.goals.store import WorktreeIdentityError
    from booley.runtime.project_repositories import GitDirectoryInspectionError

    def missing(_path):
        raise GitDirectoryInspectionError("worktree metadata missing")

    monkeypatch.setattr(store_module, "git_directories", missing)
    with pytest.raises(WorktreeIdentityError, match="metadata missing"):
        resolve_call_context({"work_dir": str(goal_mode.worktree)})


@pytest.mark.parametrize("met", [False, True])
def test_goal_status_simulation_contract_explanation(goal_mode, met):
    from booley.goals.recorder import GoalEvidenceRecorder
    from tests.goals.conftest import SIM_KEY, bind
    from tests.goals.test_recorder import _v1

    state = _v1(
        goal_mode,
        GoalEvidenceRecorder(bind(goal_mode)),
        SIM_KEY,
        met,
        {
            "required_tests": [] if met else ["smoke"],
            "passed_tests": [],
            "tests_passed": 1 if met else 0,
            "tests_total": 1,
        },
    )
    result = asyncio.run(
        dispatch_goal_tool(
            "goal_status", {"work_dir": str(goal_mode.worktree)}, project_dir=goal_mode.control
        )
    )
    assert not result.is_error
    text = " ".join(result.value[0].text.split())
    assert f"{int(met)}/1 tests" in text
    if met:
        assert state.criteria[SIM_KEY].detail["goal_contract_violation"] in text
    else:
        assert "complete resolved suite" not in text
        assert "—" not in text

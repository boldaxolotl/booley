"""Real Git baseline isolation, shared reuse and report-only lifecycle contracts."""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.core.booley_dir import reset_cache
from booley.goals.freshness import DEFAULT_RESOLVERS
from booley.goals.model import GoalState
from booley.goals.paths import record_paths
from booley.goals.sim_baseline_store import (
    INPUTS_FILE,
    baseline_jobs_dir,
    entry_inputs,
    read_document,
    request_identity,
    request_path,
    sim_goals,
    write_document,
)
from booley.goals.sim_transitions import (
    baseline_observations,
    transition_summary,
)
from booley.goals.state_store import load_goal_state
from booley.goals.status import build_status
from booley.goals.store import GoalStore
from booley.goals.test_run_freezing import TargetPlans, ensure_test_run_resolutions
from booley.mcp.sim_baseline_execution import (
    baseline_kind,
    contents_stamp,
    reap_owned_checkout,
    run_baseline,
)
from booley.mcp.sim_baseline_jobs import launch_baseline as real_launch_baseline
from booley.mcp.sim_baseline_jobs import schedule_sim_baselines
from booley.runtime import job_records
from booley.runtime.project_gitignore import PROJECT_GITIGNORE
from booley.runtime.timefmt import utc_now_rfc3339
from tests.goals.conftest import (
    SIM_KEY,
    SMOKE_RUN,
    TESTS_TOML,
    enter_goals,
    git,
    pending_baseline_launch,
    run_row,
    write_tests_toml,
)
from tests.goals.test_test_run_evidence import _record

pytestmark = pytest.mark.timeout(120)


@pytest.fixture
def baseline(layout):
    record = enter_goals(layout, [{"family": "SIM", "target": "top"}])
    return SimpleNamespace(**vars(layout), record=record, launches=[])


def _schedule(layout, record=None):
    record = record or layout.record

    def launch(path, request):
        layout.launches.append(path)
        pending_baseline_launch(path, request)

    schedule_sim_baselines(layout.control, record, launch=launch)
    goal = sim_goals(record)[0]
    path = request_path(
        layout.control,
        request_identity(layout.control, record, goal, entry_inputs(layout.control, record)),
    )
    return path, read_document(path)


def _execute(checkout, target, runs, output):
    assert checkout != output
    stamp = contents_stamp(DEFAULT_RESOLVERS.fingerprint(checkout, target=target))
    return {"rows": [run_row(run) for run in runs], "stamps": [stamp]}


def _complete(layout, execute=_execute, record=None):
    path, request = _schedule(layout, record)
    rows = run_baseline(path, request, execute=execute)
    write_document(path, request | {"status": "complete", "results": rows})
    return rows


def test_entry_capture_is_separate_from_state_and_job_is_pending(baseline):
    store = GoalStore(baseline.control)
    before = load_goal_state(store, baseline.record).to_dict()
    _schedule(baseline)
    report = baseline_observations(
        baseline.control, baseline.record, sim_goals(baseline.record)[0]
    )
    assert report["status"] == "pending"
    assert len(baseline.launches) == 1
    assert load_goal_state(store, baseline.record).to_dict() == before
    assert (
        baseline_jobs_dir(baseline.control)
        != record_paths(baseline.control, baseline.record.id).jobs_dir
    )


def test_worker_runs_base_after_candidate_edit_and_cleans_checkout(baseline):
    (baseline.worktree / "rtl.v").write_bytes(b"module edited; endmodule\n")
    checkouts = []

    def execute(checkout, target, runs, output):
        checkouts.append(checkout)
        assert (checkout / "rtl.v").read_bytes() == b"module top; endmodule\n"
        assert git(checkout, "rev-parse", "HEAD") == baseline.record.base_sha
        return _execute(checkout, target, runs, output)

    rows = _complete(baseline, execute)
    assert rows[0]["grade"] == "pass"
    assert rows[0]["proof"]["base_sha"] == baseline.record.base_sha
    assert not checkouts[0].exists()
    assert (baseline.worktree / "rtl.v").read_bytes() == b"module edited; endmodule\n"


def test_second_session_in_another_worktree_starts_no_job(baseline):
    _complete(baseline)
    other = baseline.control / "worktrees" / "second"
    git(baseline.main, "worktree", "add", "-q", "--detach", str(other), baseline.record.base_sha)
    second = replace(baseline.record, id="second-20261006T130000Z", worktree_path=str(other))
    snapshot = entry_inputs(baseline.control, baseline.record)
    write_document(record_paths(baseline.control, second.id).root / INPUTS_FILE, snapshot)
    _schedule(baseline, second)
    assert len(baseline.launches) == 1
    assert (
        baseline_observations(baseline.control, second, sim_goals(second)[0])["status"]
        == "complete"
    )


def test_changed_producer_contents_are_not_cached(baseline):
    def changed(checkout, target, runs, output):
        result = _execute(checkout, target, runs, output)
        (checkout / "rtl.v").write_bytes(b"module changed; endmodule\n")
        return result

    with pytest.raises(ValueError, match="producer contents differ"):
        _complete(baseline, changed)
    assert not list((baseline.control / "goals/sim-baselines/evidence").glob("*.json"))


def test_post_edit_producer_stamp_is_rejected_even_after_restore(baseline):
    def changed_stamp(checkout, target, runs, output):
        old = (checkout / "rtl.v").read_bytes()
        (checkout / "rtl.v").write_bytes(b"module changed; endmodule\n")
        result = _execute(checkout, target, runs, output)
        (checkout / "rtl.v").write_bytes(old)
        return result

    with pytest.raises(ValueError, match="producer contents differ"):
        _complete(baseline, changed_stamp)


@pytest.mark.parametrize("grade", ["error", "inconclusive", "unavailable"])
def test_ungraded_baseline_is_unavailable_without_goal_changes(baseline, grade):
    def unavailable(checkout, target, runs, output):
        result = _execute(checkout, target, runs, output)
        result["rows"] = [run_row(run, grade) for run in runs]
        result["reason"] = "simulation build error or timeout"
        return result

    store = GoalStore(baseline.control)
    before = load_goal_state(store, baseline.record).to_dict()
    rows = _complete(baseline, unavailable)
    assert rows[0]["grade"] == "unavailable"
    assert "timeout" in rows[0]["reason"]
    assert load_goal_state(store, baseline.record).to_dict() == before


def test_new_test_is_never_sent_to_baseline_executor(baseline):
    write_tests_toml(baseline, b'[top]\ntests = ["smoke", "new_test"]\n')
    store = GoalStore(baseline.control)
    record = ensure_test_run_resolutions(store, baseline.record.id, baseline.worktree)
    sent = []

    def execute(checkout, target, runs, output):
        sent.extend(run.test for run in runs)
        return _execute(checkout, target, runs, output)

    rows = _complete(baseline, execute, record)
    assert sent == ["smoke"]
    assert next(row for row in rows if row["test"] == "new_test")["grade"] == "new"


def test_stale_final_evidence_is_not_a_transition(baseline):
    _complete(baseline)
    _record(baseline, "pass", [run_row()])
    (baseline.worktree / "rtl.v").write_bytes(b"module changed; endmodule\n")
    goal = build_status(GoalStore(baseline.control), baseline.record).goals[0]
    summary = transition_summary(goal.sim_transitions)
    assert "current unavailable" in summary
    assert "stale" in summary
    assert "pass -> pass" not in summary


def test_new_seen_failing_comes_from_graded_matching_session_evidence(baseline):
    write_tests_toml(baseline, b'[top]\ntests = ["smoke", "new_test"]\n')
    store = GoalStore(baseline.control)
    baseline.record = ensure_test_run_resolutions(store, baseline.record.id, baseline.worktree)
    new = next(
        run
        for run in baseline.record.test_run_resolution(SIM_KEY).test_runs
        if run.test == "new_test"
    )
    _complete(baseline)
    _record(baseline, "red", [run_row(new, "fail") | {"functional_failure": True}])
    (baseline.worktree / "rtl.v").write_bytes(b"module changed; endmodule\n")
    _record(baseline, "green", [run_row(), run_row(new)])
    status = build_status(store, store.load(baseline.record.id)).goals[0]
    assert "new (seen failing) -> pass (new_test@1)" in transition_summary(status.sim_transitions)
    assert status.status == "met"


def test_dead_or_malformed_baseline_job_is_unavailable(baseline):
    _path, request = _schedule(baseline)
    job_records.write_record(
        job_records.JobRecord(
            request["job_id"],
            "sim_baseline",
            utc_now_rfc3339(),
            60,
            status=job_records.STATUS_FAILED,
        ),
        Path(request["jobs_root"]),
    )
    goal = sim_goals(baseline.record)[0]
    assert (
        baseline_observations(baseline.control, baseline.record, goal)["status"] == "unavailable"
    )
    (Path(request["jobs_root"]) / (request["job_id"] + ".json")).write_bytes(b"bad json")
    assert (
        baseline_observations(baseline.control, baseline.record, goal)["status"] == "unavailable"
    )
    assert build_status(GoalStore(baseline.control), baseline.record).goals[0].status == "unmet"


def test_finished_or_abandoned_records_never_schedule(baseline):
    for state in (GoalState.FINISHING, GoalState.FINISHED, GoalState.ABANDONED):
        schedule_sim_baselines(
            baseline.control,
            replace(baseline.record, state=state),
            launch=lambda *_args: pytest.fail("scheduled outside active Goal Mode"),
        )
    assert baseline.launches == []


def test_seed_mismatch_is_not_comparable(baseline):
    plan = TargetPlans(baseline.worktree).plan("top")
    assert baseline_kind(plan, replace(SMOKE_RUN, seed=None))[0] == "not comparable"


def test_missing_test_is_new_but_changed_arguments_are_not(baseline):
    plan = TargetPlans(baseline.worktree).plan("top")
    assert baseline_kind(plan, replace(SMOKE_RUN, test="missing"))[0] == "new"
    assert baseline_kind(plan, replace(SMOKE_RUN, args=("+other=1",)))[0] == "not comparable"


def _history(layout):
    from booley.criteria.evidence_ledger import validated_evidence_records
    from booley.goals.recorder import GOAL_SCOPE

    store = GoalStore(layout.control)
    state = load_goal_state(store, store.load(layout.record.id))
    return validated_evidence_records(
        GOAL_SCOPE, record_paths(layout.control, layout.record.id).logs_dir, state, {}
    )


@pytest.mark.parametrize(
    "defect",
    [
        None,
        "seed",
        "fingerprint",
        "same_contents",
        "missing_stamp",
        "infrastructure",
        "unclassified",
        "other_session",
    ],
)
def test_seen_failure_requires_exact_functional_evidence_across_revisions(baseline, defect):
    from copy import deepcopy

    from booley.goals.sim_transitions import seen_failing

    _record(baseline, "red", [run_row(SMOKE_RUN, "fail") | {"functional_failure": True}])
    (baseline.worktree / "rtl.v").write_bytes(b"module fixed; endmodule\n")
    _record(baseline, "green", [run_row()])
    history = deepcopy(_history(baseline))
    red, current = history[0], history[-1]
    red["goal_identity"]["spec_revisions"][SIM_KEY] = 1
    goal = replace(sim_goals(baseline.record)[0], spec_revision=2)
    if defect == "seed":
        red["detail"]["test_runs"][0]["name"] = "smoke@2"
    elif defect == "fingerprint":
        red["detail"]["test_runs"][0]["fingerprint"] = "other"
    elif defect == "same_contents":
        red["detail"]["_source_fingerprint"] = current["detail"]["_source_fingerprint"]
    elif defect == "missing_stamp":
        red["detail"].pop("_source_fingerprint")
    elif defect == "infrastructure":
        red["detail"]["test_runs"][0]["functional_failure"] = False
    elif defect == "unclassified":
        red["detail"]["test_runs"][0].pop("functional_failure")
    elif defect == "other_session":
        red["goal_identity"]["record_id"] = "other-20261006T120000Z"
    assert seen_failing(baseline.record, goal, SMOKE_RUN, current, history) is (defect is None)


@pytest.mark.parametrize(
    "baseline_grade,current_grade",
    [("pass", "pass"), ("fail", "pass"), ("pass", "fail"), ("fail", "fail")],
)
def test_worker_and_ledger_produce_the_four_existing_test_transitions(
    baseline, baseline_grade, current_grade
):
    def execute(checkout, target, runs, output):
        result = _execute(checkout, target, runs, output)
        result["rows"] = [run_row(run, baseline_grade) for run in runs]
        return result

    _complete(baseline, execute)
    (baseline.worktree / "rtl.v").write_bytes(b"module changed; endmodule\n")
    _record(baseline, "final", [run_row(SMOKE_RUN, current_grade)], met=current_grade == "pass")
    status = build_status(GoalStore(baseline.control), baseline.record).goals[0]
    assert f"{baseline_grade} -> {current_grade}" in transition_summary(status.sim_transitions)
    assert status.status == ("met" if current_grade == "pass" else "unmet")


@pytest.mark.parametrize("current_grade", ["pass", "fail"])
def test_worker_and_ledger_produce_new_test_transitions(baseline, current_grade):
    write_tests_toml(baseline, b'[top]\ntests = ["smoke", "new_test"]\n')
    store = GoalStore(baseline.control)
    baseline.record = ensure_test_run_resolutions(store, baseline.record.id, baseline.worktree)
    new = next(
        run
        for run in baseline.record.test_run_resolution(SIM_KEY).test_runs
        if run.test == "new_test"
    )
    _complete(baseline)
    _record(
        baseline, "final", [run_row(), run_row(new, current_grade)], met=current_grade == "pass"
    )
    status = build_status(store, baseline.record).goals[0]
    expected = "new (never seen failing) -> pass" if current_grade == "pass" else "new -> fail"
    assert expected in transition_summary(status.sim_transitions)
    assert new.name in transition_summary(status.sim_transitions)


@pytest.mark.parametrize("report_state", ["pending", "failed", "malformed"])
def test_finish_never_reads_baseline_jobs_and_freezes_the_report(baseline, report_state):
    import json

    from booley.goals.finish import finish_goal
    from tests.goals.test_finish import environment, request

    _path, job = _schedule(baseline)
    root = Path(job["jobs_root"])
    if report_state == "failed":
        job_records.write_record(
            job_records.JobRecord(
                job["job_id"],
                "sim_baseline",
                utc_now_rfc3339(),
                60,
                status=job_records.STATUS_FAILED,
            ),
            root,
        )
    elif report_state == "malformed":
        (root / (job["job_id"] + ".json")).write_bytes(b"not json")
    _record(baseline, "final", [run_row()])
    result = finish_goal(request(baseline, explain_html=True), environment(baseline))
    assert result["status"] == "finished"
    package = json.loads(Path(result["package"]).read_bytes())
    summary = package["goals"][0]["simulation_baseline"]["summary"]
    assert ("pending" if report_state == "pending" else "unavailable") in summary
    assert summary in result["message"]
    assert "Baseline -&gt; final" in Path(result["html"]).read_text()
    assert result["running_jobs"] == []


def test_corrupt_baseline_results_do_not_break_status_or_finish(baseline):
    path, raw = _schedule(baseline)
    write_document(path, raw | {"status": "complete", "results": [{}]})
    assert (
        baseline_observations(baseline.control, baseline.record, sim_goals(baseline.record)[0])[
            "status"
        ]
        == "unavailable"
    )
    _record(baseline, "final", [run_row()])
    assert build_status(GoalStore(baseline.control), baseline.record).goals[0].status == "met"


def test_sim_goal_added_by_approved_change_schedules_at_original_base(layout, monkeypatch):
    import asyncio

    from booley.goals.apply import ChangeEnvironment
    from booley.goals.change_service import create_proposal
    from booley.goals.entry import EntryEnvironment
    from booley.mcp.goal_tools import GOAL_PROPOSE_CHANGE, dispatch_goal_tool

    record = enter_goals(layout, [{"family": "LINT", "target": "top"}])
    (layout.worktree / "rtl.v").write_bytes(b"module changed; endmodule\n")
    env = ChangeEnvironment(
        EntryEnvironment(layout.control, routing_guidance="Explicit test routing guidance.")
    )
    proposal = create_proposal(
        env,
        record.id,
        {
            "kind": "add",
            "after": {"family": "SIM", "target": "top"},
            "rationale": "simulation required",
        },
        session_key="test",
    )
    scheduled = []
    monkeypatch.setattr(
        "booley.mcp.sim_baseline_jobs.schedule_goal_baselines",
        lambda project, rec: scheduled.append(rec),
    )
    result = asyncio.run(
        dispatch_goal_tool(
            GOAL_PROPOSE_CHANGE,
            {
                "work_dir": str(layout.worktree),
                "operation": "approve",
                "proposal_id": proposal.proposal.id,
                "reason": "simulation required",
                "approval_quote": "Add the SIM Goal.",
            },
            booley_dir=layout.control,
        )
    )
    assert result.is_error is False
    assert scheduled[0].base_sha == record.base_sha
    assert sim_goals(scheduled[0])


def test_launch_failure_is_report_only(baseline):
    def broken(*_args):
        raise OSError("no process capacity")

    schedule_sim_baselines(baseline.control, baseline.record, launch=broken)
    report = baseline_observations(
        baseline.control, baseline.record, sim_goals(baseline.record)[0]
    )
    assert report["status"] == "unavailable"
    assert "no process capacity" in report["reason"]
    assert GoalStore(baseline.control).load(baseline.record.id).state is GoalState.ACTIVE


def test_automatic_entry_launches_without_running_simulation(layout):
    from booley.goals.entry import EntryEnvironment, EntryRequest, enter_goal_mode
    from booley.goals.model import parse_goal_args
    from tests.goals.conftest import TOP_CORE

    (layout.worktree / "top.core").write_bytes(TOP_CORE.encode())
    git(layout.worktree, "add", "top.core")
    git(layout.worktree, "commit", "-qm", "core")
    write_tests_toml(layout, b'[top]\ntests = ["smoke"]\n')
    launches = []

    def launch(path, raw):
        launches.append(path)
        job_records.write_record(
            job_records.JobRecord(
                raw["job_id"], "sim_baseline", utc_now_rfc3339(), 60, pid=os.getpid(), argv=[]
            ),
            Path(raw["jobs_root"]),
        )

    env = EntryEnvironment(
        layout.control,
        schedule_baselines=lambda record: schedule_sim_baselines(
            layout.control, record, launch=launch
        ),
        routing_guidance="Explicit test routing guidance.",
    )
    record = enter_goal_mode(
        EntryRequest(
            layout.worktree, "automatic", parse_goal_args([{"family": "SIM", "target": "top"}])
        ),
        env,
    ).record
    assert record.state is GoalState.ACTIVE
    assert len(launches) == 1
    assert (
        baseline_observations(layout.control, record, sim_goals(record)[0])["status"] == "pending"
    )
    assert not (launches[0].parent / "reports").exists()


def test_entry_launch_error_never_rolls_back_goal_mode(layout):
    from booley.goals.entry import EntryEnvironment, EntryRequest, enter_goal_mode
    from booley.goals.model import parse_goal_args

    def broken(_record):
        raise OSError("baseline unavailable")

    record = enter_goal_mode(
        EntryRequest(
            layout.worktree, "report-only", parse_goal_args([{"family": "SIM", "target": "top"}])
        ),
        EntryEnvironment(
            layout.control,
            schedule_baselines=broken,
            routing_guidance="Explicit test routing guidance.",
        ),
    ).record
    assert record.state is GoalState.ACTIVE


def test_entry_cores_are_restored_after_a_stealth_edit(layout):
    core = layout.worktree / ".booley/cores/bonus.core"
    core.parent.mkdir()
    original = b"CAPI=2:\nname: ::bonus:0\ntargets:\n  extra: {toplevel: before}\n"
    core.write_bytes(original)
    record = enter_goals(layout, [{"family": "SIM", "target": "top"}])
    core.write_bytes(original.replace(b"before", b"edited"))
    mode = SimpleNamespace(**vars(layout), record=record, launches=[])

    def execute(checkout, target, runs, output):
        assert (checkout / ".booley/cores/bonus.core").read_bytes() == original
        return _execute(checkout, target, runs, output)

    assert _complete(mode, execute)[0]["grade"] == "pass"
    assert b"edited" in core.read_bytes()


def test_contents_cache_reuses_another_commit_with_identical_dependencies(baseline):
    _complete(baseline)
    git(baseline.worktree, "commit", "--allow-empty", "-qm", "unrelated metadata")
    other = replace(baseline.record, base_sha=git(baseline.worktree, "rev-parse", "HEAD"))
    rows = _complete(
        baseline, lambda *_args: pytest.fail("identical dependent contents reran"), other
    )
    assert rows[0]["grade"] == "pass"
    assert len(baseline.launches) == 2  # a new base-selection Job, but no new simulation


def test_interrupted_worker_reaps_only_its_owned_checkout(baseline):
    import subprocess
    import sys

    path, raw = _schedule(baseline)
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait(timeout=30)
    owned = baseline.control / f".baseline-wt-{process.pid}-{baseline.record.base_sha[:7]}"
    git(baseline.main, "worktree", "add", "--detach", "-q", str(owned), baseline.record.base_sha)
    unrelated = baseline.control / f".baseline-wt-{process.pid}-unrelated"
    unrelated.mkdir()
    (unrelated / "evidence.txt").write_bytes(b"keep")
    write_document(
        path,
        raw
        | {
            "owned_checkout": str(owned),
            "scratch_parent": str(owned.parent),
            "owned_paired_checkout": None,
        },
    )
    job_records.write_record(
        job_records.JobRecord(
            raw["job_id"],
            "sim_baseline",
            utc_now_rfc3339(),
            60,
            pid=process.pid,
            status=job_records.STATUS_FAILED,
        ),
        Path(raw["jobs_root"]),
    )
    _schedule(baseline)
    assert not owned.exists()
    assert str(owned) not in git(baseline.main, "worktree", "list", "--porcelain")
    assert (unrelated / "evidence.txt").read_bytes() == b"keep"
    assert read_document(path)["status"] == "unavailable"


def test_child_timeout_terminates_its_process_group(baseline, monkeypatch):
    import subprocess

    from booley.mcp.sim_baseline_execution import execute_child

    path, _raw = _schedule(baseline)
    process = SimpleNamespace(pid=os.getpid())
    process.wait = lambda **_kwargs: (_ for _ in ()).throw(
        subprocess.TimeoutExpired("simulation", 1)
    )
    calls = []
    monkeypatch.setattr(
        "booley.mcp.sim_baseline_execution.subprocess.Popen", lambda *_args, **_kwargs: process
    )
    monkeypatch.setattr(
        "booley.mcp.sim_baseline_execution.terminate_process_group",
        lambda child, group: calls.append((child, group.id)),
    )
    with pytest.raises(subprocess.TimeoutExpired):
        execute_child(baseline.worktree, "top", (SMOKE_RUN,), path.parent)
    assert calls == [(process, process.pid)]


@pytest.mark.timeout(180)
def test_review_package_highlights_new_never_seen_failing_and_dashboard_matches(baseline):
    import json

    from booley.goals.finish import finish_goal
    from booley.harness.dashboard.model import project_goal
    from tests.goals.test_finish import environment, request

    write_tests_toml(baseline, b'[top]\ntests = ["smoke", "new_test"]\n')
    store = GoalStore(baseline.control)
    baseline.record = ensure_test_run_resolutions(store, baseline.record.id, baseline.worktree)
    new = next(
        run
        for run in baseline.record.test_run_resolution(SIM_KEY).test_runs
        if run.test == "new_test"
    )
    _complete(baseline)
    _record(baseline, "final", [run_row(), run_row(new)])
    summary = transition_summary(build_status(store, baseline.record).goals[0].sim_transitions)
    result = finish_goal(request(baseline, explain_html=True), environment(baseline))
    package = json.loads(Path(result["package"]).read_bytes())
    report = package["goals"][0]["simulation_baseline"]
    assert report["summary"] == summary
    assert report["attention"] == [new.name]
    assert "Review attention: new_test@1" in result["message"]
    terminal = project_goal(store, store.load(baseline.record.id))
    assert summary in terminal.goals[0].test_runs[0]


def test_real_detached_supervisor_publishes_without_a_goal_binding(layout):
    import time

    record = enter_goals(layout, [{"family": "SIM", "target": "future", "test": "new"}])
    schedule_sim_baselines(layout.control, record, launch=real_launch_baseline)
    goal = sim_goals(record)[0]
    deadline = time.monotonic() + 15
    report = baseline_observations(layout.control, record, goal)
    while report["status"] == "pending" and time.monotonic() < deadline:
        time.sleep(0.05)
        report = baseline_observations(layout.control, record, goal)
    if report["status"] != "complete":
        diagnostic = json.dumps(report, indent=2)
        identity = request_identity(
            layout.control, record, goal, entry_inputs(layout.control, record)
        )
        log = request_path(layout.control, identity).parent / "job.log"
        if log.is_file():
            with log.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                handle.seek(max(0, handle.tell() - 8192))
                diagnostic += "\nOwned baseline job.log (last 8192 bytes):\n"
                diagnostic += handle.read(8192).decode("utf-8", errors="replace")
        pytest.fail(diagnostic)
    assert report["results"] == []
    job = job_records.read_record(report["job_id"], Path(report["jobs_root"]))
    assert job is not None and job.binding is None
    assert not list(layout.control.glob(".baseline-wt-*"))


def test_baseline_request_publication_tolerates_a_windows_reader(tmp_path, monkeypatch):
    path = tmp_path / "request.json"
    write_document(path, {"schema": "booley.sim-baseline/v1", "status": "pending"})
    replace_path = Path.replace
    blocked = False

    def replace_after_reader_closes(source, destination):
        nonlocal blocked
        if destination == path and not blocked:
            blocked = True
            error = PermissionError("request reader has the destination open")
            error.winerror = 5
            raise error
        return replace_path(source, destination)

    monkeypatch.setattr(Path, "replace", replace_after_reader_closes)
    complete = {"schema": "booley.sim-baseline/v1", "status": "complete"}
    write_document(path, complete)

    assert read_document(path) == complete
    assert list(tmp_path.iterdir()) == [path]


def _hold_reads(monkeypatch, held: dict[Path, int]):
    """Raise a Windows sharing violation on the next N opens of each path."""
    real_open = Path.open

    def open_unless_held(path, *args, **kwargs):
        if held.get(path, 0) > 0:
            held[path] -= 1
            error = PermissionError(13, "The process cannot access the file")
            error.winerror = 32
            raise error
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_unless_held)


def test_baseline_observation_reads_through_transient_windows_sharing_errors(
    baseline, monkeypatch
):
    path, request = _schedule(baseline)
    job_path = Path(request["jobs_root"]) / (request["job_id"] + ".json")
    _hold_reads(monkeypatch, {path: 1, job_path: 1})
    report = baseline_observations(
        baseline.control, baseline.record, sim_goals(baseline.record)[0]
    )
    assert report["status"] == "pending"


def test_unreadable_baseline_job_record_reports_its_cause(baseline, monkeypatch):
    _path, request = _schedule(baseline)
    job_path = Path(request["jobs_root"]) / (request["job_id"] + ".json")
    monkeypatch.setattr(
        "booley.runtime.filesystem_utils._SHARING_RETRY_DELAYS_SECONDS", (0.0,), raising=False
    )
    _hold_reads(monkeypatch, {job_path: 10**6})
    report = baseline_observations(
        baseline.control, baseline.record, sim_goals(baseline.record)[0]
    )
    assert report["status"] == "unavailable"
    assert "interrupted" not in report["reason"]
    assert "cannot be read" in report["reason"]
    assert "cannot access the file" in report["reason"]


def test_pending_request_is_published_only_after_its_job_record(baseline, monkeypatch):
    from booley.mcp import sim_baseline_jobs

    goal = sim_goals(baseline.record)[0]
    seen = []
    publish = sim_baseline_jobs.write_document

    def publish_then_observe(path, value):
        publish(path, value)
        if value.get("status") == "pending":
            seen.append(baseline_observations(baseline.control, baseline.record, goal))

    monkeypatch.setattr(sim_baseline_jobs, "write_document", publish_then_observe)
    schedule_sim_baselines(baseline.control, baseline.record, launch=lambda _path, _request: None)
    assert [report["status"] for report in seen] == ["pending"]


def test_unversioned_protected_program_is_captured_with_executable_state(layout):
    hook = layout.worktree / ".booley/hooks/build.py"
    hook.parent.mkdir()
    hook.write_bytes(b"# entry program\n")
    hook.chmod(0o755)
    record = enter_goals(layout, [{"family": "SIM", "target": "top"}])
    hook.write_bytes(b"# subsequent edit\n")
    mode = SimpleNamespace(**vars(layout), record=record, launches=[])

    def execute(checkout, target, runs, output):
        copied = checkout / ".booley/hooks/build.py"
        assert copied.read_bytes() == b"# entry program\n"
        if os.name != "nt":
            assert copied.stat().st_mode & 0o111
        return _execute(checkout, target, runs, output)

    assert _complete(mode, execute)[0]["grade"] == "pass"


def test_protected_program_mutation_during_baseline_is_rejected(layout):
    hook = layout.worktree / ".booley/hooks/build.py"
    hook.parent.mkdir()
    hook.write_bytes(b"# entry program\n")
    record = enter_goals(layout, [{"family": "SIM", "target": "top"}])
    mode = SimpleNamespace(**vars(layout), record=record, launches=[])

    def execute(checkout, target, runs, output):
        result = _execute(checkout, target, runs, output)
        (checkout / ".booley/hooks/another.py").write_bytes(b"# different controls\n")
        return result

    with pytest.raises(ValueError, match="protected directory changed"):
        _complete(mode, execute)


def test_distinct_protected_controls_never_share_baseline_evidence(baseline):
    _complete(baseline)
    changed = replace(baseline.record, protected_digest="sha256:" + "f" * 64)
    calls = []

    def execute(checkout, target, runs, output):
        calls.append(runs)
        return _execute(checkout, target, runs, output)

    _complete(baseline, execute, changed)
    assert calls == [(SMOKE_RUN,)]


def test_second_real_entry_on_same_base_reuses_baseline_without_launch(baseline):
    from booley.goals.entry import EntryEnvironment, EntryRequest, enter_goal_mode
    from booley.goals.model import parse_goal_args
    from tests.goals.conftest import write_project_files

    _complete(baseline)
    other = baseline.control / "worktrees" / "real-second"
    git(baseline.main, "worktree", "add", "--detach", "-q", str(other), baseline.record.base_sha)
    write_project_files(other / ".booley")
    (other / ".booley/tests.toml").write_bytes(b'[top]\ntests = ["smoke"]\n')
    second = enter_goal_mode(
        EntryRequest(other, "second-real", parse_goal_args([{"family": "SIM", "target": "top"}])),
        EntryEnvironment(baseline.control, routing_guidance="Explicit test routing guidance."),
    ).record
    _schedule(baseline, second)
    assert second.protected_digest == baseline.record.protected_digest
    assert len(baseline.launches) == 1


def test_authenticated_build_and_timeout_rows_are_unavailable():
    from booley.mcp.sim_baseline_execution import _exclude_uncompleted

    rows = [
        run_row(SMOKE_RUN, "fail") | {"functional_failure": False, "failure_execution": reason}
        for reason in ("blocked_by_build", "aborted")
    ]
    _exclude_uncompleted(rows)
    assert all(row["grade"] == "unavailable" for row in rows)
    assert rows[0]["reason"] == "baseline Test Run blocked_by_build"
    assert rows[1]["reason"] == "baseline Test Run aborted"


def test_baseline_jobs_are_visible_to_polling_and_dashboard(baseline, monkeypatch):
    from booley.harness.dashboard.jobs import retained_job_roots
    from booley.mcp.job_results import job_roots

    _schedule(baseline)
    expected = baseline_jobs_dir(baseline.control)
    assert expected in retained_job_roots(baseline.control, (baseline.record,))
    monkeypatch.setattr(
        "booley.mcp.job_results.project_goal_store", lambda: GoalStore(baseline.control)
    )
    assert expected in job_roots(container_root=lambda: None)


def test_child_has_an_independent_bounded_watchdog(baseline, monkeypatch):
    from booley.mcp.sim_baseline_execution import _child_watchdog

    path, _raw = _schedule(baseline)
    timers = []
    terminated = []

    class Timer:
        def __init__(self, seconds, callback):
            assert 0 < seconds <= 3600
            self.callback = callback
            timers.append(self)

        def start(self):
            self.callback()

    monkeypatch.setattr("booley.mcp.sim_baseline_execution.threading.Timer", Timer)
    monkeypatch.setattr(
        "booley.mcp.sim_baseline_execution.request_group_termination",
        lambda group: terminated.append(group.id),
    )
    timer = _child_watchdog(path.parent)
    assert timer is timers[0] and timer.daemon
    assert terminated == [os.getpid()]


@pytest.fixture
def initialized_policy(layout, isolated_git_excludes):
    """Select initialization's policy, with no ambient or blanket exclusion."""
    from booley.harness.init_cmd import InitContext, _backfill_project_gitignore
    from booley.runtime.worktree_paths import worktree_state_dir

    git(layout.main, "config", "core.excludesFile", str(isolated_git_excludes))
    exclude = layout.main / ".git/info/exclude"
    assert exclude.read_text() == "/.booley\n"
    exclude.write_bytes(b"")
    selected = worktree_state_dir(layout.worktree)
    assert selected == layout.control
    _backfill_project_gitignore(selected, InitContext(project_root=layout.main))
    assert "/.baseline-wt-*/" in (selected / ".gitignore").read_text().splitlines()
    # The local Snapshot's configuration is a normal committed Project input.
    write_tests_toml(layout, b'[top]\ntests = ["smoke"]\n')
    git(layout.worktree, "add", ".booley")
    git(layout.worktree, "commit", "-qm", "Project configuration")
    return layout


@pytest.mark.parametrize("abbrev", [7, 12])
def test_initialized_directory_policy_runs_baseline_and_reports_transition(
    initialized_policy, abbrev
):
    from booley.runtime.worktree_paths import worktree_state_dir

    layout = initialized_policy
    git(layout.main, "config", "core.abbrev", str(abbrev))
    record = enter_goals(layout, [{"family": "SIM", "target": "top"}])
    mode = SimpleNamespace(**vars(layout), record=record, launches=[])
    selected = worktree_state_dir(layout.worktree)
    policy = selected / ".gitignore"
    before = policy.read_bytes()
    short = git(layout.worktree, "rev-parse", "--short", record.base_sha)
    assert len(short) == abbrev
    scratch = selected / f".baseline-wt-{os.getpid()}-{short}"
    assert not scratch.exists()
    assert (layout.main / ".git/info/exclude").read_bytes() == b""
    checkouts = []

    def execute(checkout, target, runs, output):
        checkouts.append(checkout)
        assert checkout == scratch
        assert git(checkout, "rev-parse", "HEAD") == record.base_sha
        result = _execute(checkout, target, runs, output)
        result["rows"] = [run_row(run, "fail") for run in runs]
        return result

    assert _complete(mode, execute)[0]["grade"] == "fail"
    _record(mode, "current", [run_row()])
    summary = transition_summary(
        build_status(GoalStore(layout.control), record).goals[0].sim_transitions
    )
    assert "fail -> pass" in summary
    assert "unavailable" not in summary
    assert checkouts == [scratch]
    assert not scratch.exists()
    assert policy.read_bytes() == before


def test_current_transition_uses_history_fold(baseline):
    from booley.goals.sim_transitions import goal_transitions

    _record(baseline, "failed", [run_row(SMOKE_RUN, "fail")])
    _record(baseline, "passed", [run_row()])
    history = _history(baseline)
    for row in history:
        row["detail"].pop("test_run_ledger", None)
    rows = goal_transitions(
        baseline.control,
        baseline.record,
        sim_goals(baseline.record)[0],
        history[-1],
        history,
        baseline.worktree,
        DEFAULT_RESOLVERS,
    )
    assert rows[0]["current"] == "failed"


def _version_booley_directory(layout, excludes, tmp_path, *, submodules=False):
    """Turn the Goal worktree's Snapshot into a Paired Checkout of a standalone repository.

    ``layout.control`` (the main checkout's ``.booley``) becomes the standalone
    Versioned Booley Directory; the Goal worktree's ``.booley`` becomes a linked
    worktree of it. With ``submodules`` the RTL repository also carries one
    submodule that only Goal entry populates in the worktree.
    """
    control = layout.control
    git(layout.main, "config", "core.excludesFile", str(excludes))
    if submodules:
        dependency = tmp_path / "dependency"
        dependency.mkdir()
        git(dependency, "init", "-q", "-b", "main")
        (dependency / "source.sv").write_bytes(b"old\n")
        git(dependency, "add", "-A")
        git(dependency, "commit", "-qm", "dependency")
        # Fixture-only: the dependency is a local path, which Git blocks by default.
        git(
            layout.main,
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            str(dependency),
            "vendor/ip",
        )
        git(layout.main, "commit", "-qm", "submodule")
        git(layout.worktree, "merge", "-q", "--ff-only", "main")
        assert not (layout.worktree / "vendor/ip/source.sv").exists()
        (control / "booley.toml").write_bytes(
            b"[project]\nname = 'demo'\n[submodules]\npaths = [\"vendor/ip\"]\n"
        )
    (control / ".gitignore").write_bytes(PROJECT_GITIGNORE.encode())
    (control / "notes.txt").write_bytes(b"v1\n")
    (control / "tests.toml").write_bytes(TESTS_TOML)
    git(control, "init", "-q", "-b", "main")
    git(control, "config", "core.autocrlf", "false")
    git(control, "config", "core.excludesFile", str(excludes))
    git(control, "add", "-A")
    git(control, "commit", "-qm", "project")
    paired = layout.worktree / ".booley"
    shutil.rmtree(paired)
    git(control, "worktree", "add", "-q", "--detach", str(paired))
    return paired


@pytest.mark.parametrize("submodules", [False, True], ids=["plain", "submodules"])
def test_versioned_booley_directory_baseline_classifies_at_entry_pinned_paired_revision(
    layout, tmp_path, isolated_git_excludes, monkeypatch, submodules
):
    paired = _version_booley_directory(
        layout, isolated_git_excludes, tmp_path, submodules=submodules
    )
    record = enter_goals(layout, [{"family": "SIM", "target": "top"}])
    if submodules:
        assert (layout.worktree / "vendor/ip/source.sv").read_bytes() == b"old\n"
    pinned = record.paired_project_base_sha
    # The Paired Checkout moves on after entry; the baseline must not follow it.
    (paired / "notes.txt").write_bytes(b"v2\n")
    git(paired, "commit", "-qam", "current")
    mode = SimpleNamespace(**vars(layout), record=record, launches=[])
    path, request = _schedule(mode)
    scratch = []

    def execute(checkout, target, runs, output):
        scratch.append(checkout)
        assert checkout.resolve().is_relative_to(paired.resolve())
        assert git(checkout / ".booley", "rev-parse", "HEAD") == pinned
        assert (checkout / ".booley" / "notes.txt").read_bytes() == b"v1\n"
        if submodules:
            assert (checkout / "vendor/ip/source.sv").read_bytes() == b"old\n"
        return _execute(checkout, target, runs, output)

    monkeypatch.delenv("BOOLEY_DIR")
    reset_cache()
    try:
        rows = run_baseline(path, request, execute=execute)
    finally:
        monkeypatch.setenv("BOOLEY_DIR", str(layout.control))
        reset_cache()
    write_document(path, request | {"status": "complete", "results": rows})
    _record(mode, "current", [run_row(grade="fail")])
    summary = transition_summary(
        build_status(GoalStore(layout.control), record).goals[0].sim_transitions
    )
    assert "pass -> fail" in summary
    assert "unavailable" not in summary
    assert not scratch[0].exists()
    assert ".baseline-wt-" not in git(layout.control, "worktree", "list", "--porcelain")
    assert ".baseline-wt-" not in git(layout.worktree, "worktree", "list", "--porcelain")


def test_interrupted_versioned_baseline_reap_removes_paired_registration(
    layout, tmp_path, isolated_git_excludes, monkeypatch
):
    _version_booley_directory(layout, isolated_git_excludes, tmp_path)
    record = enter_goals(layout, [{"family": "SIM", "target": "top"}])
    mode = SimpleNamespace(**vars(layout), record=record, launches=[])
    path, request = _schedule(mode)
    scratch = []

    class KilledError(Exception):
        """Stands in for SIGKILL: the supervisor's own cleanup never runs."""

    def execute(checkout, target, runs, output):
        scratch.append(checkout)
        # The reconciler's reap of a dead supervisor's checkout (same pid prefix).
        reap_owned_checkout(read_document(path), os.getpid())
        monkeypatch.setattr(
            "booley.flows.baseline_worktree._cleanup_baseline_worktree", lambda *args: None
        )
        raise KilledError

    monkeypatch.delenv("BOOLEY_DIR")
    reset_cache()
    try:
        with pytest.raises(KilledError):
            run_baseline(path, request, execute=execute)
    finally:
        monkeypatch.setenv("BOOLEY_DIR", str(layout.control))
        reset_cache()
    assert not scratch[0].exists()
    assert ".baseline-wt-" not in git(layout.control, "worktree", "list", "--porcelain")
    assert ".baseline-wt-" not in git(layout.worktree, "worktree", "list", "--porcelain")

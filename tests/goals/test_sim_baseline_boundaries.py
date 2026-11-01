"""Fail-closed boundaries of report-only SIM baseline documents, Jobs and workers.

Each malformed or foreign input must become an explicit ``ValueError`` (a
report fact) rather than a silently accepted transition.
"""

from __future__ import annotations

import os
import sys
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.flows.test_runs import test_run_document
from booley.goals.checkout import CheckoutError
from booley.goals.sim_baseline_store import (
    SCHEMA,
    cache_path,
    cached_result,
    read_document,
    relative_path,
    result_key,
    sim_goals,
    validate_results,
    write_document,
)
from booley.goals.sim_transitions import baseline_observations, seen_failing
from booley.mcp import sim_baseline_execution as execution
from booley.mcp import sim_baseline_jobs as jobs
from booley.runtime import job_records
from booley.runtime.timefmt import utc_now_rfc3339
from tests.goals.conftest import SMOKE_RUN, enter_goals
from tests.goals.test_sim_baseline import _complete, _schedule

pytestmark = pytest.mark.timeout(120)

_DIGEST = "d" * 64


@pytest.fixture
def baseline(layout):
    """An active SIM Goal on ``top`` (as in ``test_sim_baseline``) with no launches yet."""
    record = enter_goals(layout, [{"family": "SIM", "target": "top"}])
    return SimpleNamespace(**vars(layout), record=record, launches=[])


# --- document store ---------------------------------------------------------


def test_read_document_rejects_a_foreign_schema(tmp_path: Path) -> None:
    path = tmp_path / "doc.json"
    write_document(path, {"schema": "other/v1"})
    with pytest.raises(ValueError, match="unsupported simulation baseline schema"):
        read_document(path)


@pytest.mark.parametrize("name", ["/abs", "a/../b", "a\\b", "c:x"])
def test_relative_path_refuses_unsafe_names(name: str) -> None:
    with pytest.raises(ValueError, match="unsafe simulation baseline input path"):
        relative_path(name)


def _key() -> dict[str, str]:
    return result_key("::top:0#top", SMOKE_RUN, "stamp", _DIGEST)


def _proof(**changes: str) -> dict[str, str]:
    proof = {"contents": "stamp", "protected_digest": _DIGEST, "target_identity": "::top:0#top"}
    return proof | changes


def test_cached_result_rejects_foreign_identity_and_source_proof(tmp_path: Path) -> None:
    key = _key()
    path = cache_path(tmp_path, key)
    write_document(path, {"schema": SCHEMA, "key": key, "grade": "error", "proof": _proof()})
    with pytest.raises(ValueError, match="identity or grade differs"):
        cached_result(tmp_path, key)
    write_document(
        path, {"schema": SCHEMA, "key": key, "grade": "pass", "proof": _proof(contents="x")}
    )
    with pytest.raises(ValueError, match="source proof differs"):
        cached_result(tmp_path, key)


def _request(runs=(SMOKE_RUN,), **changes):
    """A minimal validated-shape request for the one frozen smoke Test Run."""
    request = {
        "schema": SCHEMA,
        "status": "complete",
        "identity": {
            "base_sha": "a" * 40,
            "protected_digest": _DIGEST,
            "runs": [test_run_document(run) for run in runs],
        },
        "results": [],
    }
    return request | changes


def _row(grade: str = "new", **changes):
    row = {**test_run_document(SMOKE_RUN), "grade": grade}
    row["proof"] = _proof(base_sha="a" * 40)
    return row | changes


@pytest.mark.parametrize(
    "request_changes,message",
    [
        ({"status": "bogus"}, "invalid simulation baseline request status"),
        ({"results": [_row(name="other@1")]}, "not an exact requested Test Run"),
        ({"results": [_row(), _row()]}, "not an exact requested Test Run"),
        ({"results": [_row(seed=9)]}, "Test Run inputs differ"),
        ({"results": [_row(grade="bogus")]}, "invalid baseline result grade"),
        (
            {"results": [_row(proof=_proof(protected_digest="e" * 64))]},
            "protected inputs differ from entry",
        ),
        (
            {"results": [_row(proof=_proof(base_sha="b" * 40))]},
            "names another base",
        ),
        ({"results": [_row(grade="pass")]}, "differs from its contents-addressed evidence"),
        ({"results": []}, "missing Test Runs"),
    ],
)
def test_validate_results_rejects_inexact_reports(tmp_path, request_changes, message) -> None:
    with pytest.raises(ValueError, match=message):
        validate_results(tmp_path, _request(**request_changes))


def test_validate_results_accepts_a_pending_partial_report(tmp_path: Path) -> None:
    validate_results(tmp_path, _request(status="pending"))


# --- transitions ------------------------------------------------------------


def test_identity_mismatch_and_foreign_target_proof_are_reported(baseline) -> None:
    path, raw = _schedule(baseline)
    goal = sim_goals(baseline.record)[0]
    write_document(path, raw | {"identity": raw["identity"] | {"target": "other"}})
    report = baseline_observations(baseline.control, baseline.record, goal)
    assert report["status"] == "unavailable"
    assert "identity differs" in report["reason"]

    rows = _complete(baseline)
    foreign = deepcopy(rows)
    foreign[0]["proof"]["target_identity"] = "::other:0#other"
    write_document(path, read_document(path) | {"results": foreign})
    # The cached evidence no longer agrees, so the whole report is unavailable.
    assert baseline_observations(baseline.control, baseline.record, goal)["status"] == (
        "unavailable"
    )


def test_current_evidence_of_another_revision_or_missing_stamp_is_unavailable(baseline) -> None:
    from booley.goals.freshness import DEFAULT_RESOLVERS
    from booley.goals.sim_transitions import _current_outcomes

    goal = sim_goals(baseline.record)[0]
    current = {
        "goal_identity": {"record_id": "other", "spec_revisions": {}},
        "detail": {},
        "sequence": 1,
    }
    outcomes = _current_outcomes(
        baseline.record, goal, current, [current], baseline.worktree, DEFAULT_RESOLVERS
    )
    assert outcomes == ({}, "current evidence is from another Goal revision")
    # Current evidence without a contents stamp can never have been seen failing.
    assert seen_failing(baseline.record, goal, SMOKE_RUN, current, [current]) is False


def test_unresolvable_current_target_identity_is_not_comparable(baseline, monkeypatch) -> None:
    from booley.goals import sim_transitions
    from booley.goals.test_run_freezing import TargetPlanError

    class BrokenPlans:
        def __init__(self, _root):
            pass

        def identity(self, target):
            raise TargetPlanError(f"unknown Target {target}")

    monkeypatch.setattr(sim_transitions, "TargetPlans", BrokenPlans)
    goal = sim_goals(baseline.record)[0]
    assert sim_transitions._target_identity(baseline.worktree, goal) is None


# --- worker execution -------------------------------------------------------


def test_contents_stamp_requires_every_dependent_category() -> None:
    with pytest.raises(ValueError, match="cannot be fingerprinted"):
        execution.contents_stamp({})


def _restore_request(baseline, raw, **inputs):
    base = {
        "files": {},
        "links": {},
        "project_relative": ".booley",
        "executable": {},
    }
    return raw | {"inputs": base | inputs}


def test_restore_entry_inputs_refuses_captured_errors_and_bad_flags(baseline, tmp_path) -> None:
    _path, raw = _schedule(baseline)
    root = tmp_path / "checkout"
    root.mkdir()
    with pytest.raises(ValueError, match="entry inputs unavailable"):
        execution.restore_entry_inputs(
            root, raw | {"inputs": {"error": "entry inputs unavailable: boom"}}
        )
    (root / "tool.sh").write_bytes(b"#!/bin/sh\n")
    with pytest.raises(ValueError, match="executable flag must be boolean"):
        execution.restore_entry_inputs(
            root, _restore_request(baseline, raw, executable={"tool.sh": "yes"})
        )


def test_restore_entry_inputs_removes_absent_files_and_relinks(baseline, tmp_path) -> None:
    if sys.platform == "win32":
        pytest.skip("captured resolution links are symbolic links")
    _path, raw = _schedule(baseline)
    root = tmp_path / "checkout"
    (root / "rtl").mkdir(parents=True)
    (root / "gone.txt").write_bytes(b"stale")
    (root / "rtl/top.core").write_bytes(b"core")
    request = _restore_request(
        baseline,
        raw,
        files={"gone.txt": None},
        links={".booley/cores/top": "rtl"},
    )
    execution.restore_entry_inputs(root, request)
    assert not (root / "gone.txt").exists()
    link = root / ".booley/cores/top"
    assert link.is_symlink() and link.resolve() == (root / "rtl").resolve()

    (root / "outside").mkdir()
    (root / "linked").symlink_to(root / "outside", target_is_directory=True)
    with pytest.raises(ValueError, match="crosses a link"):
        execution.restore_entry_inputs(
            root, _restore_request(baseline, raw, files={"linked/file": "00"})
        )


def test_remaining_seconds_requires_a_live_watchdog_budget(baseline) -> None:
    path, raw = _schedule(baseline)
    jobs_root = Path(raw["jobs_root"])
    job_records.delete_record(raw["job_id"], jobs_root)
    with pytest.raises(ValueError, match="no valid watchdog start time"):
        execution._remaining_seconds(path.parent)
    job_records.write_record(
        job_records.JobRecord(raw["job_id"], "sim_baseline", "2000-01-01T00:00:00Z", 60),
        jobs_root,
    )
    with pytest.raises(TimeoutError, match="exceeded its total execution budget"):
        execution._remaining_seconds(path.parent)


def test_child_main_publishes_an_unavailable_result_on_failure(baseline, monkeypatch) -> None:
    path, _raw = _schedule(baseline)
    document = path.parent / "simulation-request.json"
    write_document(document, {"schema": SCHEMA, "output": str(path.parent)})

    def fail(_request):
        raise ValueError("simulator missing")

    monkeypatch.setattr(execution, "simulate", fail)
    monkeypatch.setattr(
        execution, "_child_watchdog", lambda _output: SimpleNamespace(cancel=lambda: None)
    )
    assert execution.main([]) == 2
    assert execution.main([str(document)]) == 2
    result = read_document(path.parent / "simulation-result.json")
    assert result["reason"] == "simulator missing" and result["rows"] == []


def test_baseline_execution_stamps_every_observed_fingerprint(baseline, monkeypatch) -> None:
    fingerprint = {
        category: {"digest": category, "files": []} for category in ("rtl", "tb", "target_surface")
    }
    monkeypatch.setattr(
        execution,
        "DEFAULT_RESOLVERS",
        SimpleNamespace(fingerprint=lambda *_args, **_kwargs: fingerprint),
    )
    adapter = execution.BaselineExecution(baseline.worktree, "top")
    campaign = SimpleNamespace()
    adapter.register_campaign(campaign)
    assert adapter.criterion_source_fingerprint("k", baseline.worktree, "top") == fingerprint
    assert adapter.campaigns == [campaign]
    assert adapter.stamps == [execution.contents_stamp(fingerprint)] * 2


def test_reap_owned_checkout_refuses_unproven_scratch(baseline, tmp_path) -> None:
    _path, raw = _schedule(baseline)
    execution.reap_owned_checkout(raw | {"owned_checkout": None}, 1)
    execution.reap_owned_checkout(raw | {"owned_checkout": str(tmp_path / "missing")}, 1)

    foreign = tmp_path / "scratch" / ".baseline-wt-999-abc"
    foreign.mkdir(parents=True)
    owned = raw | {"owned_checkout": str(foreign), "scratch_parent": str(foreign.parent)}
    with pytest.raises(ValueError, match="scratch ownership differs"):
        execution.reap_owned_checkout(owned, 1)

    # A child whose identity cannot be proven dead keeps the checkout in place.
    execution.reap_owned_checkout(owned | {"child_process": {"bogus": True}}, 999)
    assert foreign.exists()

    with pytest.raises(CheckoutError):
        execution.reap_owned_checkout(owned, 999)  # not a checkout at the base pin


# --- detached Job supervisor -----------------------------------------------


def test_supervisor_refuses_bad_arguments_and_unknown_jobs(baseline) -> None:
    path, raw = _schedule(baseline)
    assert jobs.main([]) == 2
    job_records.delete_record(raw["job_id"], Path(raw["jobs_root"]))
    assert jobs.main([str(path)]) == 2


def test_supervisor_cancellation_unwinds_as_interruption() -> None:
    with pytest.raises(InterruptedError, match="cancelled"):
        jobs._cancel(15, None)


def test_unstarted_worker_exit_is_published_without_a_pid(baseline) -> None:
    path, raw = _schedule(baseline)
    jobs_root = Path(raw["jobs_root"])
    job_records.write_record(
        job_records.JobRecord(raw["job_id"], "sim_baseline", utc_now_rfc3339(), 60), jobs_root
    )
    jobs._reap_unstarted(SimpleNamespace(wait=lambda: 3), path)
    job = job_records.read_record(raw["job_id"], jobs_root)
    assert job is not None and job.status == job_records.STATUS_FAILED and job.exit_code == 3
    report = read_document(path)
    assert report["status"] == "unavailable" and "exit 3" in report["reason"]

    # An unreadable request is logged, never raised from the reaper thread.
    path.write_bytes(b"not json")
    jobs._reap_unstarted(SimpleNamespace(wait=lambda: 1), path)


def test_supervisor_publishes_unavailable_when_the_baseline_fails(baseline, monkeypatch) -> None:
    path, raw = _schedule(baseline)

    def fail(_path, _request):
        raise RuntimeError("checkout failed")

    monkeypatch.setattr(execution, "run_baseline", fail)
    monkeypatch.setattr(jobs.signal, "signal", lambda *_args: None)
    assert jobs.main([str(path)]) == 2
    report = read_document(path)
    assert report["status"] == "unavailable" and "checkout failed" in report["reason"]
    job = job_records.read_record(raw["job_id"], Path(raw["jobs_root"]))
    assert job is not None and job.pid == os.getpid() and job.status == job_records.STATUS_FAILED


def test_entry_capture_failure_is_a_report_fact_not_an_entry_gate(baseline, monkeypatch) -> None:
    from booley.goals import sim_baseline_store
    from booley.goals.sim_baseline_store import capture_entry_inputs, entry_inputs

    def broken(_root):
        raise OSError("cores unreadable")

    monkeypatch.setattr(sim_baseline_store, "discover_cores", broken)
    capture_entry_inputs(baseline.control, baseline.record)
    assert "cores unreadable" in entry_inputs(baseline.control, baseline.record)["error"]


def test_scheduling_failure_never_raises_into_the_lifecycle(baseline, monkeypatch) -> None:
    def unreadable(_project, _record):
        raise ValueError("inputs unreadable")

    monkeypatch.setattr(jobs, "entry_inputs", unreadable)
    jobs.schedule_sim_baselines(
        baseline.control, baseline.record, launch=lambda *_args: pytest.fail("launched")
    )


def test_base_proof_refuses_a_checkout_off_the_goal_base(baseline) -> None:
    from tests.goals.conftest import git

    _path, raw = _schedule(baseline)
    git(baseline.worktree, "commit", "-q", "--allow-empty", "-m", "moved")
    with pytest.raises(ValueError, match="not at the Goal base commit"):
        execution.prove_base_contents(baseline.worktree, raw, {})


def test_child_result_is_read_after_a_clean_exit(baseline, monkeypatch) -> None:
    path, _raw = _schedule(baseline)
    published = {"schema": SCHEMA, "rows": [], "stamps": [], "reason": ""}
    write_document(path.parent / "simulation-result.json", published)
    process = SimpleNamespace(pid=os.getpid(), wait=lambda **_kwargs: 0)
    terminated = []
    monkeypatch.setattr(execution.subprocess, "Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(
        execution, "terminate_process_group", lambda child, _group: terminated.append(child)
    )
    result = execution.execute_child(baseline.worktree, "top", (SMOKE_RUN,), path.parent)
    assert result == published
    assert terminated == [process]
    assert "child_process" in read_document(path)


@pytest.fixture
def scratch_policy(layout, isolated_git_excludes):
    """Ignore tests use no outer blanket or host excludes."""
    from tests.goals.conftest import git

    (layout.main / ".git/info/exclude").write_bytes(b"")
    git(layout.main, "config", "core.excludesFile", str(isolated_git_excludes))
    return layout


def _select_scratch_state(layout, tmp_path, monkeypatch, selection):
    from booley.core.booley_dir import reset_cache
    from booley.runtime.worktree_paths import worktree_state_dir
    from tests.conftest import symlink_or_skip
    from tests.goals.conftest import install_paired_project

    selected, owner = layout.control, layout.main
    if selection == "custom":
        selected = layout.main / "custom-state"
        selected.mkdir()
    elif selection == "paired":
        selected = install_paired_project(layout, tmp_path)
        owner = selected
    elif selection == "symlink":
        import shutil

        selected = layout.main / "physical-state"
        selected.mkdir()
        alias = layout.worktree / ".booley"
        shutil.rmtree(alias)
        symlink_or_skip(alias, selected, target_is_directory=True)
        monkeypatch.setenv("BOOLEY_DIR", str(selected))
    if selection != "symlink":
        monkeypatch.setenv("BOOLEY_DIR", str(selected))
    reset_cache()
    if selection == "symlink":
        assert worktree_state_dir(layout.worktree) == alias
    return selected, owner


@pytest.mark.parametrize("selection", ["main", "custom", "paired", "symlink"])
def test_scratch_refusal_names_selected_owner_policy_without_creating_directory(
    scratch_policy, tmp_path, monkeypatch, selection
):
    from booley.goals.checkout import GoalCheckout
    from booley.runtime.worktree_paths import worktree_state_dir
    from tests.goals.conftest import git

    layout = scratch_policy
    selected, owner = _select_scratch_state(layout, tmp_path, monkeypatch, selection)
    policy = selected / ".gitignore"
    policy.write_bytes(b"# no scratch policy\n")
    assert worktree_state_dir(layout.worktree).resolve() == selected.resolve()
    assert GoalCheckout(selected).containing_repository()[0].resolve() == owner.resolve()
    base = git(layout.main, "rev-parse", "HEAD")
    scratch = selected / f".baseline-wt-{os.getpid()}-{base[:7]}"
    assert not scratch.exists()
    with pytest.raises(ValueError) as failure:
        execution._ignored_destination(layout.worktree, base)
    assert "not classified as ignored" in str(failure.value)
    assert "/.baseline-wt-*/" in str(failure.value)
    assert str(policy.resolve()) in str(failure.value)
    assert not scratch.exists()
    # The same selected owner accepts the prospective directory with its policy.
    policy.write_bytes(b"/.baseline-wt-*/\n")
    execution._ignored_destination(layout.worktree, base)
    assert not scratch.exists()


@pytest.mark.parametrize("exists", [False, True])
@pytest.mark.parametrize("abbrev", [7, 12])
def test_scratch_symlink_is_refused_without_touching_target(scratch_policy, exists, abbrev):
    from tests.conftest import symlink_or_skip
    from tests.goals.conftest import git

    layout = scratch_policy
    (layout.control / ".gitignore").write_bytes(b"/.baseline-wt-*/\n")
    git(layout.main, "config", "core.abbrev", str(abbrev))
    base = git(layout.main, "rev-parse", "HEAD")
    short = git(layout.worktree, "rev-parse", "--short", base)
    assert len(short) == abbrev
    target = layout.control / ".baseline-wt-target"
    if exists:
        target.mkdir()
    scratch = layout.control / f".baseline-wt-{os.getpid()}-{short}"
    symlink_or_skip(scratch, target, target_is_directory=True)
    with pytest.raises(ValueError, match=r"baseline scratch.*symlink"):
        execution._ignored_destination(layout.worktree, base)
    assert scratch.is_symlink()
    assert target.exists() is exists
    if exists:
        assert list(target.iterdir()) == []


def test_scratch_with_no_repository_owner_is_refused(scratch_policy, tmp_path, monkeypatch):
    from booley.core.booley_dir import reset_cache

    outside = tmp_path / "unowned-state"
    outside.mkdir()
    monkeypatch.setenv("BOOLEY_DIR", str(outside))
    reset_cache()
    with pytest.raises(ValueError, match="baseline scratch has no repository owner"):
        execution._ignored_destination(scratch_policy.worktree, "abcdef1")
    assert list(outside.iterdir()) == []


def test_indexed_uninitialized_gitlink_refuses_scratch_without_mutation(scratch_policy):
    from tests.goals.conftest import git

    layout = scratch_policy
    policy = layout.control / ".gitignore"
    policy.write_bytes(b"/.baseline-wt-*/\n")
    base = git(layout.main, "rev-parse", "HEAD")
    scratch = layout.control / f".baseline-wt-{os.getpid()}-{base[:7]}"
    relative = scratch.relative_to(layout.main).as_posix()
    git(layout.main, "update-index", "--add", "--cacheinfo", f"160000,{base},{relative}")
    assert not scratch.exists()
    with pytest.raises(ValueError) as failure:
        execution._ignored_destination(layout.worktree, base)
    assert "not classified as ignored" in str(failure.value)
    assert "/.baseline-wt-*/" in str(failure.value)
    assert str(policy.resolve()) in str(failure.value)
    assert "missing rule" not in str(failure.value)
    assert not scratch.exists()


def test_scratch_ignore_query_uses_discovered_owner_despite_leaked_git_dir(
    scratch_policy, tmp_path, monkeypatch
):
    from booley.goals.checkout import GoalCheckout
    from tests.goals.conftest import git

    layout = scratch_policy
    (layout.control / ".gitignore").write_bytes(b"# no local scratch rule\n")
    # Only the discovered owner's Git directory provides the deciding rule.
    (layout.main / ".git/info/exclude").write_bytes(b"/.booley/.baseline-wt-*/\n")
    base = git(layout.main, "rev-parse", "HEAD")
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    git(foreign, "init", "-q")
    monkeypatch.setenv("GIT_DIR", str(foreign / ".git"))
    assert GoalCheckout(layout.control).containing_repository()[0] == layout.main
    execution._ignored_destination(layout.worktree, base)
    assert not list(layout.control.glob(".baseline-wt-*"))


@pytest.mark.parametrize("failure", ["error", "timeout"])
def test_scratch_git_failure_retains_context_and_selected_policy(
    scratch_policy, monkeypatch, failure
):
    import subprocess

    from booley.runtime import git_ignore

    original = subprocess.run

    def fail(command, **kwargs):
        if command[1] != "check-ignore":
            return original(command, **kwargs)
        if failure == "timeout":
            raise subprocess.TimeoutExpired(command, 30)
        return subprocess.CompletedProcess(command, 128, b"", b"index unavailable")

    monkeypatch.setattr(git_ignore.subprocess, "run", fail)
    with pytest.raises(ValueError) as raised:
        execution._ignored_destination(scratch_policy.worktree, "abcdef1")
    message = str(raised.value)
    assert "timed out" in message if failure == "timeout" else "index unavailable" in message
    assert str((scratch_policy.control / ".gitignore").resolve()) in message
    assert "not classified" not in message
    assert not list(scratch_policy.control.glob(".baseline-wt-*"))

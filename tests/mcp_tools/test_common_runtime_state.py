"""Session and Job runtime state lives in the Main Booley Directory (#2603, ADR 0082)."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from booley.runtime import job_records
from booley.runtime.booley_dir import reset_cache
from booley.runtime.session_paths import project_job_roots, session_jobs_dir
from booley.runtime.timefmt import rfc3339_from_epoch
from tests.booley_checkouts_support import enter_worktree


@pytest.fixture
def worktree(tmp_path, monkeypatch) -> Iterator[tuple[Path, Path]]:
    """``(Main Booley Directory, worktree)`` with the CWD in the worktree."""
    # Empty means unset, and monkeypatch restores the original after the server sets it.
    monkeypatch.setenv("BOOLEY_LOGS_DIR", "")
    monkeypatch.setenv("BOOLEY_RUNTIME_DIR", "")
    monkeypatch.setenv("BOOLEY_SLOTS_DIR", "")
    yield enter_worktree(tmp_path, monkeypatch)
    reset_cache()


def test_worktree_mcp_server_jobs_are_visible_from_main(worktree, monkeypatch):
    from booley.mcp import handle_pruning
    from booley.mcp.server import _maybe_configure_job_logs_dir

    main, _worktree = worktree
    _maybe_configure_job_logs_dir()
    record = job_records.JobRecord("run-1", "sim", rfc3339_from_epoch(1_800_000_000.0), 60)
    root = session_jobs_dir()
    job_records.write_record(record, root)

    # Another server (or the Dashboard) reading the Project's Main Booley Directory.
    monkeypatch.delenv("BOOLEY_LOGS_DIR")
    assert root in project_job_roots(main)
    found, _unreadable = handle_pruning._read_job_records(main, {"run-1"})
    assert "run-1" in found


def test_servers_of_different_checkouts_keep_separate_logs(worktree, monkeypatch):
    from booley.mcp.server import _maybe_configure_job_logs_dir

    main, _tree = worktree
    _maybe_configure_job_logs_dir()
    worktree_logs = Path(os.environ["BOOLEY_LOGS_DIR"])
    monkeypatch.setenv("BOOLEY_LOGS_DIR", "")
    monkeypatch.chdir(main.parent)
    reset_cache()
    _maybe_configure_job_logs_dir()
    main_logs = Path(os.environ["BOOLEY_LOGS_DIR"])
    # Both servers share the host's name, yet neither sees the other's Jobs or sessions.
    assert worktree_logs.parent == main_logs.parent == main / "job_logs"
    assert worktree_logs != main_logs


@pytest.mark.parametrize("sandbox_like", [False, True], ids=["host", "sandbox"])
def test_reviewer_writes_evidence_to_main_and_no_review_state(worktree, monkeypatch, sandbox_like):
    from booley.runtime import runtime_context
    from booley.specialists.reviewer import ReviewerSpecialist

    main, tree = worktree
    if sandbox_like:
        monkeypatch.setenv("BOOLEY_DIR", str(main))
        reset_cache()
    monkeypatch.delenv("BOOLEY_STATE_FILE", raising=False)
    (tree / "rtl").mkdir()
    (tree / "rtl/dut.sv").write_text("module dut; endmodule\n")
    issue = {
        "severity": "MAJOR",
        "confidence": "HIGH",
        "category": "bugs",
        "file": "rtl/dut.sv",
        "line": 1,
        "summary": "Wrong reset value",
        "kind": "code_defect",
        "disposition": "current",
        "ticket_clause": "Reset clears output",
    }
    argv = [
        "--work-dir",
        str(tree),
        "--scope",
        "rtl/dut.sv",
        "--category",
        "rtl",
        "--focus",
        "bugs",
    ]
    monkeypatch.setattr(runtime_context, "inside_session_runtime", lambda: True)
    monkeypatch.setattr(
        ReviewerSpecialist,
        "_invoke_agent",
        lambda _self, _params: MagicMock(
            output=json.dumps({"issues": [issue]}), captured_agent_capability_calls={}
        ),
    )
    for _ in range(2):
        endpoint = ReviewerSpecialist()
        result = endpoint.execute_cli(argv)
        assert result.exit_code == 0, result.outcome.report_text
        assert "RESULT: REVIEWED WITH FINDINGS" in result.outcome.report_text
    evidence = sorted((main / "reviewer-evidence").glob("*.json"))
    assert len(evidence) == 2
    assert not (main / "reviewer-state").exists()


def test_session_registry_and_prune_use_main(worktree, monkeypatch):
    from booley.mcp import handle_pruning
    from booley.mcp.session_observer import SessionObserver

    main, _worktree = worktree
    assert SessionObserver()._registry().booley_dir == main

    pruned: list[Path] = []

    def spy(directory: Path, *, now: float) -> None:
        pruned.append(directory)

    monkeypatch.setattr(handle_pruning, "prune_project_handles", spy)
    handle_pruning._prune_quietly()
    assert pruned == [main]


def test_unbound_job_handle_registers_in_main(worktree, monkeypatch):
    from booley.mcp import job_lifecycle

    main, _worktree = worktree
    registered: list[Path] = []
    monkeypatch.setattr(
        job_lifecycle, "register_job", lambda project, *_args, **_kw: registered.append(project)
    )
    context = SimpleNamespace(work_dir=None, explicit_work_dir=None, session_key="pid:k")
    record = job_records.JobRecord("run-1", "sim", rfc3339_from_epoch(1_800_000_000.0), 60)
    job_lifecycle.JobLifecycle._stamp_context(record, context)
    assert registered == [main]


def test_job_slot_store_is_in_main(worktree, monkeypatch):
    from booley.runtime import job_slots

    main, _worktree = worktree
    root = main / "runtime" / "jobs" / "slots"
    assert job_slots.slots_dir() == root
    verifier = job_slots._default_inherited_execution_verifier
    # Only the Main Booley Directory's root is canonical; verification itself is stubbed.
    monkeypatch.setattr(job_slots, "verify_inherited_execution_id", lambda *_a, **_kw: "verified")
    assert verifier("anything", root, 1) == "verified"
    assert verifier("anything", worktree[1] / ".booley/runtime/jobs/slots", 1) is None


def test_dashboard_and_chat_handles_use_main(worktree, monkeypatch):
    from booley.harness import chat_cmd
    from booley.harness.dashboard import command

    main, tree = worktree
    blockers: list[Path] = []
    monkeypatch.setattr(command, "attach_blocker", lambda _root, d: blockers.append(d) or "")
    monkeypatch.setattr(command, "_dashboard_is_open", lambda: False)
    command.attach_policy(tree)
    assert blockers == [main]

    reserved: list[Path] = []
    monkeypatch.setattr(
        chat_cmd, "reserve_session_handle", lambda d, _key, **_kw: reserved.append(d) or "handle"
    )
    chat_cmd._claude_command(tree, [], strict=False)
    assert reserved == [main]


def test_doctor_reads_session_state_from_main(worktree, monkeypatch):
    from booley.harness import doctor

    main, tree = worktree
    handle_maps: list[Path] = []
    monkeypatch.setattr(
        doctor, "_check_handle_map_size", lambda d, _root, _warn: handle_maps.append(d)
    )
    for name in (
        "_check_ticket_leftovers",
        "_check_project_gitignore",
        "_check_project_trackability",
        "_check_git_state",
        "_check_repo_footprint",
        "_check_custom_endpoints",
        "_check_disabled_endpoints",
    ):
        monkeypatch.setattr(doctor, name, lambda *_a, **_kw: None)
    project = SimpleNamespace(
        project_root=tree, booley_dir=tree / ".booley", common_booley_dir=main
    )
    reporter = doctor._Reporter.create()
    reporter.agent_check_enabled = lambda _name: False
    doctor._run_project_checks(project, reporter)
    assert handle_maps == [main]


def test_doctor_reports_a_skip_when_the_main_directory_is_unresolvable(tmp_path, monkeypatch):
    from booley.harness import doctor

    def broken(_root):
        raise RuntimeError("malformed .git marker")

    monkeypatch.setattr(doctor, "resolve_common_booley_dir", broken)
    reporter = doctor._Reporter.create()
    assert doctor._common_booley_dir(tmp_path, reporter) is None
    assert reporter.counts["skip"] == 1

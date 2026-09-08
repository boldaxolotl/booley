"""Unaccepted review entry must not cross the acceptance boundary."""

from pathlib import Path
from types import SimpleNamespace

from booley.ticket_board.cli_handlers import _cmd_move_ticket
from booley.ticket_board.io import TicketIO


def test_mechanical_move_cannot_create_unaccepted_review(tmp_path: Path):
    tickets = tmp_path / ".booley_project" / "tickets"
    active = tickets / "board" / "active"
    active.mkdir(parents=True)
    ticket = active / "demo.md"
    ticket.write_text(
        "---\nsummary: Synthetic review transition\ntype: feature\nbranch: demo\n---\n"
    )
    tio = TicketIO(tickets, project_root=tmp_path)
    assert _cmd_move_ticket(tio, SimpleNamespace(slug="demo", to="review")) != 0
    assert ticket.exists()


import asyncio
import json

import pytest

from booley.criteria.state import DevelopmentState
from booley.review import preparation as prep
from booley.review.entry import read_entry
from booley.review.requests import request_review_command
from booley.ticket_board.io import TicketFileSpec
from booley.ticket_board.logs import save_progress
from tests.ticket_board.test_acceptance_basis import _basis_project


@pytest.fixture
def blocked(tmp_path, monkeypatch, request):
    from booley.runtime.project_dir import reset_cache

    monkeypatch.delenv("BOOLEY_PROJECT_DIR", raising=False)
    reset_cache()
    options = getattr(request, "param", {})
    from tests.ticket_board.test_acceptance_basis import _paired_basis_project

    factory = _paired_basis_project if options.get("paired") else _basis_project
    root, project, tio = factory(tmp_path)
    declared = options.get("criterion", "review_rtl_bugs_done")
    if declared == "implementation_done":
        from tests.ticket_board.test_acceptance_basis import _git

        (project / "criteria.toml").write_text(
            '[implementation_done]\ndescription = "Fixture verification"\ncategory = "none"\n'
        )
        tools_dir = project / "mcp_tools"
        tools_dir.mkdir()
        (tools_dir / "verify_fixture.py").write_text("""
from booley.mcp.base import McpTool, McpToolResult
class Verify(McpTool):
    name = "verify_fixture"
    description = "Synthetic verification endpoint"
    satisfies = ["implementation_done"]
    config_aware = False
    def _add_args(self, parser):
        pass
    def _run(self):
        self.set_criterion("implementation_done", True)
        return McpToolResult(exit_code=0, criterion_key="implementation_done", criterion_met=True)
if __name__ == "__main__":
    raise SystemExit(Verify().main())
""")
        _git(root, "add", "-f", ".booley_project/criteria.toml", ".booley_project/mcp_tools")
        _git(root, "commit", "-m", "Register fixture criterion")
    path = tio.create_ticket_file(
        "demo",
        TicketFileSpec(
            summary="Inspect incomplete work",
            ticket_type="feature",
            branch="main",
            scope=["README.md"],
            criteria={"mandatory": {declared: True}},
            on_success={"triage_report": options.get("model", False)},
        ),
    )
    assert path is not None
    assert tio.enqueue_ticket("demo")
    assert tio.move_ticket_file("demo", "blocked")
    log_dir = tio.logs_dir / "demo"
    log_dir.mkdir(parents=True, exist_ok=True)
    board = tio.find_ticket("demo")
    (log_dir / "ticket.md").write_bytes((tio.tickets_dir / board["file"]).read_bytes())
    worktree = project / "worktrees" / "demo"
    state = DevelopmentState.load(log_dir / ".runtime" / "booley_state.json")
    state.slug, state.ticket_type, state.work_dir = "demo", "feature", str(worktree)
    from booley.criteria.templates import CriteriaTemplate

    criteria = CriteriaTemplate.from_yaml({"mandatory": {declared: True}}).expand([])
    criteria["_report_submitted"] = True
    state.init_criteria(criteria, strict=True)
    state.save()
    save_progress(
        tio.logs_dir,
        "demo",
        {"blocked_reason": "Verification incomplete", "execution_id": "first"},
    )
    yield root, tio, worktree
    reset_cache()


@pytest.mark.parametrize("blocked", [{}, {"paired": True}], indirect=True)
def test_blocked_request_generates_readable_unaccepted_package(blocked):
    root, tio, worktree = blocked
    outcome = asyncio.run(request_review_command(root, "demo", reason="Finish interactively"))
    assert outcome.ready, outcome.message
    assert tio.find_ticket("demo")["status"] == "review"
    assert worktree.exists()
    row = read_entry(tio.logs_dir / "demo")
    assert row["disposition"] == "unaccepted"
    assert not (tio.logs_dir / "demo" / "acceptance" / "accepted.json").exists()
    briefing = prep.review_briefing_command(root, "demo", open_diffs=False)
    assert briefing.status == "ready", briefing.message
    assert "unaccepted" in briefing.briefing
    assert "**approve**" not in briefing.briefing
    assert "review_rtl_bugs_done" in briefing.briefing


def test_dirty_request_preserves_work_and_blocked_state(blocked):
    root, tio, worktree = blocked
    (worktree / "README.md").write_text("unfinished edits")
    for content in ("first edit", "second edit"):
        (worktree / "README.md").write_text(content)
        outcome = asyncio.run(request_review_command(root, "demo", reason="inspect"))
        assert not outcome.ready
        assert "commit review source" in outcome.message
        assert tio.find_ticket("demo")["status"] == "blocked"
        assert (worktree / "README.md").read_text() == content


def test_missing_observations_are_visible(blocked):
    root, tio, _ = blocked
    (tio.logs_dir / "demo" / ".runtime" / "booley_state.json").unlink()
    outcome = asyncio.run(request_review_command(root, "demo", reason="inspect missing evidence"))
    assert outcome.ready, outcome.message
    package = json.loads(outcome.package_path.read_text())
    assert package["criteria"]
    assert all(row["availability"] == "unavailable" for row in package["criteria"])


def test_failed_refresh_retains_previous_generation(blocked, monkeypatch):
    root, tio, _ = blocked
    first = asyncio.run(request_review_command(root, "demo", reason="inspect"))
    assert first.ready, first.message
    original = first.package_path.read_bytes()
    before = read_entry(tio.logs_dir / "demo")

    async def fail(*args, **kwargs):
        return prep.ReviewPrepOutcome("failed", "synthetic report failure")

    monkeypatch.setattr(prep, "_prepare_resolved_review", fail)
    outcome = asyncio.run(request_review_command(root, "demo", action="refresh"))
    assert not outcome.ready
    assert first.package_path.read_bytes() == original
    assert read_entry(tio.logs_dir / "demo") == before


def test_repair_stranded_review_requires_explicit_option(blocked):
    root, tio, _ = blocked
    path = tio.tickets_dir / tio.find_ticket("demo")["file"]
    dest = tio.tickets_dir / "board" / "review" / path.name
    dest.parent.mkdir(parents=True)
    path.replace(dest)
    assert not asyncio.run(request_review_command(root, "demo", reason="repair")).ready
    outcome = asyncio.run(request_review_command(root, "demo", reason="repair", repair=True))
    assert outcome.ready, outcome.message
    assert prep.review_briefing_command(root, "demo", open_diffs=False).status == "ready"


def test_unaccepted_completion_and_finalization_reject_unmet_gates(blocked):
    from booley.ticket_board.operations import _completion_acceptance_valid

    root, tio, _ = blocked
    assert asyncio.run(request_review_command(root, "demo", reason="inspect")).ready
    assert _completion_acceptance_valid(tio, "demo") is None
    outcome = asyncio.run(request_review_command(root, "demo", action="finalize"))
    assert not outcome.ready
    assert "acceptance is" in outcome.message
    assert read_entry(tio.logs_dir / "demo")["disposition"] == "unaccepted"


def test_concurrent_mutator_is_fenced_during_generation(blocked, monkeypatch):
    from booley.review import requests
    from booley.review.entry import ReviewEntryError

    root, tio, _ = blocked
    original = prep._prepare_resolved_review

    async def generate(*args, **kwargs):
        # Simulate another process without relying on scheduling or sleeps.
        operation = requests.read_json(requests.operation_path(tio.logs_dir / "demo"))
        operation["pid"] = 999999
        requests._write(requests.operation_path(tio.logs_dir / "demo"), operation)
        monkeypatch.setattr("booley.review.entry.is_pid_alive", lambda pid: True)
        with pytest.raises(ReviewEntryError, match="active review"):
            tio.move_ticket_file("demo", "queue")
        operation["pid"] = __import__("os").getpid()
        requests._write(requests.operation_path(tio.logs_dir / "demo"), operation)
        return await original(*args, **kwargs)

    monkeypatch.setattr(prep, "_prepare_resolved_review", generate)
    outcome = asyncio.run(request_review_command(root, "demo", reason="inspect"))
    assert outcome.ready, outcome.message


@pytest.mark.parametrize("boundary", ["entry", "board"])
def test_publication_recovers_without_duplicate_transition(blocked, monkeypatch, boundary):
    from booley.review import requests

    root, tio, _ = blocked
    original = requests._write
    original_append = TicketIO._append_transition_unlocked

    def fail_write(path, value):
        original(path, value)
        if boundary == "entry" and path.name == "entry.json":
            raise OSError("synthetic interruption after entry publication")

    def fail_append(*args, **kwargs):
        if boundary == "board":
            raise OSError("synthetic interruption after board move")
        return original_append(*args, **kwargs)

    monkeypatch.setattr(requests, "_write", fail_write)
    monkeypatch.setattr(TicketIO, "_append_transition_unlocked", fail_append)
    outcome = asyncio.run(request_review_command(root, "demo", reason="inspect"))
    assert not outcome.ready
    monkeypatch.setattr(requests, "_write", original)
    monkeypatch.setattr(TicketIO, "_append_transition_unlocked", original_append)
    recovered = asyncio.run(request_review_command(root, "demo", reason="inspect"))
    assert recovered.ready, recovered.message
    assert tio.find_ticket("demo")["status"] == "review"
    transitions = (tio.logs_dir / "demo" / "human-logs" / "transitions.log").read_text()
    assert transitions.count("review-entry") == 1


def test_changed_evidence_rejects_publication(blocked, monkeypatch):
    root, tio, _ = blocked
    original = prep._prepare_resolved_review

    async def change(*args, **kwargs):
        outcome = await original(*args, **kwargs)
        path = tio.logs_dir / "demo" / ".runtime" / "booley_state.json"
        value = json.loads(path.read_text())
        value["criteria"]["review_rtl_bugs_done"]["met"] = True
        path.write_text(json.dumps(value))
        return outcome

    monkeypatch.setattr(prep, "_prepare_resolved_review", change)
    outcome = asyncio.run(request_review_command(root, "demo", reason="inspect"))
    assert not outcome.ready
    assert "changed" in outcome.message
    assert tio.find_ticket("demo")["status"] == "blocked"


def test_report_disabled_package_tampering_is_rejected(blocked):
    root, _tio, _ = blocked
    outcome = asyncio.run(request_review_command(root, "demo", reason="inspect"))
    assert outcome.ready
    outcome.package_path.write_text("{}")
    briefing = prep.review_briefing_command(root, "demo", open_diffs=False)
    assert briefing.status != "ready"


@pytest.mark.parametrize("blocked", [{"model": True}], indirect=True)
def test_model_cannot_approve_unaccepted_work(blocked, monkeypatch):
    from unittest.mock import AsyncMock

    from booley.core.models import AgentResult
    from tests.review.test_preparation import _assessment, _explanation

    root, _tio, _ = blocked
    monkeypatch.setattr(prep, "load_models_config", lambda root: None)
    monkeypatch.setattr(
        prep,
        "_invoke_agent",
        AsyncMock(
            return_value=AgentResult(
                structured={"assessment": _assessment(), "explanation": _explanation()},
            )
        ),
    )
    result = asyncio.run(request_review_command(root, "demo", reason="inspect"))
    assert result.ready, result.message
    assert result.html_path is not None
    assert "Acceptance: unaccepted" in result.html_path.read_text()
    assert json.loads(result.package_path.read_text())["assessment"]["recommendation"] == "hold"
    assert prep.review_briefing_command(root, "demo", open_diffs=False).status == "ready"


@pytest.mark.parametrize("blocked", [{"criterion": "implementation_done"}], indirect=True)
@pytest.mark.parametrize("interrupt", [False, True])
def test_scoped_endpoint_records_real_evidence_then_requires_run_report(
    blocked, monkeypatch, interrupt
):
    import sys

    from booley.review.interactive import run_review_command

    root, tio, worktree = blocked
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).resolve().parents[2] / "src"))
    result = asyncio.run(request_review_command(root, "demo", reason="finish verification"))
    assert result.ready, result.message
    endpoint = worktree / ".booley_project" / "mcp_tools" / "verify_fixture.py"
    _run_standalone_fixture(endpoint, worktree)
    assert not list((tio.logs_dir / "demo" / "acceptance" / "evidence").glob("*/record.json"))
    assert run_review_command(root, "demo", [sys.executable, str(endpoint)]) == 0
    log_dir = tio.logs_dir / "demo"
    state = DevelopmentState.load(log_dir / ".runtime" / "booley_state.json")
    assert state.criteria["implementation_done"].met
    evidence = list((log_dir / "acceptance" / "evidence").glob("*/record.json"))
    assert evidence
    records = [json.loads(path.read_text()) for path in evidence]
    assert any(
        row["criterion"] == "implementation_done" and row["execution_id"] != "first"
        for row in records
    )
    outcome = asyncio.run(request_review_command(root, "demo", action="finalize"))
    assert not outcome.ready
    report = [
        sys.executable,
        "-m",
        "booley.mcp.submit_run_report",
        "--summary",
        "Verified the implementation.",
        "--uncertainties",
        "Synthetic fixture only.",
        "--design-decisions",
        "No source changes were required.",
        "--file-justifications",
        "{}",
    ]
    assert run_review_command(root, "demo", report) == 0
    _finish_interactive_fixture(root, tio, interrupt, monkeypatch)


def test_review_exec_parser_keeps_board_dispatch():
    from booley.harness.booley import _build_parser

    args = _build_parser().parse_args(
        ["board", "review-exec", "demo", "--", "python", "-m", "example"]
    )
    assert args.command == "board"
    assert args.endpoint_command == ["python", "-m", "example"]


def test_refresh_selects_new_heads_while_regenerate_does_not(blocked):
    from tests.ticket_board.test_acceptance_basis import _git

    root, _tio, worktree = blocked
    first = asyncio.run(request_review_command(root, "demo", reason="inspect"))
    assert first.ready
    (worktree / "README.md").write_text("interactive correction\n")
    _git(worktree, "add", "README.md")
    _git(
        worktree,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-m",
        "correct",
    )
    refused = asyncio.run(request_review_command(root, "demo", action="regenerate"))
    assert not refused.ready
    assert "refresh-review" in refused.message
    refreshed = asyncio.run(request_review_command(root, "demo", action="refresh"))
    assert refreshed.ready, refreshed.message
    assert refreshed.package_path != first.package_path
    assert first.package_path.exists()
    assert prep.review_briefing_command(root, "demo", open_diffs=False).status == "ready"


def test_interrupted_publication_can_be_resumed_by_new_process(blocked, monkeypatch):
    from booley.review import requests

    root, tio, _ = blocked
    original = requests._commit
    monkeypatch.setattr(requests, "_commit", lambda *args: (_ for _ in ()).throw(OSError("crash")))
    failed = asyncio.run(request_review_command(root, "demo", reason="inspect"))
    assert not failed.ready
    pending_path = requests.operation_path(tio.logs_dir / "demo")
    operation = requests.read_json(pending_path)
    operation["pid"] = 99999999
    requests._write(pending_path, operation)
    monkeypatch.setattr(requests, "_commit", original)
    recovered = asyncio.run(request_review_command(root, "demo", reason="inspect"))
    assert recovered.ready, recovered.message


def test_second_request_is_idempotent(blocked):
    root, _, _ = blocked
    first = asyncio.run(request_review_command(root, "demo", reason="inspect"))
    second = asyncio.run(request_review_command(root, "demo", reason="inspect"))
    assert first.ready and second.ready
    assert first.package_path == second.package_path


def test_stale_interrupted_request_can_be_retried_without_reset(blocked, monkeypatch):
    from booley.review import requests
    from tests.ticket_board.test_acceptance_basis import _git

    root, _tio, worktree = blocked
    original = requests._commit

    def crash(*args):
        raise OSError("synthetic interruption")

    monkeypatch.setattr(requests, "_commit", crash)
    assert not asyncio.run(request_review_command(root, "demo", reason="inspect")).ready
    (worktree / "README.md").write_text("new interactive work\n")
    _git(worktree, "add", "README.md")
    _git(
        worktree,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-m",
        "correct",
    )
    monkeypatch.setattr(requests, "_commit", original)
    outcome = asyncio.run(request_review_command(root, "demo", reason="inspect"))
    assert outcome.ready, outcome.message
    assert (worktree / "README.md").read_text() == "new interactive work\n"


def test_scoped_context_rejects_foreign_worktree_and_state(blocked, monkeypatch):
    import os

    from booley.review import execution_context, interactive, requests
    from booley.review.entry import ReviewEntryError

    root, tio, worktree = blocked
    assert asyncio.run(request_review_command(root, "demo", reason="inspect")).ready
    entry = read_entry(tio.logs_dir / "demo")
    operation = {
        "pid": os.getpid(),
        "phase": "interactive",
        "token": "fixture",
        "basis_id": entry["basis_id"],
    }
    with tio._ticket_lock("demo", review_operation=True):
        _, env = interactive._environment(tio, "demo", operation)
        requests._write(requests.operation_path(tio.logs_dir / "demo"), operation)
    for key, value in env.items():
        if key.startswith("BOOLEY_") or key == "TICKETS_DIR":
            monkeypatch.setenv(key, value)
    execution_context.validate_recording(worktree)
    with pytest.raises(ReviewEntryError, match="work directory"):
        execution_context.validate_recording(root)
    monkeypatch.setenv("BOOLEY_STATE_FILE", str(root / "foreign-state.json"))
    with pytest.raises(ReviewEntryError, match="state path"):
        execution_context.validate_recording(worktree)


def test_live_job_blocks_request_without_mutation(blocked, monkeypatch):
    from booley.review import requests

    root, tio, _ = blocked
    monkeypatch.setattr(requests, "active_ticket_jobs", lambda _: [object()])
    outcome = asyncio.run(request_review_command(root, "demo", reason="inspect"))
    assert not outcome.ready
    assert "Jobs are active" in outcome.message
    assert tio.find_ticket("demo")["status"] == "blocked"


def test_missing_diff_artifact_invalidates_inspection(blocked):
    from tests.ticket_board.test_acceptance_basis import _git

    root, _, worktree = blocked
    (worktree / "README.md").write_text("changed source\n")
    _git(worktree, "add", "README.md")
    _git(
        worktree,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-m",
        "change",
    )
    result = asyncio.run(request_review_command(root, "demo", reason="inspect"))
    assert result.ready, result.message
    package = json.loads(result.package_path.read_text())
    Path(package["changed_files"][0]["diff_right"]).unlink()
    assert prep.review_briefing_command(root, "demo", open_diffs=False).status != "ready"


def _run_standalone_fixture(endpoint, worktree):
    import os
    import subprocess
    import sys

    standalone_env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("BOOLEY_") and key != "TICKETS_DIR"
    }
    subprocess.run(
        [sys.executable, str(endpoint)],
        cwd=worktree,
        env=standalone_env,
        check=True,
        timeout=30,
        capture_output=True,
    )


def _finish_interactive_fixture(root, tio, interrupt, monkeypatch):
    log_dir = tio.logs_dir / "demo"
    from booley.review import requests

    freeze = requests.freeze_acceptance

    def interrupted(*args, **kwargs):
        snapshot = freeze(*args, **kwargs)
        raise OSError(f"interrupted after acceptance {snapshot.digest}")

    if interrupt:
        monkeypatch.setattr(requests, "freeze_acceptance", interrupted)
    outcome = asyncio.run(request_review_command(root, "demo", action="finalize"))
    if interrupt:
        assert not outcome.ready
        frozen = (log_dir / "acceptance" / "accepted.json").read_bytes()
        monkeypatch.setattr(requests, "freeze_acceptance", freeze)
        outcome = asyncio.run(request_review_command(root, "demo", action="finalize"))
        assert (log_dir / "acceptance" / "accepted.json").read_bytes() == frozen
    assert outcome.ready, outcome.message
    again = asyncio.run(request_review_command(root, "demo", action="finalize"))
    assert again.ready and again.package_path == outcome.package_path
    from booley.ticket_board.operations import _completion_acceptance_valid

    assert _completion_acceptance_valid(tio, "demo") is not None
    assert read_entry(log_dir)["disposition"] == "accepted"
    from booley.ticket_board.operations import op_complete

    assert op_complete(tio, "demo", no_merge=True, no_cleanup=True)
    assert tio.find_ticket("demo")["status"] == "done"

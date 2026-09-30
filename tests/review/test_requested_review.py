"""Unaccepted review entry must not cross the acceptance boundary."""

from pathlib import Path
from types import SimpleNamespace

from booley.harness.booley import _cmd_board_show
from booley.ticket_board.cli_handlers import _cmd_move_ticket
from booley.ticket_board.io import TicketIO
from booley.ticket_board.lifecycle import TicketState
from tests.ticket_board.conftest import place_ticket


def test_mechanical_move_cannot_create_unaccepted_review(tmp_path: Path):
    tickets = tmp_path / ".booley_project" / "tickets"
    ticket = place_ticket(
        tickets,
        "demo",
        "active",
        "---\nsummary: Synthetic review transition\ntype: feature\nbranch: demo\n---\n",
    )
    tio = TicketIO(tickets, project_root=tmp_path)
    assert _cmd_move_ticket(tio, SimpleNamespace(slug="demo", to="review")) != 0
    assert ticket.exists()
    record = read_state_record(tickets, "demo")
    assert record is not None and record.state is TicketState.RUNNING


import asyncio
import json
from contextlib import nullcontext
from typing import ClassVar

import pytest

from booley.criteria.state import DevelopmentState
from booley.evidence.fields import SOURCE_FINGERPRINT_DETAIL_KEY
from booley.flows.source_fingerprint import compute_source_fingerprint
from booley.ticket_board import review_preparation as prep
from booley.ticket_board.board_layout import (
    RUNTIME_DEFAULTS,
    read_state_record,
    state_record_path,
    ticket_document_path,
    write_state_record,
)
from booley.ticket_board.review_lifecycle import request_review_command
from booley.ticket_board.review_records import ReviewEntryError, read_entry
from booley.ticket_board.ticket_history import read_closed_ticket
from tests.ticket_board.test_ticket_baseline import _basis_project


def _assert_closed_done(tio, slug):
    """Assert *slug* closed into Ticket History as done and left the board (ADR 0065)."""
    assert tio.find_ticket(slug) is None
    assert not ticket_document_path(tio.tickets_dir, slug).exists()
    assert not state_record_path(tio.tickets_dir, slug).exists()
    closed = read_closed_ticket(tio.tickets_dir, slug)
    assert closed is not None
    assert closed.block.outcome is TicketState.DONE


def _on_success(options):
    actions = ["review"]
    for enabled, action in (
        (options.get("merge", False), "merge"),
        (options.get("cleanup", False), "cleanup"),
        (options.get("model", False), "triage_report"),
    ):
        if enabled:
            actions.append(action)
    return f"[{', '.join(actions)}]"


def _add_live_freshness_target(root: Path, project: Path) -> None:
    from tests.ticket_board.test_ticket_baseline import _git

    (root / "rtl").mkdir()
    (root / "tb").mkdir()
    (root / "rtl" / "dut.sv").write_text("module dut; endmodule\n")
    (root / "tb" / "tb.sv").write_text("module tb; endmodule\n")
    (root / "design.core").write_text(
        "CAPI=2:\nname: ::design:0\nfilesets:\n"
        "  rtl: {files: [rtl/dut.sv]}\n"
        "  tb: {files: [tb/tb.sv], tags: [tb]}\n"
        "targets:\n  sim:\n    flow: sim\n"
        "    flow_options: {tool: verilator}\n"
        "    filesets: [rtl, tb]\n    toplevel: tb\n",
        encoding="utf-8",
    )
    (project / "tests.toml").write_text('[sim]\ntests = ["smoke"]\n', encoding="utf-8")
    _git(root, "add", "design.core", "rtl/dut.sv", "tb/tb.sv")
    _git(root, "add", "-f", ".booley_project/tests.toml")
    _git(root, "commit", "-m", "Add verification target")


def _initialize_live_freshness_state(state: DevelopmentState, worktree: Path) -> None:
    from booley.criteria.templates import CriteriaTemplate

    template = CriteriaTemplate.from_yaml(
        {
            "mandatory": {
                "sim_pass": ["sim"],
                "coverage": [
                    {
                        "targets": ["sim"],
                        "tests": "all",
                        "metrics": {"line": {"min_pct": 80}},
                    }
                ],
            }
        }
    )
    criteria = {**template.expand([]), "_report_submitted": True}
    state.init_criteria(criteria, criterion_params=template.expand_params([]), strict=True)
    fingerprint = compute_source_fingerprint(worktree, target="sim")
    for key in ("sim_pass_sim", "coverage_sim"):
        state.set_criterion(
            key,
            True,
            detail={
                SOURCE_FINGERPRINT_DETAIL_KEY: {
                    "categories": ["rtl", "tb"],
                    "target": "sim",
                    "fingerprint": fingerprint,
                }
            },
        )
    state.set_criterion("_report_submitted", True)


def _register_implementation_criterion(root: Path, project: Path, *, paired: bool) -> None:
    from tests.ticket_board.test_ticket_baseline import _git

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
    repository = project if paired else root
    paths = (
        ("criteria.toml", "mcp_tools")
        if paired
        else (
            ".booley_project/criteria.toml",
            ".booley_project/mcp_tools",
        )
    )
    _git(repository, "add", "-f", *paths)
    _git(repository, "commit", "-m", "Register fixture criterion")


def _initialize_blocked_state(
    tio: TicketIO,
    worktree: Path,
    declared: str,
    *,
    live_freshness: bool = False,
) -> None:
    """Create the fixture's durable blocked-review evidence."""
    from booley.criteria.templates import CriteriaTemplate

    log_dir = tio.logs_dir / "demo"
    log_dir.mkdir(parents=True, exist_ok=True)
    board = tio.find_ticket("demo")
    (log_dir / "ticket.md").write_bytes((tio.tickets_dir / board["file"]).read_bytes())
    state = DevelopmentState.load(log_dir / ".runtime" / "booley_state.json")
    state.slug, state.ticket_type, state.work_dir = "demo", "feature", str(worktree)
    if live_freshness:
        _initialize_live_freshness_state(state, worktree)
    else:
        criteria = CriteriaTemplate.from_yaml({"mandatory": {declared: True}}).expand([])
        criteria["_report_submitted"] = True
        state.init_criteria(criteria, strict=True)
    state.save()
    record = read_state_record(tio.tickets_dir, "demo")
    assert record is not None
    write_state_record(
        tio.tickets_dir,
        "demo",
        record.with_runtime(
            {"blocked_reason": "Verification incomplete", "execution_id": "first"}
        ),
    )


@pytest.fixture
def blocked(tmp_path, monkeypatch, request):
    from booley.runtime.project_dir import reset_cache

    monkeypatch.delenv("BOOLEY_PROJECT_DIR", raising=False)
    reset_cache()
    options = getattr(request, "param", {})
    from tests.ticket_board.test_ticket_baseline import _paired_basis_project

    factory = _paired_basis_project if options.get("paired") else _basis_project
    root, project, tio = factory(tmp_path)
    declared = options.get("criterion", "review_rtl_bugs_done")
    if declared == "implementation_done":
        _register_implementation_criterion(root, project, paired=bool(options.get("paired")))
    live_freshness = options.get("live_freshness", False)
    if live_freshness:
        _add_live_freshness_target(root, project)
        criteria_field = (
            "CRITERIA_MANDATORY:\n"
            "  SIM: {sim: {all: pass}}\n"
            "  COVERAGE: {sim: {tests: all, metrics: {line: {min_pct: 80}}}}\n"
        )
    else:
        criterion = (
            "IMPLEMENTATION_DONE: true"
            if declared == "implementation_done"
            else "REVIEW: {rtl: {bugs: done}}"
        )
        criteria_field = f"CRITERIA_MANDATORY: {{{criterion}}}\n"
    project_ref = "project_destination_ref: refs/heads/main\n" if options.get("paired") else ""
    path = tio.create_ticket_document(
        "demo",
        "---\n"
        "summary: Inspect incomplete work\n"
        "type: feature\n"
        "branch: main\n"
        f"{project_ref}"
        f"scope: [{'design.core, rtl/dut.sv, tb/tb.sv' if live_freshness else 'README.md'}]\n"
        f"on_success: {_on_success(options)}\n"
        f"{criteria_field}"
        "---\n\n## Description\nInspect incomplete work.\n",
    )
    assert path is not None
    assert tio.enqueue_ticket("demo")
    assert tio.move_ticket_file("demo", TicketState.BLOCKED)
    worktree = project / "worktrees" / "demo"
    _initialize_blocked_state(
        tio,
        worktree,
        declared,
        live_freshness=live_freshness,
    )
    yield root, tio, worktree
    reset_cache()


def _automatic_accepted_handoff(blocked, monkeypatch):
    """Prepare and bind a real automatic handoff without selecting an inspection."""
    from booley.ticket_board import operations
    from booley.ticket_board.ticket_baseline import validate_current_basis_refs

    root, tio, _worktree = blocked
    state = DevelopmentState.load(tio.logs_dir / "demo" / ".runtime" / "booley_state.json")
    for criterion in state.criteria:
        state.set_criterion(criterion, True)
    state.save()
    package = asyncio.run(prep.prepare_review(root, "demo"))
    assert package.ready, package.message

    assert tio.move_ticket_file("demo", TicketState.RUNNING)
    run_log = tio.logs_dir / "demo" / "human-logs" / "run.log"
    run_log.parent.mkdir(parents=True, exist_ok=True)
    run_log.write_text("# Developer Agent run log\n", encoding="utf-8")
    heads = validate_current_basis_refs(root, tio.load_basis("demo"))
    monkeypatch.setattr(operations, "_handoff_basis_heads", lambda *_args: heads)
    monkeypatch.setattr(operations, "_validate_transitions_for_handoff", lambda *_args: True)
    assert operations.op_handoff(tio, "demo", expected_execution_id="first")
    assert read_entry(tio.logs_dir / "demo") is None
    return root, tio, package


@pytest.mark.parametrize(
    "blocked", [{"merge": True, "criterion": "implementation_done"}], indirect=True
)
@pytest.mark.parametrize("no_merge", [False, True])
def test_automatic_accepted_handoff_can_be_publicly_approved(blocked, monkeypatch, no_merge):
    from booley.ticket_board import operations
    from booley.ticket_board.review_lifecycle import approve_review_command

    root, tio, _package = _automatic_accepted_handoff(blocked, monkeypatch)
    merged = []
    if not no_merge:

        def complete_with_merge(tio, slug, policy, _snapshot):
            merged.append(policy.merge)
            assert operations._approve_transition(
                tio, slug, actor="test", detail="configured merge"
            )
            operations._finish_completed_ticket(tio, slug, cleanup=False, close=True)
            return True

        monkeypatch.setattr(operations, "_complete_with_merge", complete_with_merge)

    assert approve_review_command(root, "demo", no_merge=no_merge)
    _assert_closed_done(tio, "demo")
    assert merged == ([] if no_merge else [True])


_STALE_HANDOFF_CASES = [
    {"merge": True, "criterion": "implementation_done"},
    {"merge": True, "criterion": "implementation_done", "paired": True},
]


def _stale_automatic_handoff(blocked, monkeypatch):
    from booley.ticket_board.acceptance_ledger import read_acceptance
    from tests.ticket_board.test_ticket_baseline import _git

    root, tio, worktree = blocked
    _root, _tio, prepared = _automatic_accepted_handoff(blocked, monkeypatch)
    acceptance_path = tio.logs_dir / "demo" / "acceptance" / "accepted.json"
    snapshot = read_acceptance(tio.logs_dir / "demo").snapshot
    assert snapshot is not None
    basis = tio.load_basis("demo")
    ticket_ref = basis.participant("outer").ticket_ref
    readme = worktree / "README.md"
    readme.write_text(readme.read_text(encoding="utf-8") + "\n<!-- after acceptance -->\n")
    _git(worktree, "add", "README.md")
    _git(worktree, "commit", "-m", "Change accepted source")
    return SimpleNamespace(
        root=root,
        tio=tio,
        prepared=prepared,
        acceptance_path=acceptance_path,
        acceptance_bytes=acceptance_path.read_bytes(),
        frozen_head=snapshot.participant_heads["outer"],
        ticket_ref=ticket_ref,
        ticket_ref_before=snapshot.participant_heads["outer"],
        main_before=_git(root, "rev-parse", "main"),
        live_head=_git(worktree, "rev-parse", "HEAD"),
    )


@pytest.mark.parametrize("blocked", _STALE_HANDOFF_CASES, indirect=True)
def test_stale_accepted_handoff_refuses_approval_without_state_change(
    blocked, monkeypatch, capsys
):
    from booley.ticket_board.review_lifecycle import approve_review_command
    from tests.ticket_board.test_ticket_baseline import _git

    stale = _stale_automatic_handoff(blocked, monkeypatch)

    assert not approve_review_command(stale.root, "demo", no_merge=True)
    approval_error = capsys.readouterr().err
    assert "Ticket heads changed after acceptance" in approval_error
    assert f"outer frozen: {stale.frozen_head}" in approval_error
    assert f"outer live: {stale.live_head}" in approval_error
    assert "restore the exact frozen heads" in approval_error
    assert "booley board reset demo" in approval_error
    assert stale.tio.find_ticket("demo")["status"] == "review"
    assert _git(stale.root, "rev-parse", "main") == stale.main_before
    assert stale.acceptance_path.read_bytes() == stale.acceptance_bytes
    assert _git(stale.root, "rev-parse", stale.ticket_ref) == stale.live_head
    assert stale.ticket_ref_before == stale.frozen_head
    assert not (stale.tio.logs_dir / "demo" / "acceptance-journal.json").exists()


@pytest.mark.parametrize("blocked", _STALE_HANDOFF_CASES, indirect=True)
def test_stale_accepted_handoff_surfaces_public_recovery_guidance(blocked, monkeypatch, capsys):
    from booley.harness import booley as harness
    from booley.ticket_board.review_lifecycle import review_command, run_review_command

    stale = _stale_automatic_handoff(blocked, monkeypatch)

    review = asyncio.run(review_command(stale.root, "demo"))
    assert not review.ready
    assert "Ticket heads changed after acceptance" in review.message
    assert "restore the exact frozen heads" in review.message
    with pytest.raises(ReviewEntryError, match="Ticket heads changed after acceptance") as caught:
        run_review_command(stale.root, "demo", ["echo", "must-not-run"])
    assert "restore the exact frozen heads" in str(caught.value)

    args = harness._build_parser().parse_args(["board", "show", "demo", "--no-open-diffs"])
    assert harness._cmd_board_show(args, stale.root) == 0
    briefing = capsys.readouterr().out
    assert "STALE ACCEPTANCE" in briefing
    assert f"outer frozen: {stale.frozen_head}" in briefing
    assert f"outer live: {stale.live_head}" in briefing
    assert "restore the exact frozen heads" in briefing
    assert "booley board reset demo" in briefing

    stale.prepared.package_path.write_text("{}\n", encoding="utf-8")
    assert harness._cmd_board_show(args, stale.root) == 2
    integrity_error = capsys.readouterr().err
    assert "invalid review package binding" in integrity_error
    assert "STALE ACCEPTANCE" not in integrity_error
    assert "board review" not in integrity_error


@pytest.mark.parametrize("blocked", [{"criterion": "implementation_done"}], indirect=True)
def test_current_accepted_review_validation_names_immutable_exits(blocked, monkeypatch):
    from booley.ticket_board import review_preparation
    from booley.ticket_board.review_lifecycle import run_review_command

    root, _tio, _worktree = blocked
    _automatic_accepted_handoff(blocked, monkeypatch)

    with pytest.raises(ReviewEntryError, match="accepted review is immutable") as caught:
        run_review_command(root, "demo", ["echo", "must-not-run"])
    message = str(caught.value)
    assert "booley board approve demo" in message
    assert "booley board reset demo" in message

    monkeypatch.setattr(
        review_preparation,
        "_resolve_context",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            review_preparation.ReviewPrepError("Ticket baseline refs are invalid")
        ),
    )
    with pytest.raises(ReviewEntryError, match="Ticket baseline refs are invalid"):
        run_review_command(root, "demo", ["echo", "must-not-run"])


@pytest.mark.parametrize("blocked", [{"criterion": "implementation_done"}], indirect=True)
def test_unaccepted_review_drift_is_not_stale_acceptance(blocked):
    from tests.ticket_board.test_ticket_baseline import _git

    root, _tio, worktree = blocked
    assert asyncio.run(request_review_command(root, "demo", reason="inspect")).ready
    readme = worktree / "README.md"
    readme.write_text(readme.read_text(encoding="utf-8") + "\n<!-- changed -->\n")
    _git(worktree, "add", "README.md")
    _git(worktree, "commit", "-m", "Change unaccepted review source")

    outcome = asyncio.run(request_review_command(root, "demo", action="request"))

    assert not outcome.ready
    assert "review inputs changed" in outcome.message
    assert "booley board review" in outcome.message
    assert "STALE ACCEPTANCE" not in outcome.message


@pytest.mark.parametrize(
    "blocked",
    [
        {"criterion": "implementation_done"},
        {"criterion": "implementation_done", "paired": True},
    ],
    indirect=True,
)
@pytest.mark.timeout(180)
def test_stale_selected_accepted_review_uses_shared_approval_diagnostic(blocked, capsys):
    from booley.ticket_board.review_lifecycle import (
        approve_review_command,
        review_command,
        run_review_command,
    )
    from tests.ticket_board.test_ticket_baseline import _git

    root, tio, worktree = blocked
    state = DevelopmentState.load(tio.logs_dir / "demo" / ".runtime" / "booley_state.json")
    for criterion in state.criteria:
        state.set_criterion(criterion, True)
    state.save()
    assert asyncio.run(request_review_command(root, "demo", reason="inspect")).ready
    assert asyncio.run(request_review_command(root, "demo", action="finalize")).ready
    assert read_entry(tio.logs_dir / "demo")["disposition"] == "accepted"

    readme = worktree / "README.md"
    readme.write_text(readme.read_text(encoding="utf-8") + "\n<!-- stale -->\n")
    _git(worktree, "add", "README.md")
    _git(worktree, "commit", "-m", "Change selected accepted source")

    review = asyncio.run(review_command(root, "demo"))
    assert not review.ready
    assert "Ticket heads changed after acceptance" in review.message
    with pytest.raises(ReviewEntryError, match="Ticket heads changed after acceptance"):
        run_review_command(root, "demo", ["echo", "must-not-run"])
    with pytest.raises(ReviewEntryError, match="Ticket heads changed after acceptance") as caught:
        approve_review_command(root, "demo", no_merge=True)
    assert "restore the exact frozen heads" in str(caught.value)
    assert "booley board reset demo" in str(caught.value)
    assert tio.find_ticket("demo")["status"] == "review"
    assert not capsys.readouterr().err
    briefing = prep.review_briefing_command(root, "demo", open_diffs=False)
    assert briefing.status == "ready", briefing.message
    assert "STALE ACCEPTANCE" in briefing.briefing
    assert "Choose: **restore exact accepted heads** / **reset**" in briefing.briefing
    assert "Choose: **approve**" not in briefing.briefing

    (tio.logs_dir / "demo" / "acceptance" / "review-package.json").unlink()
    (tio.logs_dir / "demo" / ".runtime" / "triage-prep" / "manifest.json").unlink()
    missing = prep.review_briefing_command(root, "demo", open_diffs=False)
    assert missing.status == "stale"
    assert "STALE ACCEPTANCE" in missing.message
    assert "frozen accepted review package is unavailable" in missing.message


@pytest.mark.parametrize(
    "blocked",
    [{"merge": True, "cleanup": True, "criterion": "implementation_done"}],
    indirect=True,
)
def test_accepted_handoff_honors_independent_merge_override(blocked, monkeypatch):
    from booley.ticket_board.review_lifecycle import approve_review_command

    root, tio, _package = _automatic_accepted_handoff(blocked, monkeypatch)

    assert approve_review_command(root, "demo", no_merge=True)
    _assert_closed_done(tio, "demo")


@pytest.mark.parametrize(
    "blocked", [{"merge": True, "criterion": "implementation_done"}], indirect=True
)
@pytest.mark.parametrize(
    "options",
    [
        {},
        {"force": True},
        {"request": True, "repair": True, "reason": "recover review"},
    ],
)
def test_automatic_accepted_handoff_review_reuses_bound_package(blocked, monkeypatch, options):
    from booley.ticket_board.review_lifecycle import review_command

    root, tio, prepared = _automatic_accepted_handoff(blocked, monkeypatch)
    manifest = tio.logs_dir / "demo" / ".runtime" / "triage-prep" / "manifest.json"
    briefing = prepared.package_path
    before = (manifest.read_bytes(), briefing.read_bytes())

    outcome = asyncio.run(review_command(root, "demo", **options))

    assert outcome.status == "fresh", outcome.message
    assert outcome.package_path == prepared.package_path
    assert outcome.message == (
        "Ticket 'demo' is already accepted; run booley board approve demo to complete it."
    )
    assert read_entry(tio.logs_dir / "demo") is None
    assert (manifest.read_bytes(), briefing.read_bytes()) == before


@pytest.mark.parametrize(
    "blocked", [{"merge": True, "criterion": "implementation_done"}], indirect=True
)
@pytest.mark.parametrize("facade", ["prepare_review", "prepare_review_command"])
def test_prepare_review_force_preserves_accepted_handoff_package(blocked, monkeypatch, facade):
    from booley.ticket_board import review_lifecycle

    root, tio, prepared = _automatic_accepted_handoff(blocked, monkeypatch)
    manifest = tio.logs_dir / "demo" / ".runtime" / "triage-prep" / "manifest.json"
    before = (manifest.read_bytes(), prepared.package_path.read_bytes())

    outcome = asyncio.run(getattr(review_lifecycle, facade)(root, "demo", force=True))

    assert outcome.status == "fresh", outcome.message
    assert outcome.package_path == prepared.package_path
    assert (manifest.read_bytes(), prepared.package_path.read_bytes()) == before


@pytest.mark.parametrize(
    "blocked", [{"merge": True, "criterion": "implementation_done"}], indirect=True
)
def test_accepted_handoff_guides_review_when_bound_package_changed(blocked, monkeypatch):
    from booley.ticket_board.review_lifecycle import approve_review_command, review_command

    root, tio, _prepared = _automatic_accepted_handoff(blocked, monkeypatch)
    manifest = tio.logs_dir / "demo" / ".runtime" / "triage-prep" / "manifest.json"
    manifest.write_text("{}\n", encoding="utf-8")

    outcome = asyncio.run(review_command(root, "demo"))

    assert outcome.status == "accepted"
    assert not outcome.ready
    assert "booley board approve demo" in outcome.message
    assert approve_review_command(root, "demo", no_merge=True)
    _assert_closed_done(tio, "demo")


@pytest.mark.parametrize(
    "blocked", [{"merge": True, "criterion": "implementation_done"}], indirect=True
)
def test_legacy_accepted_handoff_without_package_returns_guidance(blocked, monkeypatch):
    from booley.ticket_board.review_lifecycle import review_command

    root, tio, prepared = _automatic_accepted_handoff(blocked, monkeypatch)
    (tio.logs_dir / "demo" / "acceptance" / "review-package.json").unlink()
    (tio.logs_dir / "demo" / ".runtime" / "triage-prep" / "manifest.json").unlink()
    prepared.package_path.unlink()

    outcome = asyncio.run(review_command(root, "demo"))

    assert outcome.status == "accepted"
    assert not outcome.ready
    assert outcome.package_path is None
    assert "booley board approve demo" in outcome.message


def test_validate_action_names_public_approve_command(tmp_path, monkeypatch):
    from booley.ticket_board import review_lifecycle

    tio = SimpleNamespace(
        logs_dir=tmp_path,
        find_ticket=lambda _slug: {"status": "review"},
    )
    monkeypatch.setattr(review_lifecycle, "read_entry", lambda _path: {"selected": True})
    monkeypatch.setattr(
        review_lifecycle,
        "read_acceptance",
        lambda _path: SimpleNamespace(kind="accepted"),
    )

    with pytest.raises(ReviewEntryError, match="booley board approve demo") as caught:
        review_lifecycle._validate_action(tio, "demo", "request", repair=True)
    assert "accepted review/complete workflow" not in str(caught.value)


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
    assert "RTL bugs review" in briefing.briefing
    assert "review_rtl_bugs_done" not in briefing.briefing


@pytest.mark.parametrize("blocked", [{"live_freshness": True}], indirect=True)
def test_requested_review_and_board_show_project_live_stale_criteria_read_only(blocked, capsys):
    from tests.ticket_board.test_ticket_baseline import _git

    root, tio, worktree = blocked
    tb = worktree / "tb" / "tb.sv"
    tb.write_text("module tb; // comment-only change\nendmodule\n", encoding="utf-8")
    _git(worktree, "add", "tb/tb.sv")
    _git(worktree, "commit", "-m", "Edit testbench comment")
    state_path = tio.logs_dir / "demo" / ".runtime" / "booley_state.json"
    before = state_path.read_bytes()

    outcome = asyncio.run(request_review_command(root, "demo", reason="Inspect freshness"))

    assert outcome.ready, outcome.message
    package = json.loads(outcome.package_path.read_text())
    rows = {row["criterion"]: row for row in package["criteria"]}
    for key in ("sim_pass_sim", "coverage_sim"):
        assert rows[key]["outcome"] == "met"
        assert rows[key]["freshness"] == "stale"
        assert rows[key]["changed_categories"] == ["tb"]
        assert rows[key]["status"] == "STALE (tb)"
    assert rows["_report_submitted"]["freshness"] == "stale"
    assert state_path.read_bytes() == before

    args = SimpleNamespace(slug="demo", no_open_diffs=True)
    assert _cmd_board_show(args, root) == 0
    rendered = capsys.readouterr().out
    assert rendered.count("STALE (tb)") >= 2
    assert "**Recommendation:** hold" in rendered
    assert "Stale mandatory verification evidence" in rendered
    assert state_path.read_bytes() == before


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


def test_request_review_rejects_corrupt_criteria_satisfaction_record(blocked):
    root, tio, _ = blocked
    acceptance_dir = tio.logs_dir / "demo" / "acceptance"
    acceptance_dir.mkdir(parents=True)
    (acceptance_dir / "accepted.json").write_text("{", encoding="utf-8")

    outcome = asyncio.run(request_review_command(root, "demo", reason="inspect"))

    assert not outcome.ready
    assert "Criteria Satisfaction Record is corrupt" in outcome.message


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
    # Strand the Ticket in review without a review entry.
    record = read_state_record(tio.tickets_dir, "demo")
    assert record is not None
    write_state_record(tio.tickets_dir, "demo", record.with_state(TicketState.REVIEW))
    assert not asyncio.run(request_review_command(root, "demo", reason="repair")).ready
    outcome = asyncio.run(request_review_command(root, "demo", reason="repair", repair=True))
    assert outcome.ready, outcome.message
    assert prep.review_briefing_command(root, "demo", open_diffs=False).status == "ready"


def _strand_provisional_review(tio, execution_id: str = "first") -> None:
    """Simulate an ADR 0066 provisional handoff: review, no acceptance, a marker."""
    record = read_state_record(tio.tickets_dir, "demo")
    assert record is not None
    write_state_record(tio.tickets_dir, "demo", record.with_state(TicketState.REVIEW))
    marker = tio.logs_dir / "demo" / "review" / "provisional-handoff.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"schema": 1, "execution_id": execution_id}))


def test_provisional_review_publishes_unaccepted_inspection_without_repair(blocked):
    """ADR 0066: board review opens the inspection a provisional handoff awaits."""
    from booley.ticket_board.review_lifecycle import approve_review_command, review_command

    root, tio, _ = blocked
    _strand_provisional_review(tio)

    outcome = asyncio.run(review_command(root, "demo"))

    assert outcome.ready, outcome.message
    row = read_entry(tio.logs_dir / "demo")
    assert row["disposition"] == "unaccepted"
    assert row["source_status"] == "review"
    assert row["capture_sha"]
    assert tio.find_ticket("demo")["status"] == "review"
    assert not (tio.logs_dir / "demo" / "acceptance" / "accepted.json").exists()
    # Without candidates to promote, strict acceptance still refuses; stays in review.
    with pytest.raises(ValueError, match="strict acceptance would still fail"):
        approve_review_command(root, "demo")
    assert tio.find_ticket("demo")["status"] == "review"


def test_waiver_candidate_record_change_makes_the_package_stale(blocked):
    """ADR 0066: the candidate record is a review input, so edits must be noticed."""
    from booley.ticket_board import waiver_candidates as store

    root, tio, _ = blocked
    assert asyncio.run(request_review_command(root, "demo", reason="inspect")).ready
    assert prep.review_briefing_command(root, "demo", open_diffs=False).status == "ready"

    store.record_rejections(
        tio.tickets_dir,
        "demo",
        [
            store.Rejection(
                "acme:x:y:1#sim", "cp1:abc", "sha256:" + "a" * 64, "2026-09-30T00:00:00Z", "A"
            )
        ],
    )

    assert prep.review_briefing_command(root, "demo", open_diffs=False).status != "ready"


def test_review_without_provisional_marker_still_needs_repair(blocked):
    from booley.ticket_board.review_lifecycle import review_command

    root, tio, _ = blocked
    record = read_state_record(tio.tickets_dir, "demo")
    write_state_record(tio.tickets_dir, "demo", record.with_state(TicketState.REVIEW))

    assert not asyncio.run(review_command(root, "demo")).ready
    assert read_entry(tio.logs_dir / "demo") is None


def test_unaccepted_completion_and_finalization_reject_unmet_gates(blocked):
    from booley.ticket_board.operations import _completion_acceptance_valid

    root, tio, _ = blocked
    assert asyncio.run(request_review_command(root, "demo", reason="inspect")).ready
    assert _completion_acceptance_valid(tio, "demo") is None
    outcome = asyncio.run(request_review_command(root, "demo", action="finalize"))
    assert not outcome.ready
    assert "mandatory criterion" in outcome.message
    assert read_entry(tio.logs_dir / "demo")["disposition"] == "unaccepted"


def test_approve_rejects_unmet_selection_without_agent(blocked, monkeypatch):
    from booley.ticket_board.review_lifecycle import approve_review_command

    root, tio, _ = blocked
    assert asyncio.run(request_review_command(root, "demo", reason="inspect")).ready

    async def unexpected_agent(*_args, **_kwargs):
        pytest.fail("approval launched an agent")

    monkeypatch.setattr(prep, "_invoke_agent", unexpected_agent)
    with pytest.raises(ValueError, match="mandatory criterion"):
        approve_review_command(root, "demo")
    assert tio.find_ticket("demo")["status"] == "review"
    assert not (tio.logs_dir / "demo" / "acceptance" / "accepted.json").exists()


def test_review_reuses_current_requested_package(blocked):
    from booley.ticket_board.review_lifecycle import review_command

    root, _tio, _ = blocked
    first = asyncio.run(request_review_command(root, "demo", reason="inspect"))
    second = asyncio.run(review_command(root, "demo"))
    assert first.ready and second.ready
    assert second.status == "fresh"
    assert second.package_path == first.package_path


def test_board_review_prepares_blocked_without_transition(blocked, monkeypatch):
    from booley.harness.blocked_prep import BlockedPrepOutcome
    from booley.ticket_board.review_lifecycle import review_command

    root, tio, _ = blocked

    async def dossier(_root, _slug, *, force=False):
        assert not force
        return BlockedPrepOutcome("ready", "dossier prepared")

    monkeypatch.setattr("booley.harness.blocked_prep.prepare_blocked_dossier", dossier)
    outcome = asyncio.run(review_command(root, "demo"))
    assert outcome.ready, outcome.message
    assert tio.find_ticket("demo")["status"] == "blocked"
    assert read_entry(tio.logs_dir / "demo") is None


def test_board_approve_rejects_changed_selected_head(blocked):
    from booley.ticket_board.review_lifecycle import approve_review_command
    from tests.ticket_board.test_ticket_baseline import _git

    root, tio, worktree = blocked
    assert asyncio.run(request_review_command(root, "demo", reason="inspect")).ready
    (worktree / "README.md").write_text("changed after review\n")
    _git(worktree, "add", "README.md")
    _git(
        worktree,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-m",
        "change reviewed head",
    )
    with pytest.raises(ValueError, match="selected review inputs changed"):
        approve_review_command(root, "demo")
    assert tio.find_ticket("demo")["status"] == "review"
    assert not (tio.logs_dir / "demo" / "acceptance" / "accepted.json").exists()


def test_concurrent_mutator_is_fenced_during_generation(blocked, monkeypatch):
    from booley.ticket_board import review_lifecycle as requests

    root, tio, _ = blocked
    original = prep._prepare_resolved_review

    async def generate(*args, **kwargs):
        # Simulate another process without relying on scheduling or sleeps.
        operation = requests.read_json(requests.operation_path(tio.logs_dir / "demo"))
        operation["pid"] = 999999
        requests._write(requests.operation_path(tio.logs_dir / "demo"), operation)
        monkeypatch.setattr("booley.ticket_board.review_records.is_pid_alive", lambda pid: True)
        with pytest.raises(ReviewEntryError, match="active review"):
            tio.move_ticket_file("demo", TicketState.QUEUED)
        operation["pid"] = __import__("os").getpid()
        requests._write(requests.operation_path(tio.logs_dir / "demo"), operation)
        return await original(*args, **kwargs)

    monkeypatch.setattr(prep, "_prepare_resolved_review", generate)
    outcome = asyncio.run(request_review_command(root, "demo", reason="inspect"))
    assert outcome.ready, outcome.message


@pytest.mark.parametrize("boundary", ["entry", "board"])
def test_publication_recovers_without_duplicate_transition(blocked, monkeypatch, boundary):
    from booley.ticket_board import review_lifecycle as requests

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
    monkeypatch.setattr(prep, "load_backend_config", lambda root: None)
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
@pytest.mark.timeout(180)
def test_scoped_endpoint_records_real_evidence_then_requires_run_report(
    blocked, monkeypatch, interrupt
):
    import sys

    from booley.ticket_board.review_execution import run_review_command

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
    from tests.ticket_board.test_ticket_baseline import _git

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
    assert "board review" in refused.message
    refreshed = asyncio.run(request_review_command(root, "demo", action="refresh"))
    assert refreshed.ready, refreshed.message
    assert refreshed.package_path != first.package_path
    assert first.package_path.exists()
    assert prep.review_briefing_command(root, "demo", open_diffs=False).status == "ready"


def test_interrupted_publication_can_be_resumed_by_new_process(blocked, monkeypatch):
    from booley.ticket_board import review_lifecycle as requests

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
    from booley.ticket_board import review_lifecycle as requests
    from tests.ticket_board.test_ticket_baseline import _git

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

    from booley.runtime import execution_lease as execution_context
    from booley.runtime.execution_lease import ExecutionLeaseError
    from booley.ticket_board import review_execution as interactive
    from booley.ticket_board import review_lifecycle as requests

    root, tio, worktree = blocked
    assert asyncio.run(request_review_command(root, "demo", reason="inspect")).ready
    entry = read_entry(tio.logs_dir / "demo")
    with tio._ticket_lock("demo", review_operation=True):
        board = tio.find_ticket("demo")
        worktree, env, lease = interactive._environment(
            tio,
            "demo",
            lease_id="fixture",
            ticket_generation=entry["ticket_generation"],
            review_ticket=tio.tickets_dir / board["file"],
        )
        operation = lease.operation_record(owner_pid=os.getpid())
        requests._write(requests.operation_path(tio.logs_dir / "demo"), operation)
    for key, value in env.items():
        if key.startswith("BOOLEY_") or key == "TICKETS_DIR":
            monkeypatch.setenv(key, value)
    execution_context.validate_recording(worktree)
    with pytest.raises(ExecutionLeaseError, match="work directory"):
        execution_context.validate_recording(root)
    monkeypatch.setenv("BOOLEY_STATE_FILE", str(root / "foreign-state.json"))
    with pytest.raises(ExecutionLeaseError, match="state path"):
        execution_context.validate_recording(worktree)


def test_review_exec_rejects_preexisting_basis_drift(blocked):
    from booley.ticket_board import review_execution as interactive

    root, tio, _worktree = blocked
    assert asyncio.run(request_review_command(root, "demo", reason="inspect")).ready
    board = tio.find_ticket("demo")

    with pytest.raises(ReviewEntryError, match="Ticket generation"):
        interactive._environment(
            tio,
            "demo",
            lease_id="fixture",
            ticket_generation="stale-generation",
            review_ticket=tio.tickets_dir / board["file"],
        )


def _rewrite_record(tio, drift: str) -> None:
    """Change only runtime fields, or the review state, of the demo state record."""
    record = read_state_record(tio.tickets_dir, "demo")
    assert record is not None
    changed = (
        record.with_runtime({"step": "review-note"})
        if drift == "runtime"
        else record.with_state(TicketState.BLOCKED)
    )
    write_state_record(tio.tickets_dir, "demo", changed)


@pytest.mark.parametrize(
    ("drift", "message"),
    [
        ("basis", "runtime Ticket"),
        ("status", "Ticket Board review Ticket state"),
        ("runtime", None),
        ("acceptance", "accepted Criteria Satisfaction Record"),
        ("job", "matching detached job"),
    ],
)
def test_review_exec_lease_rejects_lifecycle_drift(blocked, monkeypatch, drift, message):
    import os

    from booley.runtime import execution_lease as execution_context
    from booley.runtime.execution_lease import ExecutionLeaseError
    from booley.ticket_board import review_execution as interactive
    from booley.ticket_board import review_lifecycle as requests

    root, tio, worktree = blocked
    assert asyncio.run(request_review_command(root, "demo", reason="inspect")).ready
    entry = read_entry(tio.logs_dir / "demo")
    with tio._ticket_lock("demo", review_operation=True):
        board = tio.find_ticket("demo")
        _, env, lease = interactive._environment(
            tio,
            "demo",
            lease_id="fixture",
            ticket_generation=entry["ticket_generation"],
            review_ticket=tio.tickets_dir / board["file"],
        )
        requests._write(
            requests.operation_path(tio.logs_dir / "demo"),
            lease.operation_record(owner_pid=os.getpid()),
        )
    for key, value in env.items():
        if key.startswith("BOOLEY_") or key == "TICKETS_DIR":
            monkeypatch.setenv(key, value)
    if drift == "basis":
        runtime_ticket = lease.required_files[1].path
        runtime_ticket.write_text(runtime_ticket.read_text() + "drift\n")
    elif drift in {"runtime", "status"}:
        _rewrite_record(tio, drift)
    elif drift == "acceptance":
        accepted = lease.absent_paths[0].path
        accepted.parent.mkdir(parents=True, exist_ok=True)
        accepted.write_text("{}\n")
    else:
        from booley.runtime.pid import DEAD, ProcessIdentity, ProcessObservation

        operation = lease.operation_record(owner_pid=os.getpid())
        operation["pid"] = 999999
        operation["owner_identity"] = ProcessIdentity(
            pid=999999, identity_scope="synthetic", start_token=1
        ).to_payload()
        requests._write(
            requests.operation_path(tio.logs_dir / "demo"),
            operation,
        )
        monkeypatch.setattr(
            execution_context,
            "observe_process",
            lambda _identity: ProcessObservation(DEAD),
        )
        monkeypatch.setattr(
            execution_context,
            "active_jobs",
            lambda _root: [SimpleNamespace(lease_id="another-lease")],
        )
    if message is None:
        # A runtime-only record write leaves the review state, so the lease holds.
        execution_context.validate_recording(worktree)
        return
    with pytest.raises(ExecutionLeaseError, match=message):
        execution_context.validate_recording(worktree)


def test_live_job_blocks_request_without_mutation(blocked, monkeypatch):
    from booley.ticket_board import review_lifecycle as requests

    root, tio, _ = blocked
    monkeypatch.setattr(requests, "active_ticket_jobs", lambda _: [object()])
    outcome = asyncio.run(request_review_command(root, "demo", reason="inspect"))
    assert not outcome.ready
    assert "Jobs are active" in outcome.message
    assert tio.find_ticket("demo")["status"] == "blocked"


def test_malformed_job_record_blocks_request_without_mutation(blocked):
    root, tio, _worktree = blocked
    jobs = tio.logs_dir / "demo" / ".runtime" / "jobs"
    jobs.mkdir(parents=True, exist_ok=True)
    jobs.joinpath("broken.json").write_text("{not json", encoding="utf-8")

    outcome = asyncio.run(request_review_command(root, "demo", reason="inspect"))

    assert not outcome.ready
    assert "repair or removal" in outcome.message
    assert tio.find_ticket("demo")["status"] == "blocked"


@pytest.mark.parametrize("token", [None, "another-lease"])
def test_review_mutation_fails_closed_for_legacy_or_unrelated_jobs(blocked, monkeypatch, token):
    from booley.ticket_board import review_lifecycle as requests
    from booley.ticket_board import review_records, ticket_jobs

    root, tio, _worktree = blocked
    assert asyncio.run(request_review_command(root, "demo", reason="inspect")).ready
    operation = {"pid": 999999, "phase": "interactive"}
    if token is not None:
        operation["token"] = token
    requests._write(requests.operation_path(tio.logs_dir / "demo"), operation)
    monkeypatch.setattr(review_records, "is_pid_alive", lambda _pid: False)
    monkeypatch.setattr(
        ticket_jobs,
        "active_ticket_jobs",
        lambda _log_dir: [SimpleNamespace(lease_id=None)],
    )

    outcome = asyncio.run(request_review_command(root, "demo", action="refresh"))

    assert not outcome.ready
    assert (
        "operation identity is invalid" in outcome.message
        or "Jobs are still active" in outcome.message
    )


def test_review_exec_cleanup_preserves_lease_for_any_active_ticket_job(blocked, monkeypatch):
    import sys

    from booley.ticket_board import review_execution as interactive
    from booley.ticket_board import review_lifecycle as requests

    root, tio, _worktree = blocked
    assert asyncio.run(request_review_command(root, "demo", reason="inspect")).ready
    monkeypatch.setattr(requests, "active_ticket_jobs", lambda _log_dir: [])
    monkeypatch.setattr(
        interactive,
        "active_ticket_jobs",
        lambda _log_dir: [SimpleNamespace(lease_id=None)],
    )
    assert interactive.run_review_command(root, "demo", [sys.executable, "-c", "pass"]) == 0
    assert requests.operation_path(tio.logs_dir / "demo").is_file()


def test_missing_diff_artifact_invalidates_inspection(blocked):
    from tests.ticket_board.test_ticket_baseline import _git

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
    from booley.ticket_board import review_lifecycle as requests

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
    _repair_accepted_fixture(root, tio, outcome, interrupt, monkeypatch)
    from booley.ticket_board.operations import _completion_acceptance_valid

    assert _completion_acceptance_valid(tio, "demo") is not None
    assert read_entry(log_dir)["disposition"] == "accepted"
    from booley.ticket_board.operations import op_complete

    assert op_complete(tio, "demo")
    _assert_closed_done(tio, "demo")


def _approval_with_rejection(tio, ctx, _decisions, *, merge, staging=None):
    """Exercise the public approval lifecycle with an approval-owned input change."""
    from booley.ticket_board import waiver_candidates as store

    store.record_rejections(
        staging / "tickets" if staging is not None else tio.tickets_dir,
        ctx.slug,
        [
            store.Rejection(
                "acme:x:y:1#sim", "cp1:abc", "sha256:" + "a" * 64, "2026-09-30T00:00:00Z", "A"
            )
        ],
    )
    return prep._source_fingerprint(ctx)


def _interrupt_public_approval(root, tio, monkeypatch, interrupt):
    from booley.ticket_board import operations
    from booley.ticket_board import review_lifecycle as lifecycle

    if not interrupt:
        return None
    if interrupt == "capture":
        capture = lifecycle._record_approval_capture

        def interrupted(*_args, **_kwargs):
            raise OSError("interrupted before approval capture")

        monkeypatch.setattr(lifecycle, "_record_approval_capture", interrupted)
        with pytest.raises(OSError, match="before approval capture"):
            lifecycle.approve_review_command(root, "demo")
        monkeypatch.setattr(lifecycle, "_record_approval_capture", capture)
        return None
    if interrupt == "completion":
        complete = operations.op_complete
        monkeypatch.setattr(operations, "op_complete", lambda *_args, **_kwargs: False)
        assert not lifecycle.approve_review_command(root, "demo")
        monkeypatch.setattr(operations, "op_complete", complete)
    else:
        freeze = lifecycle.freeze_acceptance

        def interrupted(*args, **kwargs):
            snapshot = freeze(*args, **kwargs)
            raise OSError(f"interrupted after acceptance {snapshot.digest}")

        monkeypatch.setattr(lifecycle, "freeze_acceptance", interrupted)
        with pytest.raises(OSError, match="interrupted after acceptance"):
            lifecycle.approve_review_command(root, "demo")
        monkeypatch.setattr(lifecycle, "freeze_acceptance", freeze)
    return (tio.logs_dir / "demo" / "acceptance" / "accepted.json").read_bytes()


@pytest.mark.parametrize("blocked", [{"criterion": "implementation_done"}], indirect=True)
@pytest.mark.parametrize("interrupt", [False, True, "completion", "capture"])
@pytest.mark.parametrize("waiver_writes", [False, True])
@pytest.mark.timeout(180)
def test_board_approve_freezes_selected_package_without_agent(
    blocked, monkeypatch, interrupt, waiver_writes
):
    import sys

    from booley.ticket_board import review_lifecycle as lifecycle
    from booley.ticket_board.review_execution import run_review_command
    from booley.ticket_board.review_lifecycle import approve_review_command, review_command

    root, tio, worktree = blocked
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).resolve().parents[2] / "src"))
    assert asyncio.run(request_review_command(root, "demo", reason="finish verification")).ready
    endpoint = worktree / ".booley_project" / "mcp_tools" / "verify_fixture.py"
    assert run_review_command(root, "demo", [sys.executable, str(endpoint)]) == 0
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
    selected = asyncio.run(review_command(root, "demo"))
    assert selected.ready, selected.message

    async def unexpected_agent(*_args, **_kwargs):
        pytest.fail("approval launched an agent")

    monkeypatch.setattr(prep, "_invoke_agent", unexpected_agent)
    if waiver_writes:
        monkeypatch.setattr(lifecycle, "_apply_waiver_decisions", _approval_with_rejection)
    frozen = _interrupt_public_approval(root, tio, monkeypatch, interrupt)
    assert approve_review_command(root, "demo")
    if frozen is not None:
        assert (tio.logs_dir / "demo" / "acceptance" / "accepted.json").read_bytes() == frozen
    _assert_closed_done(tio, "demo")
    assert (tio.logs_dir / "demo" / "acceptance" / "accepted.json").exists()
    # Approving a Ticket that already closed done is a no-op that leaves its
    # Ticket History record untouched (ADR 0065).
    record = read_closed_ticket(tio.tickets_dir, "demo").path.read_bytes()
    assert approve_review_command(root, "demo")
    assert read_closed_ticket(tio.tickets_dir, "demo").path.read_bytes() == record


def test_waiver_approval_capture_still_rejects_unrelated_input_changes(blocked):
    from booley.ticket_board import review_lifecycle as lifecycle

    root, tio, _worktree = blocked
    assert asyncio.run(request_review_command(root, "demo", reason="inspect")).ready
    ctx = lifecycle._selected_context(tio, "demo")
    lifecycle._record_approval_capture(ctx, prep._source_fingerprint(ctx))
    (ctx.log_dir / "REPORT.md").write_text("Changed outside approval.\n")

    with pytest.raises(ReviewEntryError, match="selected review inputs changed"):
        lifecycle.approve_review_command(root, "demo")

    assert not (ctx.log_dir / "acceptance" / "accepted.json").exists()
    assert tio.find_ticket("demo")["status"] == "review"


@pytest.mark.parametrize("damage", ["missing", "downgraded"])
def test_finalize_requires_every_basis_mandatory_criterion(blocked, damage):
    from booley.ticket_board.acceptance_ledger import read_acceptance

    root, tio, _ = blocked
    state_path = tio.logs_dir / "demo" / ".runtime" / "booley_state.json"
    state = json.loads(state_path.read_text())
    state["criteria"] = {
        "implementation_done": {"met": True, "mandatory": True},
        "_report_submitted": {"met": True, "mandatory": True},
    }
    if damage == "downgraded":
        state["criteria"]["review_rtl_bugs_done"] = {"met": True, "mandatory": False}
    state_path.write_text(json.dumps(state))
    request = asyncio.run(request_review_command(root, "demo", reason="inspect"))
    assert request.ready, request.message
    result = asyncio.run(request_review_command(root, "demo", action="finalize"))
    assert not result.ready
    assert "review_rtl_bugs_done" in result.message
    assert read_acceptance(tio.logs_dir / "demo").kind == "unavailable"
    assert read_entry(tio.logs_dir / "demo")["disposition"] == "unaccepted"


@pytest.mark.parametrize(
    "field", ["state", "heads", "ticket_generation", "ticket_identity", "execution_id"]
)
def test_review_entry_rejects_missing_required_fields(blocked, field):
    from booley.ticket_board.review_records import (
        criteria_projection,
        digest,
        entry_path,
    )

    root, tio, _ = blocked
    assert asyncio.run(request_review_command(root, "demo", reason="inspect")).ready
    log_dir = tio.logs_dir / "demo"
    row = dict(read_entry(log_dir))
    del row[field]
    entry_path(log_dir).write_text(json.dumps({"entry": row, "sha256": digest(row)}))
    with pytest.raises(ReviewEntryError, match="invalid review entry"):
        criteria_projection(log_dir)


@pytest.mark.parametrize("bad", ["true", 1, None, {}])
def test_review_entry_rejects_invalid_criterion_flags(blocked, bad):
    from booley.ticket_board.review_records import digest, entry_path

    root, tio, _ = blocked
    assert asyncio.run(request_review_command(root, "demo", reason="inspect")).ready
    log_dir = tio.logs_dir / "demo"
    row = read_entry(log_dir)
    row["state"]["criteria"]["review_rtl_bugs_done"]["mandatory"] = bad
    entry_path(log_dir).write_text(json.dumps({"entry": row, "sha256": digest(row)}))
    with pytest.raises(ReviewEntryError, match="mandatory"):
        read_entry(log_dir)


def _repair_accepted_fixture(root, tio, outcome, interrupt, monkeypatch):
    from booley.ticket_board import review_lifecycle as requests
    from booley.ticket_board.review_records import package_dir

    log_dir = tio.logs_dir / "demo"
    frozen = (log_dir / "acceptance" / "accepted.json").read_bytes()
    for prepare in (prep.prepare_review_command, prep.prepare_review):
        generation = package_dir(log_dir, read_entry(log_dir))
        (generation / "briefing.json").unlink()
        assert prep.review_briefing_command(root, "demo", open_diffs=False).status != "ready"
        bind = requests.bind_review_package

        def interrupted(*args, bind=bind, **kwargs):
            bind(*args, **kwargs)
            raise OSError("interrupted after package binding")

        if interrupt:
            monkeypatch.setattr(requests, "bind_review_package", interrupted)
            failed = asyncio.run(prepare(root, "demo", force=True))
            assert not failed.ready
            monkeypatch.setattr(requests, "bind_review_package", bind)
        repaired = asyncio.run(prepare(root, "demo", force=True))
        assert repaired.ready, repaired.message
        assert repaired.package_path != outcome.package_path
        assert prep.review_briefing_command(root, "demo", open_diffs=False).status == "ready"
        assert (log_dir / "acceptance" / "accepted.json").read_bytes() == frozen
        outcome = repaired


@pytest.mark.parametrize(
    ("keys", "value"),
    [
        (("disposition",), "unknown"),
        (("source_status",), "done"),
        (("generation",), "../../invalid"),
        (("capture_sha",), "invalid"),
        (("heads",), {"project": "abc"}),
        (("ticket_identity", "schema"), 2),
        (("ticket_identity", "generation"), "wrong-generation"),
        (("state", "criteria", "review_rtl_bugs_done", "availability"), "unknown"),
        (("state", "criteria", "review_rtl_bugs_done", "transition_evidence"), ["invalid"]),
    ],
)
def test_invalid_review_metadata_is_rejected_before_projection(blocked, keys, value):
    from booley.ticket_board.review_records import (
        criteria_projection,
        digest,
        entry_path,
    )

    root, tio, _ = blocked
    assert asyncio.run(request_review_command(root, "demo", reason="inspect")).ready
    log_dir = tio.logs_dir / "demo"
    row = read_entry(log_dir)
    node = row
    for key in keys[:-1]:
        node = node[key]
    node[keys[-1]] = value
    entry_path(log_dir).write_text(json.dumps({"entry": row, "sha256": digest(row)}))
    with pytest.raises(ReviewEntryError):
        criteria_projection(log_dir)


def test_invalid_json_and_checksum_fail_before_reading_criteria(tmp_path):
    from booley.ticket_board.review_records import (
        criteria_projection,
        entry_path,
    )

    path = entry_path(tmp_path)
    path.parent.mkdir()
    for contents in ("not json", '{"entry": {}, "sha256": "wrong"}'):
        path.write_text(contents)
        with pytest.raises(ReviewEntryError):
            criteria_projection(tmp_path)


@pytest.mark.parametrize("damage", ["schema", "approval"])
def test_unaccepted_package_rejects_invalid_schema_or_approval(blocked, damage):
    from booley.review.artifact import ReviewArtifactError, ReviewPackage

    root, _, _ = blocked
    result = asyncio.run(request_review_command(root, "demo", reason="inspect"))
    assert result.ready
    package = json.loads(result.package_path.read_text())
    if damage == "schema":
        package["inspection"]["schema"] = 99
    else:
        package["assessment"]["recommendation"] = "approve"
    with pytest.raises(ReviewArtifactError):
        ReviewPackage.parse(package)


def test_interrupted_publication_blocks_completion(blocked, capsys):
    from booley.ticket_board.operations import _completion_acceptance_valid
    from booley.ticket_board.review_records import operation_path

    _, tio, _ = blocked
    path = operation_path(tio.logs_dir / "demo")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"phase": "publishing", "pid": 99999999}))
    assert _completion_acceptance_valid(tio, "demo") is None
    assert "publication was interrupted" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_review_lifecycle_facade_delegates_public_operations(
    tmp_path: Path, monkeypatch
) -> None:
    from booley.ticket_board import review_execution, review_lifecycle

    outcome = SimpleNamespace(status="ready")
    jobs = [SimpleNamespace(run_id="job-1")]

    async def prepare(project_root: Path, slug: str, *, force: bool = False):
        assert (project_root, slug, force) == (tmp_path, "demo", True)
        return outcome

    async def wait(_log_dir: Path):
        return jobs

    monkeypatch.setattr(review_lifecycle, "active_ticket_jobs", lambda _log_dir: jobs)
    monkeypatch.setattr(review_lifecycle, "wait_for_ticket_jobs", wait)
    monkeypatch.setattr(review_lifecycle.prep, "prepare_review", prepare)
    monkeypatch.setattr(review_lifecycle.prep, "prepare_review_command", prepare)
    monkeypatch.setattr(
        review_lifecycle.prep,
        "verify_review_handoff",
        lambda project_root, slug: (project_root, slug),
    )
    monkeypatch.setattr(
        review_lifecycle.prep,
        "review_briefing_command",
        lambda project_root, slug, *, open_diffs: (project_root, slug, open_diffs),
    )
    monkeypatch.setattr(
        review_execution,
        "run_review_command",
        lambda project_root, slug, command: len(command),
    )

    assert review_lifecycle.active_review_jobs(tmp_path) == jobs
    assert await review_lifecycle.wait_for_review_jobs(tmp_path) == jobs
    assert await review_lifecycle.prepare_review(tmp_path, "demo", force=True) is outcome
    assert await review_lifecycle.prepare_review_command(tmp_path, "demo", force=True) is outcome
    assert review_lifecycle.verify_review_handoff(tmp_path, "demo") == (tmp_path, "demo")
    assert review_lifecycle.review_briefing_command(tmp_path, "demo", open_diffs=False) == (
        tmp_path,
        "demo",
        False,
    )
    assert review_lifecycle.run_review_command(tmp_path, "demo", ["echo", "ok"]) == 2


def test_accepted_regeneration_rejects_changed_inputs(tmp_path, monkeypatch):
    from booley.ticket_board import review_lifecycle
    from booley.ticket_board.acceptance_diagnostics import (
        StaleAcceptanceError,
        format_stale_acceptance,
    )

    participant = SimpleNamespace(role="outer", ticket_ref="refs/heads/booley-ticket/demo")
    basis = SimpleNamespace(
        participant=lambda _role: participant,
        participants=(participant,),
    )
    tio = SimpleNamespace(logs_dir=tmp_path, load_basis=lambda _slug: basis)
    ctx = SimpleNamespace(
        inspection={"heads": {"outer": "b" * 40}, "state": {"generation": 2}},
        worktree=tmp_path / "worktree",
        project_repository=None,
    )
    prior = {"heads": {"outer": "a" * 40}, "state": {"generation": 1}}
    monkeypatch.setattr(
        review_lifecycle,
        "read_acceptance",
        lambda _path: SimpleNamespace(
            kind="accepted",
            snapshot=SimpleNamespace(participant_heads={"outer": "a" * 40}),
        ),
    )

    with pytest.raises(StaleAcceptanceError) as caught:
        review_lifecycle._validate_regeneration(tio, "demo", "regenerate", prior, ctx)
    diagnostic = format_stale_acceptance("demo", caught.value.drift)
    assert "Ticket heads changed after acceptance" in diagnostic
    assert "booley board reset demo" in diagnostic


def test_review_lifecycle_reports_missing_and_stale_selection(tmp_path, monkeypatch):
    from booley.ticket_board import review_lifecycle

    tio = TicketIO(tmp_path / "tickets", project_root=tmp_path)
    with pytest.raises(ReviewEntryError, match="no selected review"):
        review_lifecycle._selected_context(tio, "demo")

    ctx = SimpleNamespace(log_dir=tmp_path, inspection={"capture_sha": "capture"})
    monkeypatch.setattr(review_lifecycle.prep, "_read_manifest", lambda _ctx: {})
    monkeypatch.setattr(review_lifecycle.prep, "_review_prompt", lambda _ctx: ("", "prompt"))
    monkeypatch.setattr(review_lifecycle.prep, "_fresh_outcome", lambda *_args: None)
    monkeypatch.setattr(review_lifecycle, "_check_capture", lambda *_args: None)
    with pytest.raises(ReviewEntryError, match="missing or stale"):
        review_lifecycle._require_selected_package(tio, ctx)


def test_selected_accepted_heads_must_match_criteria_satisfaction_record(tmp_path, monkeypatch):
    from booley.ticket_board import review_lifecycle
    from booley.ticket_board.acceptance_diagnostics import (
        ParticipantHeadLocation,
        StaleAcceptanceError,
        compare_accepted_heads,
    )

    row = {
        "heads": {"outer": "b" * 40},
        "generation": "selection",
    }
    tio = SimpleNamespace(
        logs_dir=tmp_path,
        _project_root=tmp_path,
        _load_basis_unlocked=lambda _slug: object(),
    )
    drift = compare_accepted_heads(
        {"outer": "a" * 40},
        {"outer": "c" * 40},
        [ParticipantHeadLocation("outer", "refs/heads/demo", tmp_path)],
    )
    assert drift is not None
    monkeypatch.setattr(review_lifecycle, "read_entry", lambda _path: row)
    monkeypatch.setattr(
        review_lifecycle.prep,
        "_resolve_context",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(StaleAcceptanceError(drift)),
    )
    monkeypatch.setattr(
        review_lifecycle,
        "read_acceptance",
        lambda _path: SimpleNamespace(
            kind="accepted",
            snapshot=SimpleNamespace(participant_heads={"outer": "a" * 40}),
        ),
    )

    with pytest.raises(ReviewEntryError, match="package is corrupt"):
        review_lifecycle._selected_context(tio, "demo")


def test_approval_recovery_translates_stale_acceptance(monkeypatch, tmp_path):
    from booley.ticket_board import review_lifecycle
    from booley.ticket_board.acceptance_diagnostics import (
        ParticipantHeadLocation,
        StaleAcceptanceError,
        compare_accepted_heads,
    )

    drift = compare_accepted_heads(
        {"outer": "a" * 40},
        {"outer": "b" * 40},
        [ParticipantHeadLocation("outer", "refs/heads/demo", tmp_path)],
    )
    assert drift is not None
    monkeypatch.setattr(
        review_lifecycle,
        "_recover",
        lambda *_args: (_ for _ in ()).throw(StaleAcceptanceError(drift)),
    )

    with pytest.raises(ReviewEntryError, match="Ticket heads changed after acceptance"):
        review_lifecycle._recover_for_approval(SimpleNamespace(), "demo")


def test_approve_done_ticket_handles_invalid_and_merge_paths(monkeypatch):
    from booley.ticket_board import operations, review_lifecycle

    tio = SimpleNamespace(find_ticket=lambda _slug: {"on_success": {"merge": True}})
    monkeypatch.setattr(operations, "_completion_acceptance_valid", lambda *_args: None)
    assert not review_lifecycle._approve_done_ticket(tio, "demo", no_merge=False, no_cleanup=False)

    monkeypatch.setattr(operations, "_completion_acceptance_valid", lambda *_args: object())
    monkeypatch.setattr(operations, "op_complete", lambda *_args, **_kwargs: True)
    assert review_lifecycle._approve_done_ticket(tio, "demo", no_merge=False, no_cleanup=False)


def test_approve_review_ticket_rejects_missing_completion_and_target_plan(monkeypatch):
    from booley.ticket_board import operations, review_lifecycle

    tio = SimpleNamespace(load_basis=lambda _slug: SimpleNamespace(target_plan=object()))
    monkeypatch.setattr(operations, "_completion_context", lambda *_args: None)
    assert not review_lifecycle._approve_review_ticket(
        tio, "demo", no_merge=False, no_cleanup=False
    )

    monkeypatch.setattr(
        operations,
        "_completion_context",
        lambda *_args: (None, SimpleNamespace(merge=False)),
    )
    with pytest.raises(ReviewEntryError, match="Target Plan acceptance requires merge"):
        review_lifecycle._approve_review_ticket(tio, "demo", no_merge=False, no_cleanup=False)


def test_approve_review_ticket_rejects_corrupt_acceptance(monkeypatch, tmp_path):
    from booley.ticket_board import operations, review_lifecycle

    class Lock:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    tio = SimpleNamespace(
        logs_dir=tmp_path,
        _ticket_lock=lambda *_args, **_kwargs: Lock(),
        load_basis=lambda _slug: SimpleNamespace(target_plan=None),
        find_ticket=lambda _slug: {"status": "review"},
    )
    ctx = SimpleNamespace(log_dir=tmp_path, inspection={"reason": "inspect", "capture_sha": "sha"})
    monkeypatch.setattr(
        operations,
        "_completion_context",
        lambda *_args: (None, SimpleNamespace(merge=True)),
    )
    monkeypatch.setattr(review_lifecycle, "_recover", lambda *_args: None)
    monkeypatch.setattr(review_lifecycle, "assert_idle", lambda *_args: None)
    monkeypatch.setattr(review_lifecycle, "_quiescent", lambda *_args: None)
    monkeypatch.setattr(review_lifecycle, "_selected_context", lambda *_args: ctx)
    monkeypatch.setattr(review_lifecycle, "_require_selected_package", lambda *_args: None)
    monkeypatch.setattr(
        review_lifecycle,
        "read_acceptance",
        lambda _path: SimpleNamespace(kind="corrupt", reason="bad"),
    )

    with pytest.raises(ReviewEntryError, match="Criteria Satisfaction Record is corrupt"):
        review_lifecycle._approve_review_ticket(tio, "demo", no_merge=False, no_cleanup=False)


def test_approve_review_command_rejects_missing_and_non_review_ticket(blocked):
    from booley.ticket_board.review_lifecycle import ReviewEntryError, approve_review_command

    root, _tio, _ = blocked
    with pytest.raises(ReviewEntryError, match="approve requires a review ticket"):
        approve_review_command(root, "demo")

    with pytest.raises(ReviewEntryError, match="not found"):
        approve_review_command(root, "missing")


@pytest.mark.asyncio
async def test_review_command_reports_blocked_dossier_and_state_errors(blocked, monkeypatch):
    from booley.ticket_board import review_lifecycle

    root, tio, _ = blocked
    original_assert_idle = review_lifecycle.assert_idle
    monkeypatch.setattr(
        review_lifecycle,
        "assert_idle",
        lambda *_args: (_ for _ in ()).throw(ReviewEntryError("busy")),
    )
    busy = await review_lifecycle._review_blocked_ticket(root, "demo", tio, force=False)
    assert not busy.ready and busy.message == "busy"
    monkeypatch.setattr(review_lifecycle, "assert_idle", original_assert_idle)

    async def not_ready_dossier(*_args, **_kwargs):
        return _not_ready_dossier()

    monkeypatch.setattr("booley.harness.blocked_prep.prepare_blocked_dossier", not_ready_dossier)
    not_ready = await review_lifecycle._review_blocked_ticket(root, "demo", tio, force=False)
    assert not not_ready.ready and "dossier" in not_ready.message

    requested = await review_lifecycle.review_command(root, "demo", request=True, reason="inspect")
    assert requested.ready
    invalid_request = await review_lifecycle.review_command(root, "demo", request=True)
    assert not invalid_request.ready and "--request requires" in invalid_request.message
    invalid_repair = await review_lifecycle.review_command(root, "demo", repair=True)
    assert not invalid_repair.ready and "--repair requires" in invalid_repair.message


def _not_ready_dossier():
    from booley.harness.blocked_prep import BlockedPrepOutcome

    return BlockedPrepOutcome("failed", "dossier unavailable")


def _blocked_diagnosis():
    return {
        "classification": "ticket-code",
        "board_reason": "failed",
        "blocked_stage": "developer",
        "blockers": [{"name": "sim", "reason": "failed", "evidence": "state"}],
        "passing_non_blocking": [],
        "developer_questions": [],
        "recommended_action": "retry with feedback",
        "findings": [],
    }


@pytest.mark.asyncio
async def test_review_blocked_ticket_keeps_live_log_appends_fresh(tmp_path, monkeypatch):
    from contextlib import nullcontext

    from booley.core.models import AgentResult
    from booley.harness import blocked_prep
    from booley.ticket_board import review_lifecycle
    from booley.ticket_board import review_preparation as prep

    ticket = tmp_path / "blocked" / "demo.md"
    ticket.parent.mkdir()
    ticket.write_text("ticket\n", encoding="utf-8")
    log_dir = tmp_path / "logs" / "demo"
    run_log = log_dir / "human-logs" / "run.log"
    run_log.parent.mkdir(parents=True)
    run_log.write_text("blocked\n", encoding="utf-8")
    ctx = blocked_prep.BlockedContext(
        tmp_path,
        "demo",
        ticket,
        log_dir,
        log_dir / ".runtime" / "triage-prep",
        None,
    )

    async def invoke(_ctx, _evidence):
        return AgentResult(structured=_blocked_diagnosis())

    async def prepare_review(*_args, **_kwargs):
        with run_log.open("a", encoding="utf-8") as stream:
            stream.write("review activity\n")
        return prep.ReviewPrepOutcome("ready", "review ready", package_path=tmp_path / "review")

    tio = SimpleNamespace(
        logs_dir=log_dir.parent,
        _ticket_lock=lambda *_args, **_kwargs: nullcontext(),
    )
    monkeypatch.setattr(blocked_prep, "_resolve_context", lambda *_args: ctx)
    monkeypatch.setattr(blocked_prep, "_invoke", invoke)
    monkeypatch.setattr(review_lifecycle, "_quiescent", lambda *_args: None)
    monkeypatch.setattr(prep, "prepare_review_command", prepare_review)

    outcome = await review_lifecycle._review_blocked_ticket(tmp_path, "demo", tio, force=False)

    assert outcome.ready
    assert blocked_prep.render_blocked_dossier(tmp_path, "demo").ready


@pytest.mark.asyncio
async def test_review_command_reports_missing_corrupt_and_wrong_state(tmp_path, monkeypatch):
    from booley.ticket_board import review_lifecycle

    class FakeTio:
        board = None

        def __init__(self, *_args, **_kwargs):
            self.logs_dir = tmp_path / "logs"
            self._project_root = tmp_path

        def _ticket_lock(self, *_args, **_kwargs):
            return nullcontext()

        def find_ticket(self, _slug):
            return self.board

        def inspect_ticket(self, slug):
            return self.find_ticket(slug)

        def read_progress(self, _slug):
            return dict(RUNTIME_DEFAULTS)

    monkeypatch.setattr(review_lifecycle, "TicketIO", FakeTio)
    missing = await review_lifecycle.review_command(tmp_path, "demo")
    assert not missing.ready and "not found" in missing.message

    FakeTio.board = {"file": "board/demo.md", "status": "queue"}
    wrong_state = await review_lifecycle.review_command(tmp_path, "demo")
    assert not wrong_state.ready and "blocked or review" in wrong_state.message

    FakeTio.board = {"file": "board/demo.md", "status": "review"}
    monkeypatch.setattr(
        review_lifecycle,
        "_current_review_package",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        review_lifecycle,
        "read_acceptance",
        lambda _path: SimpleNamespace(kind="corrupt", reason="bad"),
    )
    corrupt = await review_lifecycle.review_command(tmp_path, "demo")
    assert not corrupt.ready and "Criteria Satisfaction Record is corrupt" in corrupt.message


def test_run_review_command_rejects_invalid_entry_states(tmp_path, monkeypatch):
    from booley.ticket_board import review_execution

    class FakeTio:
        board: ClassVar[dict[str, str] | None] = {
            "file": "board/demo.md",
            "status": "review",
        }

        def __init__(self, *_args, **_kwargs):
            self.logs_dir = tmp_path / "logs"
            self.tickets_dir = tmp_path / "tickets"

        def find_ticket(self, _slug):
            return self.board

        def _ticket_lock(self, *_args, **_kwargs):
            class Lock:
                def __enter__(self):
                    return self

                def __exit__(self, *_args):
                    return False

            return Lock()

    monkeypatch.setattr(review_execution, "TicketIO", FakeTio)
    FakeTio.board = None
    with pytest.raises(ReviewEntryError, match="requires a review ticket"):
        review_execution.run_review_command(tmp_path, "demo", ["echo", "ok"])

    FakeTio.board = {"file": "board/demo.md", "status": "review"}
    with pytest.raises(ReviewEntryError, match="after --"):
        review_execution.run_review_command(tmp_path, "demo", [])

    monkeypatch.setattr(review_execution, "read_entry", lambda _path: {"disposition": "accepted"})
    monkeypatch.setattr(review_execution, "assert_idle", lambda *_args: None)
    monkeypatch.setattr(review_execution, "_quiescent", lambda *_args: None)
    with pytest.raises(ReviewEntryError, match="explicitly unaccepted"):
        review_execution.run_review_command(tmp_path, "demo", ["echo", "ok"])

    calls = iter([FakeTio.board, None])
    monkeypatch.setattr(FakeTio, "find_ticket", lambda _self, _slug: next(calls))
    monkeypatch.setattr(
        review_execution, "read_entry", lambda _path: {"disposition": "unaccepted"}
    )
    with pytest.raises(ReviewEntryError, match="requires a review ticket"):
        review_execution.run_review_command(tmp_path, "demo", ["echo", "ok"])


def test_prepare_blocked_dossier_returns_stable_failure(tmp_path, monkeypatch):
    from booley.harness import blocked_prep

    monkeypatch.setattr(
        blocked_prep,
        "_resolve_context",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("synthetic failure")),
    )
    outcome = asyncio.run(blocked_prep.prepare_blocked_dossier(tmp_path, "demo"))
    assert not outcome.ready
    assert "synthetic failure" in outcome.message


def test_prepare_blocked_dossier_reuses_fresh_dossier(tmp_path, monkeypatch):
    from booley.harness import blocked_prep

    fresh = tmp_path / "dossier.json"
    ctx = SimpleNamespace()
    monkeypatch.setattr(blocked_prep, "_resolve_context", lambda *_args: ctx)
    monkeypatch.setattr(
        blocked_prep,
        "_fresh",
        lambda _ctx: blocked_prep.FreshResult(package_path=fresh),
    )
    outcome = asyncio.run(blocked_prep.prepare_blocked_dossier(tmp_path, "demo"))
    assert outcome.status == "fresh"
    assert outcome.package_path == fresh


def test_mechanical_move_to_review_is_rejected(tmp_path, capsys):
    tickets = tmp_path / ".booley_project" / "tickets"
    place_ticket(
        tickets,
        "demo",
        "active",
        "---\nsummary: Synthetic review transition\ntype: feature\nbranch: demo\n---\n",
    )
    tio = TicketIO(tickets, project_root=tmp_path)
    assert not tio._move_prerequisite("demo", TicketState.REVIEW, None)
    assert "requires board review --request" in capsys.readouterr().err

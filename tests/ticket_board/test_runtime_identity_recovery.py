"""Recovery preserves current proof and attributes corrupt historical inputs."""

import json
from pathlib import Path

import pytest

from booley.criteria.state import DevelopmentState
from booley.ticket_board.acceptance_ledger import (
    freeze_acceptance,
    read_acceptance,
    record_amendment_observations,
    record_changes,
)
from booley.ticket_board.amendment import apply_amendment, preview_amendment
from booley.ticket_board.board_layout import read_state_record, write_state_record
from booley.ticket_board.lifecycle import TicketState
from booley.ticket_board.operations import op_activate, op_block, op_promote_waiting
from booley.ticket_board.paths import runtime_file
from booley.ticket_board.runtime_identity import retire_foreign_pointers
from booley.ticket_board.ticket_baseline import TicketBaselineError

from .test_amendment_publication import _optional_request
from .test_ticket_baseline import _blocked_ticket
from .test_v2_ticket_writes import _enqueue_waiting, _git_project, _write_prepared_refresh


@pytest.mark.parametrize("name", ["review/entry.json", ".runtime/triage-prep/manifest.json"])
@pytest.mark.parametrize("content", ["[]", "{"])
def test_corrupt_selected_pointer_is_an_attributed_domain_error(tmp_path, name, content):
    path = tmp_path / name
    path.parent.mkdir(parents=True)
    path.write_text(content)
    with pytest.raises(TicketBaselineError, match="selected review pointer"):
        retire_foreign_pointers(
            tmp_path, tmp_path / "history", "operation", {"generation": "a" * 32}
        )
    assert path.read_text() == content


def test_proof_retry_recovers_abandoned_atomic_write_temporary(tmp_path, monkeypatch):
    from booley.ticket_board import acceptance_ledger

    state = DevelopmentState()
    state.slug = "proof"
    state.init_criteria({"sim_pass": True})
    changes = state.set_criterion("sim_pass", True)

    def interrupted(path, content):
        (path.parent / ".record.json.killed.tmp").write_bytes(content)
        raise OSError("killed after staging")

    with monkeypatch.context() as patch:
        patch.setattr(acceptance_ledger, "_write_once", interrupted)
        with pytest.raises(OSError, match="killed after staging"):
            record_amendment_observations(
                tmp_path,
                state,
                changes,
                operation_id="operation",
                ticket_identity={"generation": "a" * 32},
            )
    record_amendment_observations(
        tmp_path,
        state,
        changes,
        operation_id="operation",
        ticket_identity={"generation": "a" * 32},
    )
    assert len(list((tmp_path / "acceptance/evidence").glob("*/record.json"))) == 1
    assert not list((tmp_path / "acceptance/evidence").glob("*/.record.json.*.tmp"))
    assert len(state.acceptance_transactions) == 1


def _publish_queued_refresh(project, board, slug):
    machine, journal = _write_prepared_refresh(project, board, slug, "a" * 32)
    ticket = board.tickets_dir / board.find_ticket(slug)["file"]
    from booley.ticket_board.basis_refresh import prepare_waiting_basis_refresh

    prepare_waiting_basis_refresh(project, ticket, slug)
    board._publish_spec_fields(
        Path(ticket), board._prepare_spec_fields(Path(ticket), {"machine": machine})
    )
    record = read_state_record(board.tickets_dir, slug)
    write_state_record(board.tickets_dir, slug, record.with_state(TicketState.QUEUED))
    return machine, journal


@pytest.mark.parametrize("snapshot", ["missing", "stale"])
def test_queued_refresh_preserves_current_selection_and_acceptance(
    tmp_path, monkeypatch, snapshot
):
    project, board = _git_project(tmp_path, monkeypatch)
    ticket = _enqueue_waiting(project, board, "preserved")
    original = ticket.read_bytes()
    identity, _journal = _publish_queued_refresh(project, board, "preserved")
    log = board.logs_dir / "preserved"
    if snapshot == "stale":
        (log / "ticket.md").write_bytes(original)
    state = DevelopmentState.load(runtime_file(board.logs_dir, "preserved", "booley_state.json"))
    state.slug = "preserved"
    state.init_criteria({"review_rtl_bugs_clean": True})
    record_changes(
        log,
        state,
        state.set_criterion("review_rtl_bugs_clean", True),
        invocation_id="current",
        producer="review",
        execution_id="current",
        ticket_identity=identity,
        transaction_id="c" * 64,
    )
    state.acceptance_transactions = ["c" * 64]
    state.save()
    accepted = freeze_acceptance(
        log,
        state,
        execution_id="current",
        ticket_identity=identity,
        participant_heads={"outer": identity["baseline"]["outer"]["commit"]},
    )
    assert op_promote_waiting(board) == []
    repaired = DevelopmentState.load(state._file_path)
    assert repaired.acceptance_transactions == ["c" * 64]
    assert repaired.criteria["review_rtl_bugs_clean"].met
    assert read_acceptance(log).snapshot.digest == accepted.digest
    assert (
        board.load_basis("preserved", runtime_ticket_path=log / "ticket.md").ticket_identity()
        == identity
    )


def _apply_second_amendment(tio, snapshot, first):
    original = snapshot.read_bytes()
    old_identity = tio.load_basis("blocked-again").ticket_identity()
    assert op_activate(tio, "blocked-again")
    assert op_block(tio, "blocked-again", "Further amendment", "implementation")
    request = {
        "actor": "Human",
        "reason": "Extend scope",
        "feedback": "Continue",
        "scope_add": ["EXTRA.md"],
    }
    second = apply_amendment(
        tio,
        "blocked-again",
        request,
        preview_amendment(tio, "blocked-again", request)["digest"],
    )
    history = snapshot.parent / "amendments"
    first_row = json.loads((history / f"{first['operation_id']}.json").read_text())
    second_row = json.loads((history / f"{second['operation_id']}.json").read_text())
    assert first_row["new_ticket_identity"] == old_identity == second_row["old_ticket_identity"]
    assert (history / f"{second['operation_id']}.prior-ticket.md").read_bytes() == original


def _old_runtime_proof(tio, log):
    old = tio.load_basis("blocked-again").ticket_identity()
    state = DevelopmentState.load(runtime_file(tio.logs_dir, "blocked-again", "booley_state.json"))
    state.slug = "blocked-again"
    state.init_criteria({"review_rtl_bugs_clean": True})
    record_changes(
        log,
        state,
        state.set_criterion("review_rtl_bugs_clean", True),
        invocation_id="old",
        producer="review",
        execution_id="old",
        ticket_identity=old,
        transaction_id="c" * 64,
    )
    state.acceptance_transactions = ["c" * 64]
    state.save()
    freeze_acceptance(
        log,
        state,
        execution_id="old",
        ticket_identity=old,
        participant_heads={"outer": old["baseline"]["outer"]["commit"]},
    )
    old_pointer = (log / "acceptance/accepted.json").read_bytes()
    return state, old_pointer


def _legacy_amended_runtime(tmp_path, count):
    root, ticket, tio = _blocked_ticket(tmp_path, extra_file="EXTRA.md")
    tio.move_and_update("blocked-again", TicketState.BLOCKED, {"steps_completed": ["setup"]})
    log = tio.logs_dir / "blocked-again"
    snapshot = log / "ticket.md"
    snapshot.write_bytes(ticket.read_bytes())
    old_bytes = snapshot.read_bytes()
    state, old_pointer = _old_runtime_proof(tio, log)
    request = _optional_request()
    first = apply_amendment(
        tio, "blocked-again", request, preview_amendment(tio, "blocked-again", request)["digest"]
    )
    if count == 2:
        _apply_second_amendment(tio, snapshot, first)
    for history in (log / "amendments").glob("????????????????????????????????.json"):
        row = json.loads(history.read_text())
        row.pop("old_ticket_identity", None)
        row.pop("new_ticket_identity", None)
        history.write_text(json.dumps(row))
    # Old _rebuild_state preserved selections/met values but applied optionality.
    state.criteria["review_rtl_bugs_clean"].mandatory = False
    state.save()
    (log / "acceptance/accepted.json").write_bytes(old_pointer)
    snapshot.write_bytes(old_bytes)
    return root, tio, snapshot, state, first


@pytest.mark.asyncio
@pytest.mark.parametrize("snapshot_state", ["missing", "malformed", "stale", "matching"])
@pytest.mark.parametrize("amendments", [1, 2])
async def test_old_amendment_cohort_repairs_without_a_readable_recent_snapshot(
    tmp_path, monkeypatch, snapshot_state, amendments
):
    from booley.harness.setup.intake import run
    from booley.runtime.project_dir import reset_cache

    root, tio, snapshot, state, _first = _legacy_amended_runtime(tmp_path, amendments)
    monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(root / ".booley_project"))
    reset_cache()
    if snapshot_state == "missing":
        snapshot.unlink()
    elif snapshot_state == "malformed":
        snapshot.write_text("invalid snapshot")
    elif snapshot_state == "matching":
        snapshot.write_bytes(
            (tio.tickets_dir / tio.find_ticket("blocked-again")["file"]).read_bytes()
        )
    before = snapshot.stat().st_mtime_ns if snapshot.exists() else None
    ctx = await run("blocked-again", root)
    tio.load_basis(ctx.slug, runtime_ticket_path=snapshot)
    repaired = DevelopmentState.load(state._file_path)
    if snapshot_state == "matching":
        assert snapshot.stat().st_mtime_ns == before
    assert "c" * 64 not in repaired.acceptance_transactions
    assert not repaired.criteria["review_rtl_bugs_clean"].met
    assert repaired.criteria["review_rtl_bugs_clean"].mandatory is False
    assert read_acceptance(snapshot.parent).kind != "accepted"


@pytest.mark.asyncio
async def test_explicit_intake_recovers_a_queued_unfinished_refresh(tmp_path, monkeypatch):
    from booley.harness.setup.intake import run
    from booley.ticket_board.basis_refresh import load_basis_refresh

    project, board = _git_project(tmp_path, monkeypatch)
    _enqueue_waiting(project, board, "queued-recovery")
    _identity, _journal = _publish_queued_refresh(project, board, "queued-recovery")
    ctx = await run("queued-recovery", project)
    assert ctx.slug == "queued-recovery"
    assert load_basis_refresh(project, ctx.slug) is None


@pytest.mark.parametrize("content", ["[]", "{"])
def test_corrupt_amendment_history_is_an_attributed_domain_error(tmp_path, content):
    _root, tio, snapshot, _state, first = _legacy_amended_runtime(tmp_path, 1)
    path = snapshot.parent / "amendments" / f"{first['operation_id']}.json"
    path.write_text(content)
    before = snapshot.read_bytes()
    with pytest.raises(TicketBaselineError, match="amendment history"):
        op_activate(tio, "blocked-again")
    assert snapshot.read_bytes() == before


def test_preview_and_handoff_explain_demoted_unchanged_proof(tmp_path):
    _root, _ticket, tio = _blocked_ticket(tmp_path)
    log = tio.logs_dir / "blocked-again"
    _old_runtime_proof(tio, log)
    request = _optional_request()
    preview = preview_amendment(tio, "blocked-again", request)
    outcome = preview["current_results"]["review_rtl_bugs_clean"]
    assert outcome == {"result": "met", "after_result": "unmet", "evidence": "rerun"}
    applied = apply_amendment(tio, "blocked-again", request, preview["digest"])
    history = json.loads((log / "amendments" / f"{applied['operation_id']}.json").read_text())
    assert history["evidence_outcomes"]["review_rtl_bugs_clean"]["evidence"] == "rerun"
    assert "rerun stale Criteria: review_rtl_bugs_clean" in history["next_action"]


def test_freeze_ignores_unpublished_temporary_record_directories(tmp_path):
    state = DevelopmentState()
    state.slug = "accepted"
    state.init_criteria({"sim_pass": True})
    identity = {"generation": "a" * 32}
    record_changes(
        tmp_path,
        state,
        state.set_criterion("sim_pass", True),
        invocation_id="current",
        producer="sim",
        execution_id="current",
        ticket_identity=identity,
    )
    temporary = tmp_path / "acceptance/evidence/.tmp.acceptance.unpublished/record.json"
    temporary.parent.mkdir()
    temporary.write_text("{")
    snapshot = freeze_acceptance(
        tmp_path,
        state,
        execution_id="current",
        ticket_identity=identity,
        participant_heads={"outer": "b" * 40},
    )
    assert len(snapshot.evidence) == 1


@pytest.mark.parametrize("generation", [None, "a" * 32, "b" * 32])
def test_generation_only_selected_pointer_is_preserved_or_retired(tmp_path, generation):
    path = tmp_path / "review/entry.json"
    path.parent.mkdir()
    original = json.dumps({"ticket_generation": generation})
    path.write_text(original)
    if generation is None:
        with pytest.raises(TicketBaselineError, match="has no Ticket identity"):
            retire_foreign_pointers(tmp_path, tmp_path / "history", "op", {"generation": "a" * 32})
        assert path.read_text() == original
        return
    retire_foreign_pointers(tmp_path, tmp_path / "history", "op", {"generation": "a" * 32})
    if generation == "a" * 32:
        assert path.read_text() == original
    else:
        assert not path.exists()
        assert (tmp_path / "history/op.review-entry.json").read_text() == original


@pytest.mark.parametrize(
    "patch, expected",
    [
        ({"authored_sha256": "invalid"}, "authored identity"),
        ({"baseline": []}, "baseline identity"),
        ({"schema": 999}, "malformed Ticket identity"),
        ({}, "malformed Ticket identity"),
    ],
)
def test_foreign_observation_identity_is_validated_before_filtering(tmp_path, patch, expected):
    from booley.ticket_board.acceptance_ledger import (
        AcceptanceLedgerError,
        historical_ticket_identities,
    )

    state = DevelopmentState()
    state.slug = "foreign"
    state.init_criteria({"sim_pass": True})
    record_changes(
        tmp_path,
        state,
        state.set_criterion("sim_pass", True),
        invocation_id="old",
        producer="sim",
        execution_id="old",
        ticket_identity={"generation": "a" * 32},
    )
    path = next((tmp_path / "acceptance/evidence").glob("*/record.json"))
    row = json.loads(path.read_text())
    row["ticket_identity"].update(patch)
    path.write_text(json.dumps(row))
    before = path.read_bytes()
    current = {"generation": "b" * 32, "authored_sha256": "c" * 64}
    with pytest.raises(AcceptanceLedgerError, match=expected):
        historical_ticket_identities(tmp_path, state, current)
    assert path.read_bytes() == before

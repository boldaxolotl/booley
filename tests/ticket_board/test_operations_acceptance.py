"""Acceptance lifecycle behavior exercised through public board operations."""

from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.ticket_board import (
    acceptance_basis,
    operations,
    workspace_ops,
)
from booley.ticket_board.acceptance_basis import (
    AcceptanceBasis,
    BasisParticipant,
)


def _participant(role: str = "outer") -> BasisParticipant:
    return BasisParticipant(
        role,
        "a" * 40,
        f"refs/heads/booley-generation/0123456789abcdef/{role}",
        "refs/heads/main",
        "b" * 40,
    )


def _handoff_tio(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    run_log = tmp_path / "run.log"
    run_log.write_text("done\n", encoding="utf-8")
    entry = {"status": "running", "step": "summary", "on_success": {}}
    tio = SimpleNamespace(
        logs_dir=tmp_path,
        tickets_dir=tmp_path,
        _project_root=tmp_path,
        find_ticket=lambda _slug: entry,
    )
    monkeypatch.setattr(operations, "existing_human_log_file", lambda *_args: run_log)
    monkeypatch.setattr(operations, "_validate_transitions_for_handoff", lambda *_args: True)
    monkeypatch.setattr(operations, "is_event_enabled", lambda *_args: False)
    monkeypatch.setattr(
        operations,
        "_op_move_and_log",
        lambda *_args, **kwargs: kwargs["before_move"](),
    )
    return tio


def _review_tio(tmp_path: Path) -> SimpleNamespace:
    basis = AcceptanceBasis((_participant(),))
    entry = {
        "status": "review",
        "step": "summary",
        "file": str(tmp_path / "ticket.md"),
        "acceptance_basis": basis.as_dict(),
        "on_success": {},
    }
    return SimpleNamespace(
        logs_dir=tmp_path,
        tickets_dir=tmp_path,
        _project_root=tmp_path,
        find_ticket=lambda _slug: entry,
        load_basis=lambda _slug: basis,
    )


def test_reset_helpers_report_missing_basis_and_preflight_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    ticket = tmp_path / "ticket.md"
    ticket.write_text("ticket\n", encoding="utf-8")
    entry = {
        "acceptance_basis": {"schema": 1},
        "branch": "main",
        "file": str(ticket),
        "status": "blocked",
    }
    tio = SimpleNamespace(
        _project_root=tmp_path,
        tickets_dir=tmp_path,
        logs_dir=tmp_path,
        find_ticket=lambda _slug: entry,
        _ticket_lock=lambda _slug: nullcontext(),
        _load_basis_unlocked=lambda _slug: None,
    )
    monkeypatch.setattr(operations, "_reset_owner_available", lambda *_args: True)
    monkeypatch.setattr(operations, "_reset_jobs_inactive", lambda *_args: True)
    monkeypatch.setattr(operations, "_queue_destination_available", lambda *_args: True)
    monkeypatch.setattr(
        "booley.ticket_board.io.find_ticket_file", lambda *_args: (ticket, "blocked")
    )
    assert operations.op_reset(tio, "ticket") is False
    assert "authoritative Acceptance Basis is unavailable" in capsys.readouterr().err

    basis = AcceptanceBasis((_participant(),))
    tio._load_basis_unlocked = lambda _slug: basis
    monkeypatch.setattr(
        workspace_ops,
        "preflight_basis_reset",
        lambda *_args: (_ for _ in ()).throw(
            workspace_ops.AcceptanceBasisOperationError("cannot preflight")
        ),
    )
    assert operations.op_reset(tio, "ticket") is False
    assert "cannot preflight" in capsys.readouterr().err


def test_handoff_preparation_short_circuits_jobs_and_reuses_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tio = _handoff_tio(tmp_path, monkeypatch)
    monkeypatch.setattr(operations, "_handoff_jobs_clear", lambda *_args: False)
    assert operations.op_handoff(tio, "ticket") is False
    monkeypatch.setattr(operations, "_handoff_jobs_clear", lambda *_args: True)
    monkeypatch.setattr(operations, "_handoff_basis_heads", lambda *_args: {"outer": "a" * 40})
    monkeypatch.setattr(operations, "_bind_existing_handoff_snapshot", lambda *_args: True)
    assert operations.op_handoff(tio, "ticket") is True


def test_handoff_jobs_clear_reports_active_jobs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    tio = _handoff_tio(tmp_path, monkeypatch)
    job = SimpleNamespace(endpoint="sim", run_id="run-1")
    monkeypatch.setattr("booley.harness.job_fence.active_ticket_jobs", lambda _path: [job])
    assert operations.op_handoff(tio, "ticket") is False
    assert "sim (run-1)" in capsys.readouterr().err


def test_materialized_handoff_requires_ticket_and_successful_preparation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tio = _handoff_tio(tmp_path, monkeypatch)
    basis = AcceptanceBasis((_participant(),))
    monkeypatch.setattr(operations, "_load_handoff_basis", lambda *_args: basis)
    monkeypatch.setattr(
        acceptance_basis,
        "validate_current_basis_refs",
        lambda *_args: {"outer": "a" * 40},
    )
    monkeypatch.setattr(
        acceptance_basis,
        "materialize_ticket_commits",
        lambda _root, _basis, _destination, _heads: tmp_path,
    )
    monkeypatch.setattr(acceptance_basis, "assert_live_inputs_unchanged", lambda *_args: None)
    monkeypatch.setattr("booley.ticket_board.io.find_ticket_file", lambda *_args: (None, None))
    assert operations.op_handoff(tio, "ticket") is False
    assert "unavailable during Basis validation" in capsys.readouterr().err
    ticket = tmp_path / "ticket.md"
    ticket.write_text("ticket", encoding="utf-8")
    monkeypatch.setattr(
        "booley.ticket_board.io.find_ticket_file", lambda *_args: (ticket, "queue")
    )
    monkeypatch.setattr(
        "booley.runtime.project_dir.resolve_checkout_project_dir", lambda _root: tmp_path
    )
    monkeypatch.setattr(
        "booley.runtime.project_prepare.prepare_project",
        lambda *_args, **_kwargs: SimpleNamespace(ok=False, error="prepare failed"),
    )
    monkeypatch.setattr("booley.flows.execution.flow_enabled", lambda *_args: False)
    assert operations.op_handoff(tio, "ticket") is False
    assert "prepare failed" in capsys.readouterr().err


def test_completion_snapshot_rejects_basis_and_selector_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tio = _review_tio(tmp_path)
    snapshot = SimpleNamespace(
        acceptance_basis={"different": True}, participant_heads={"outer": "a" * 40}
    )
    monkeypatch.setattr(
        "booley.ticket_board.acceptance_ledger.read_acceptance",
        lambda *_args: SimpleNamespace(kind="accepted", snapshot=snapshot),
    )
    monkeypatch.setattr(
        "booley.ticket_board.acceptance_ledger.validate_review_package_binding",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        acceptance_basis,
        "load_basis_receipt",
        lambda *_args: {"current": True},
    )
    assert operations.op_complete(tio, "ticket", no_merge=True, no_cleanup=True) is False
    assert "different Board Acceptance Basis" in capsys.readouterr().err


def test_handoff_basis_heads_validates_materialized_composite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    basis = AcceptanceBasis((_participant(),))
    tio = _handoff_tio(tmp_path, monkeypatch)
    monkeypatch.setattr(operations, "_load_handoff_basis", lambda *_args: basis)
    monkeypatch.setattr(
        acceptance_basis,
        "validate_current_basis_refs",
        lambda *_args: {"outer": "a" * 40},
    )
    monkeypatch.setattr(
        acceptance_basis,
        "materialize_ticket_commits",
        lambda _root, _basis, destination, _heads: destination,
    )
    monkeypatch.setattr(acceptance_basis, "assert_live_inputs_unchanged", lambda *_args: None)
    monkeypatch.setattr(operations, "_prepare_materialized_basis_view", lambda *_args: [])
    monkeypatch.setattr(operations, "_bind_existing_handoff_snapshot", lambda *_args: True)
    assert operations.op_handoff(tio, "ticket") is True

    monkeypatch.setattr(
        operations,
        "_prepare_materialized_basis_view",
        lambda *_args: ["target changed"],
    )
    assert operations.op_handoff(tio, "ticket") is False
    assert "selectors changed" in capsys.readouterr().err


def test_completion_acceptance_reports_unreadable_corrupt_and_valid_snapshots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from booley.ticket_board.acceptance_ledger import AcceptanceLedgerError

    snapshot = SimpleNamespace()
    outcomes = iter(
        [
            SimpleNamespace(kind="accepted", snapshot=None),
            SimpleNamespace(kind="accepted", snapshot=snapshot),
            SimpleNamespace(kind="accepted", snapshot=snapshot),
        ]
    )
    monkeypatch.setattr(
        "booley.ticket_board.acceptance_ledger.read_acceptance",
        lambda *_args: next(outcomes),
    )
    validation = iter([AcceptanceLedgerError("broken binding"), None])

    def validate(*_args: object) -> None:
        result = next(validation)
        if result is not None:
            raise result

    monkeypatch.setattr(operations, "_validate_accepted_snapshot", validate)
    monkeypatch.setattr(operations, "_approve_transition", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(operations, "_finish_completed_ticket", lambda *_args, **_kwargs: None)
    tio = _review_tio(tmp_path)

    assert operations.op_complete(tio, "ticket", no_merge=True, no_cleanup=True) is False
    assert "unreadable" in capsys.readouterr().err
    assert operations.op_complete(tio, "ticket", no_merge=True, no_cleanup=True) is False
    assert "broken binding" in capsys.readouterr().err
    assert operations.op_complete(tio, "ticket", no_merge=True, no_cleanup=True) is True

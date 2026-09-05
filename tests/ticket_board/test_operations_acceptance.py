"""Focused boundary tests for Acceptance Basis helper modules."""

from __future__ import annotations

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
    AcceptanceBasisError,
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


def test_reset_helpers_report_missing_basis_and_preflight_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    entry = {"acceptance_basis": {"schema": 1}, "branch": "main"}
    assert operations._reset_ticket_branches(tmp_path, "ticket", entry, None) is False
    assert "authoritative Acceptance Basis is unavailable" in capsys.readouterr().err
    assert operations._preflight_reset_branches(tmp_path, "ticket", entry, None) is None
    assert "authoritative Acceptance Basis is unavailable" in capsys.readouterr().err
    monkeypatch.setattr(
        workspace_ops,
        "preflight_basis_reset",
        lambda *_args: (_ for _ in ()).throw(
            workspace_ops.AcceptanceBasisOperationError("cannot preflight")
        ),
    )
    assert (
        operations._preflight_reset_branches(
            tmp_path, "ticket", entry, AcceptanceBasis((_participant(),))
        )
        is None
    )
    assert "cannot preflight" in capsys.readouterr().err


def test_handoff_preparation_short_circuits_jobs_and_reuses_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tio = SimpleNamespace(logs_dir=tmp_path)
    monkeypatch.setattr(operations, "_handoff_jobs_clear", lambda *_args: False)
    assert operations._prepare_handoff_snapshot(tio, "ticket", None, None) is False
    monkeypatch.setattr(operations, "_handoff_jobs_clear", lambda *_args: True)
    monkeypatch.setattr(operations, "_handoff_basis_heads", lambda *_args: {"outer": "a" * 40})
    monkeypatch.setattr(operations, "_bind_existing_handoff_snapshot", lambda *_args: True)
    assert operations._prepare_handoff_snapshot(tio, "ticket", None, None) is True


def test_handoff_jobs_clear_reports_active_jobs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    job = SimpleNamespace(endpoint="sim", run_id="run-1")
    monkeypatch.setattr("booley.harness.job_fence.active_ticket_jobs", lambda _path: [job])
    assert operations._handoff_jobs_clear(tmp_path, "ticket") is False
    assert "sim (run-1)" in capsys.readouterr().err


def test_materialized_handoff_requires_ticket_and_successful_preparation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tio = SimpleNamespace(tickets_dir=tmp_path, _project_root=tmp_path)
    monkeypatch.setattr("booley.ticket_board.io.find_ticket_file", lambda *_args: (None, None))
    with pytest.raises(AcceptanceBasisError, match="unavailable during Basis validation"):
        operations._prepare_materialized_basis_view(
            tio, "ticket", tmp_path, AcceptanceBasis((_participant(),))
        )
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
    with pytest.raises(AcceptanceBasisError, match="prepare failed"):
        operations._prepare_materialized_basis_view(
            tio, "ticket", tmp_path, AcceptanceBasis((_participant(),))
        )


def test_completion_snapshot_rejects_basis_and_selector_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    basis = AcceptanceBasis((_participant(),))
    tio = SimpleNamespace(
        _project_root=tmp_path,
        load_basis=lambda _slug: basis,
    )
    snapshot = SimpleNamespace(
        acceptance_basis={"different": True}, participant_heads={"outer": "a" * 40}
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
    from booley.ticket_board.acceptance_ledger import AcceptanceLedgerError

    with pytest.raises(AcceptanceLedgerError, match="different Board Acceptance Basis"):
        operations._validate_accepted_snapshot(tio, "ticket", tmp_path, snapshot)


def test_handoff_basis_heads_validates_materialized_composite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    basis = AcceptanceBasis((_participant(),))
    tio = SimpleNamespace(_project_root=tmp_path)
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
    assert operations._handoff_basis_heads(tio, "ticket") == {"outer": "a" * 40}

    monkeypatch.setattr(
        operations,
        "_prepare_materialized_basis_view",
        lambda *_args: ["target changed"],
    )
    assert operations._handoff_basis_heads(tio, "ticket") is None
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
    tio = SimpleNamespace(logs_dir=tmp_path)

    assert operations._completion_acceptance_valid(tio, "ticket") is None
    assert "unreadable" in capsys.readouterr().err
    assert operations._completion_acceptance_valid(tio, "ticket") is None
    assert "broken binding" in capsys.readouterr().err
    assert operations._completion_acceptance_valid(tio, "ticket") is snapshot

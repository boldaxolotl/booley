from __future__ import annotations

import pytest

from booley.flows.execution_persistence import (
    AcceptanceRecordingError,
    NoAcceptanceRecorder,
    StandaloneFlowExecution,
)
from booley.ticket_board import acceptance_ledger
from booley.ticket_board.flow_execution import TicketAcceptanceRecorder


def test_transaction_recording_without_log_directory_is_a_noop(monkeypatch) -> None:
    monkeypatch.delenv("BOOLEY_LOGS_DIR", raising=False)
    recorder = TicketAcceptanceRecorder()
    assert (
        recorder.record_or_verify_transaction(
            None,
            [],
            acceptance_facts={},
            ticket_identity={},  # type: ignore[arg-type]
        )
        is None
    )


def test_transaction_recording_translates_ledger_errors(monkeypatch, tmp_path) -> None:
    recorder = TicketAcceptanceRecorder(log_dir=tmp_path)

    def fail(*_args, **_kwargs):
        raise acceptance_ledger.AcceptanceLedgerError("ledger failed")

    monkeypatch.setattr(acceptance_ledger, "record_or_verify_transaction", fail)
    with pytest.raises(AcceptanceRecordingError, match="ledger failed"):
        recorder.record_or_verify_transaction(
            None,
            [],
            acceptance_facts={},
            ticket_identity={},  # type: ignore[arg-type]
        )


def test_acceptance_identity_is_the_validated_ticket_identity(monkeypatch) -> None:
    monkeypatch.delenv("BOOLEY_TICKET_FILE", raising=False)
    identity = {"generation": "d" * 32}
    assert TicketAcceptanceRecorder(ticket_identity=identity).acceptance_identity() is identity
    assert TicketAcceptanceRecorder().acceptance_identity() == {}


def test_recorders_without_durable_state_have_no_acceptance_identity() -> None:
    assert NoAcceptanceRecorder().acceptance_identity() is None
    assert StandaloneFlowExecution().acceptance_identity() is None

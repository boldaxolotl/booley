"""Ticket composition behavior for deterministic Flow execution."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from booley.criteria.state import DevelopmentState
from booley.flows.execution_persistence import AcceptanceRecordingError
from booley.flows.request import FlowRequest
from booley.ticket_board.acceptance_basis import AcceptanceBasisError
from booley.ticket_board.flow_execution import (
    TicketAcceptanceRecorder,
    TicketBoardFlowExecution,
)
from booley.ticket_board.frontmatter import format_frontmatter


def test_recorder_rejects_malformed_acceptance_basis(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ticket = tmp_path / "ticket.md"
    ticket.write_text(
        format_frontmatter({"acceptance_basis": "not-a-record"}, "ticket"),
        encoding="utf-8",
    )
    monkeypatch.setenv("BOOLEY_TICKET_FILE", str(ticket))
    state = DevelopmentState()
    state.init_criteria({"lint_clean_demo": True}, strict=True)
    changes = state.set_criterion("lint_clean_demo", True)

    with pytest.raises(AcceptanceRecordingError, match="acceptance_basis"):
        TicketAcceptanceRecorder(log_dir=tmp_path / "logs").record_changes(
            state,
            changes,
            invocation_id="lint-1",
            producer="lint",
        )

    assert not (tmp_path / "logs" / "acceptance").exists()


def test_unexpected_adapter_value_error_is_not_disguised_as_blocked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = TicketBoardFlowExecution()
    monkeypatch.setattr(
        adapter,
        "_load_basis",
        lambda: (_ for _ in ()).throw(ValueError("programming defect")),
    )

    with pytest.raises(ValueError, match="programming defect"):
        adapter.validate_and_resolve(FlowRequest(target="demo", work_dir=tmp_path))


@pytest.mark.parametrize("ticket_path", [None, "missing-ticket.md"])
def test_adapter_blocks_when_ticket_snapshot_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ticket_path: str | None,
) -> None:
    if ticket_path is None:
        monkeypatch.delenv("BOOLEY_TICKET_FILE", raising=False)
    else:
        monkeypatch.setenv("BOOLEY_TICKET_FILE", str(tmp_path / ticket_path))

    outcome = TicketBoardFlowExecution().validate_and_resolve(
        FlowRequest(target="demo", work_dir=tmp_path)
    )

    assert outcome.report_text == "BLOCKED: ticket snapshot is unavailable"


def test_ticket_recorder_requires_an_evidence_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BOOLEY_TICKET_FILE", "ticket.md")
    monkeypatch.delenv("BOOLEY_LOGS_DIR", raising=False)

    with pytest.raises(AcceptanceRecordingError, match="no acceptance evidence directory"):
        TicketAcceptanceRecorder().record_changes(
            DevelopmentState(),
            [],
            invocation_id="lint-1",
            producer="lint",
        )


def test_ticket_runtime_configuration_requires_logs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BOOLEY_LOGS_DIR", raising=False)

    with pytest.raises(AcceptanceBasisError, match="no acceptance evidence directory"):
        TicketBoardFlowExecution._configure_runtime(FlowRequest(target="demo", work_dir=tmp_path))


def test_ticket_runtime_configuration_derives_runtime_and_report_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logs_dir = tmp_path / "logs"
    monkeypatch.setenv("BOOLEY_LOGS_DIR", str(logs_dir))
    # Track the variable even when the process started without it so the
    # adapter's direct environment write is reliably undone after this test.
    monkeypatch.setenv("BOOLEY_RUNTIME_DIR", "")
    request = FlowRequest(target="demo", work_dir=tmp_path)

    TicketBoardFlowExecution._configure_runtime(request)

    runtime_dir = logs_dir / ".runtime"
    assert request.report_dir == runtime_dir / "flow-reports"
    assert Path(os.environ["BOOLEY_RUNTIME_DIR"]) == runtime_dir

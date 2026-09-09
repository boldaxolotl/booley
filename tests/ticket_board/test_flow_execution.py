"""Ticket composition behavior for deterministic Flow execution."""

from __future__ import annotations

from pathlib import Path

import pytest

from booley.criteria.state import DevelopmentState
from booley.flows.execution_persistence import AcceptanceRecordingError
from booley.flows.request import FlowRequest
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

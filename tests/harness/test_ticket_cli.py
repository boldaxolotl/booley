"""Tests for ticket_cli facade — mock via set_ticket_ops."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from booley.harness.ticket_cli import (
    TicketCLIError,
    block,
    claim,
    classify,
    collect_evidence,
    complete,
    fail,
    handoff,
    next_step,
    parse_ticket,
    promote_waiting,
    resume,
    set_ticket_ops,
    timing,
    update_board,
    validate_logs,
    validate_ticket,
)


@pytest.fixture(autouse=True)
def _mock_ops():
    """Inject a MagicMock as the active TicketOps for every test."""
    mock = MagicMock()
    set_ticket_ops(mock)
    yield mock
    set_ticket_ops(None)


class TestTicketCLI:
    def test_classify_returns_json(self, _mock_ops, project_root: Path):
        expected = {"executable": [{"slug": "test"}], "blocked": [], "waiting": []}
        _mock_ops.classify.return_value = expected
        result = classify(project_root)
        assert result["executable"][0]["slug"] == "test"
        _mock_ops.classify.assert_called_once_with(project_root)

    def test_next_step_returns_name(self, _mock_ops, project_root: Path):
        _mock_ops.next_step.return_value = "implementation"
        result = next_step(project_root, "bugfix", "planning")
        assert result == "implementation"

    def test_next_step_done_returns_none(self, _mock_ops, project_root: Path):
        _mock_ops.next_step.return_value = None
        result = next_step(project_root, "bugfix", "review")
        assert result is None

    def test_next_step_with_skip(self, _mock_ops, project_root: Path):
        _mock_ops.next_step.return_value = "sim-debug-loop"
        result = next_step(project_root, "bugfix", "lint-check", skip="rtl-review-1,tb-review")
        assert result == "sim-debug-loop"
        _mock_ops.next_step.assert_called_once_with(
            project_root, "bugfix", "lint-check", "rtl-review-1,tb-review"
        )

    def test_validate_ticket_valid(self, _mock_ops, project_root: Path):
        _mock_ops.validate_ticket.return_value = {"errors": [], "valid": True}
        result = validate_ticket(project_root, "test.md", check_git=True)
        assert result["valid"] is True
        _mock_ops.validate_ticket.assert_called_once_with(project_root, "test.md", check_git=True)

    def test_validate_ticket_invalid_no_raise(self, _mock_ops, project_root: Path):
        """validate_ticket returns errors dict, does not raise."""
        _mock_ops.validate_ticket.return_value = {"errors": ["missing scope"], "valid": False}
        result = validate_ticket(project_root, "bad.md")
        assert result["valid"] is False

    def test_block_passes_args(self, _mock_ops, project_root: Path):
        block(project_root, "test-slug", reason="sim failed", step="sim-debug-loop")
        _mock_ops.block.assert_called_once_with(
            project_root, "test-slug", reason="sim failed", step="sim-debug-loop"
        )

    def test_fail_passes_args(self, _mock_ops, project_root: Path):
        fail(project_root, "test-slug", error="crash", step="implementation")
        _mock_ops.fail.assert_called_once_with(
            project_root, "test-slug", error="crash", step="implementation"
        )

    def test_cli_error_raised(self, _mock_ops, project_root: Path):
        _mock_ops.parse_ticket.side_effect = TicketCLIError("parse-ticket", 2, "not found")
        with pytest.raises(TicketCLIError) as exc_info:
            parse_ticket(project_root, "missing.md")
        assert exc_info.value.returncode == 2
        assert "not found" in exc_info.value.stderr

    def test_empty_classify_raises(self, _mock_ops, project_root: Path):
        _mock_ops.classify.side_effect = TicketCLIError("classify", -1, "timed out")
        with pytest.raises(TicketCLIError) as exc_info:
            classify(project_root)
        assert exc_info.value.returncode == -1
        assert "timed out" in exc_info.value.stderr


class TestTicketCLIClaim:
    def test_claim_delegates_to_ops(self, _mock_ops, project_root: Path):
        _mock_ops.claim.return_value = True
        assert claim(project_root, "fix-fsm") is True
        _mock_ops.claim.assert_called_once_with(project_root, "fix-fsm")

    def test_claim_returns_false_on_failure(self, _mock_ops, project_root: Path):
        _mock_ops.claim.return_value = False
        assert claim(project_root, "fix-fsm") is False


class TestTicketCLIResume:
    def test_resume_returns_json(self, _mock_ops, project_root: Path):
        expected = {
            "completed_steps": ["planning", "implementation"],
            "current_step": "sim-debug-loop",
        }
        _mock_ops.resume.return_value = expected
        result = resume(project_root, "fix-fsm")
        assert result["current_step"] == "sim-debug-loop"

    def test_resume_error(self, _mock_ops, project_root: Path):
        _mock_ops.resume.side_effect = TicketCLIError("resume", 1, "not found")
        with pytest.raises(TicketCLIError):
            resume(project_root, "missing-slug")


class TestTicketCLIHandoff:
    def test_handoff_calls_correctly(self, _mock_ops, project_root: Path):
        handoff(project_root, "fix-fsm")
        _mock_ops.handoff.assert_called_once_with(project_root, "fix-fsm")


class TestTicketCLIUpdateBoard:
    def test_update_with_set_fields(self, _mock_ops, project_root: Path):
        update_board(project_root, "fix-fsm", set_fields={"step": "planning"})
        _mock_ops.update_board.assert_called_once_with(
            project_root,
            "fix-fsm",
            set_fields={"step": "planning"},
            append_step="",
            reset_steps=False,
            reset_steps_from="",
            log=False,
        )

    def test_update_with_append_step(self, _mock_ops, project_root: Path):
        update_board(project_root, "fix-fsm", append_step="implementation")
        _mock_ops.update_board.assert_called_once_with(
            project_root,
            "fix-fsm",
            set_fields=None,
            append_step="implementation",
            reset_steps=False,
            reset_steps_from="",
            log=False,
        )

    def test_update_with_reset_steps(self, _mock_ops, project_root: Path):
        update_board(project_root, "fix-fsm", reset_steps=True)
        _mock_ops.update_board.assert_called_once_with(
            project_root,
            "fix-fsm",
            set_fields=None,
            append_step="",
            reset_steps=True,
            reset_steps_from="",
            log=False,
        )

    def test_update_with_reset_steps_from(self, _mock_ops, project_root: Path):
        update_board(project_root, "fix-fsm", reset_steps_from="planning")
        _mock_ops.update_board.assert_called_once_with(
            project_root,
            "fix-fsm",
            set_fields=None,
            append_step="",
            reset_steps=False,
            reset_steps_from="planning",
            log=False,
        )

    def test_update_with_log_flag(self, _mock_ops, project_root: Path):
        update_board(project_root, "fix-fsm", log=True)
        _mock_ops.update_board.assert_called_once_with(
            project_root,
            "fix-fsm",
            set_fields=None,
            append_step="",
            reset_steps=False,
            reset_steps_from="",
            log=True,
        )

    def test_update_minimal(self, _mock_ops, project_root: Path):
        update_board(project_root, "fix-fsm")
        _mock_ops.update_board.assert_called_once()


class TestTicketCLICollectEvidence:
    def test_collect_evidence_returns_json(self, _mock_ops, project_root: Path):
        expected = {"evidence": {"commits": 3, "sim_pass": True}}
        _mock_ops.collect_evidence.return_value = expected
        result = collect_evidence(project_root, "fix-fsm")
        assert result["evidence"]["sim_pass"] is True


class TestTicketCLIComplete:
    def test_complete_delegates(self, _mock_ops, project_root: Path):
        complete(project_root, "fix-fsm")
        _mock_ops.complete.assert_called_once_with(project_root, "fix-fsm")


class TestTicketCLIMisc:
    def test_promote_waiting(self, _mock_ops, project_root: Path):
        _mock_ops.promote_waiting.return_value = "fix-sha3"
        result = promote_waiting(project_root)
        assert "fix-sha3" in result

    def test_validate_logs_valid(self, _mock_ops, project_root: Path):
        _mock_ops.validate_logs.return_value = (True, "All OK")
        valid, report = validate_logs(project_root, "fix-fsm")
        assert valid is True
        assert "All OK" in report

    def test_validate_logs_invalid(self, _mock_ops, project_root: Path):
        _mock_ops.validate_logs.return_value = (False, "Missing sim-results.md")
        valid, _report = validate_logs(project_root, "fix-fsm")
        assert valid is False

    def test_timing_with_save(self, _mock_ops, project_root: Path):
        _mock_ops.timing.return_value = "| Stage | Time |"
        _result = timing(project_root, "fix-fsm", save=True)
        _mock_ops.timing.assert_called_once_with(project_root, "fix-fsm", save=True)

    def test_timing_without_save(self, _mock_ops, project_root: Path):
        _mock_ops.timing.return_value = "| Stage | Time |"
        _result = timing(project_root, "fix-fsm")
        _mock_ops.timing.assert_called_once_with(project_root, "fix-fsm", save=False)

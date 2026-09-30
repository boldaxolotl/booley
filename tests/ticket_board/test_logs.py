"""Tests for booley.ticket_board.logs — log management."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from booley.ticket_board.board_layout import (
    StateRecord,
    StateRecordError,
    read_state_record,
    runtime_default,
    state_record_path,
    write_state_record,
)
from booley.ticket_board.lifecycle import TicketState
from booley.ticket_board.logs import append_incident, clear_from_step


@pytest.fixture
def logs_dir(tmp_path):
    d = tmp_path / "logs"
    d.mkdir()
    return d


def _tickets_dir(logs_dir: Path) -> Path:
    """The tickets dir owning *logs_dir*, where state records live."""
    return logs_dir.parent


# ---------------------------------------------------------------------------
# Runtime fields in the state record (replaced progress.json)
# ---------------------------------------------------------------------------


class TestResetRuntimeFromStep:
    def test_clears_error_fields_and_keeps_state(self, logs_dir):
        (logs_dir / "t").mkdir()
        record = StateRecord.fresh(
            TicketState.BLOCKED,
            step="implementation",
            steps_completed=["setup", "planning", "implementation", "custom"],
            error="boom",
            failed_step="sim",
            blocked_reason="stuck",
            blocked_step="sim",
            execution_id="exec-1",
        )
        write_state_record(_tickets_dir(logs_dir), "t", record)
        clear_from_step(logs_dir, "t", "implementation")
        result = read_state_record(_tickets_dir(logs_dir), "t")
        assert result is not None
        assert result.state is TicketState.BLOCKED
        # Steps before the target stay done; custom steps survive the reset.
        assert result.runtime["steps_completed"] == ["setup", "planning", "run-config", "custom"]
        for key in ("error", "failed_step", "blocked_reason", "blocked_step"):
            assert result.runtime[key] is None
        assert result.runtime["execution_id"] == "exec-1"
        assert result.runtime["last_update"]

    def test_draft_without_record_stays_a_draft(self, logs_dir):
        (logs_dir / "t").mkdir()
        clear_from_step(logs_dir, "t", "plan")
        assert not state_record_path(_tickets_dir(logs_dir), "t").exists()

    def test_corrupt_record_fails_closed(self, logs_dir):
        (logs_dir / "t").mkdir()
        path = state_record_path(_tickets_dir(logs_dir), "t")
        path.parent.mkdir(parents=True)
        path.write_text("not json", encoding="utf-8")
        with pytest.raises(StateRecordError):
            clear_from_step(logs_dir, "t", "plan")
        assert path.read_text(encoding="utf-8") == "not json"


class TestRuntimeDefault:
    def test_returns_deep_copy(self):
        val = runtime_default("steps_completed")
        assert val == []
        val.append("x")
        assert runtime_default("steps_completed") == []


# ---------------------------------------------------------------------------
# Incidents
# ---------------------------------------------------------------------------


class TestAppendIncident:
    def test_creates_incidents_file(self, logs_dir):
        n = append_incident(logs_dir, "t", "timeout", "sim", "sim timed out")
        assert n == 1
        content = (logs_dir / "t" / "incidents.md").read_text(encoding="utf-8")
        assert "# Incidents" in content
        assert "## Incident 1: timeout" in content
        assert "sim timed out" in content

    def test_increments_incident_number(self, logs_dir):
        append_incident(logs_dir, "t", "timeout", "sim", "first")
        n = append_incident(logs_dir, "t", "crash", "tb_coder", "second")
        assert n == 2
        content = (logs_dir / "t" / "incidents.md").read_text(encoding="utf-8")
        assert "## Incident 2: crash" in content

    def test_includes_resolution(self, logs_dir):
        append_incident(logs_dir, "t", "err", "lint", "bad", resolution="fixed by retry")
        content = (logs_dir / "t" / "incidents.md").read_text(encoding="utf-8")
        assert "fixed by retry" in content


# ---------------------------------------------------------------------------
# clear_from_step
# ---------------------------------------------------------------------------


class TestClearFromStep:
    def test_noop_when_log_dir_missing(self, logs_dir):
        clear_from_step(logs_dir, "nonexistent", "plan")

    def test_removes_status_json(self, logs_dir):
        slug_dir = logs_dir / "t"
        slug_dir.mkdir()
        status = slug_dir / "status.json"
        canonical_status = slug_dir / ".runtime" / "status.json"
        status.write_text("{}", encoding="utf-8")
        canonical_status.parent.mkdir()
        canonical_status.write_text("{}", encoding="utf-8")
        clear_from_step(logs_dir, "t", "plan")
        assert not status.exists()
        assert not canonical_status.exists()

    def test_appends_retry_banner(self, logs_dir):
        slug_dir = logs_dir / "t"
        slug_dir.mkdir()
        clear_from_step(logs_dir, "t", "plan")
        harness_log = slug_dir / "human-logs" / "harness.log"
        assert harness_log.exists()
        content = harness_log.read_text(encoding="utf-8")
        assert "RETRY from plan" in content

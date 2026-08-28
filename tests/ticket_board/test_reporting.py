"""Tests for ticket_board.reporting: step details, timing/usage reports, board display."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from booley.ticket_board.reporting import (
    _build_board_rows,
    _order_steps,
    display_board,
    format_step_detail,
    format_timing_report,
    format_usage_report,
)

# ---------------------------------------------------------------------------
# format_step_detail
# ---------------------------------------------------------------------------


class TestFormatStageDetail:
    def test_empty_meta(self):
        assert format_step_detail("planning", {}) == ""

    def test_planning_questions(self):
        meta = {"clarifying_questions": 3}
        result = format_step_detail("planning", meta)
        assert "3 clarifying questions" in result

    def test_planning_single_question(self):
        meta = {"clarifying_questions": 1}
        result = format_step_detail("planning", meta)
        assert "1 clarifying question" in result
        assert "questions" not in result  # no plural

    def test_review_clean(self):
        meta = {"issues_found": 0, "issues_fixed": 0}
        assert format_step_detail("rtl-review-1", meta) == "clean"

    def test_review_with_issues(self):
        meta = {"issues_found": 5, "issues_fixed": 3}
        result = format_step_detail("rtl-review-1", meta)
        assert "5 found" in result
        assert "3 fixed" in result

    def test_review_with_severity(self):
        meta = {
            "issues_found": 2,
            "issues_fixed": 1,
            "issues": [
                {"severity": "warning"},
                {"severity": "error"},
            ],
        }
        result = format_step_detail("rtl-review-1", meta)
        assert "warning" in result
        assert "error" in result

    def test_sim_debug_loop(self):
        meta = {
            "debug_rounds_used": 3,
            "debug_rounds_max": 5,
            "targets_passed": 2,
            "configs_failed": 1,
        }
        result = format_step_detail("sim-debug-loop", meta)
        assert "3/5 debug rounds" in result
        assert "2/3 configs pass" in result

    def test_mutation_testing(self):
        meta = {"mutations_injected": 10, "mutations_detected": 8, "detection_rate": 0.8}
        result = format_step_detail("rtl-mutation-testing", meta)
        assert "8/10 detected" in result
        assert "80%" in result

    def test_synthesis(self):
        meta = {"targets": [{"name": "config_a", "delta_pct": "+5%", "cells": 1234}]}
        result = format_step_detail("synthesis", meta)
        assert "config_a" in result
        assert "1234" in result

    def test_incidents_appended(self):
        meta = {"incidents": 2, "clarifying_questions": 1}
        result = format_step_detail("planning", meta)
        assert "2 incidents" in result

    def test_incidents_alone(self):
        meta = {"incidents": 1}
        result = format_step_detail("setup", meta)
        assert "1 incident" in result
        assert "incidents" not in result  # singular


# ---------------------------------------------------------------------------
# _order_steps
# ---------------------------------------------------------------------------


class TestOrderStages:
    def test_known_stages_ordered(self):
        d = {"implementation": 1, "setup": 2, "planning": 3}
        ordered = _order_steps(d)
        assert ordered.index("setup") < ordered.index("planning")
        assert ordered.index("planning") < ordered.index("implementation")

    def test_unknown_stages_appended(self):
        d = {"setup": 1, "custom-stage": 2}
        ordered = _order_steps(d)
        assert ordered[-1] == "custom-stage"


# ---------------------------------------------------------------------------
# format_timing_report
# ---------------------------------------------------------------------------


class TestFormatTimingReport:
    def test_empty_durations(self):
        report = format_timing_report({})
        assert "No timing data" in report

    def test_basic_report(self):
        durations = {"planning": 120, "implementation": 300}
        report = format_timing_report(durations, title="Test Timing")
        assert "Test Timing" in report
        assert "planning" in report
        assert "implementation" in report
        assert "Total" in report

    def test_with_stage_meta(self):
        durations = {"planning": 60}
        meta = {"planning": {"clarifying_questions": 2, "tokens": 5000}}
        report = format_timing_report(durations, step_meta=meta)
        assert "2 clarifying questions" in report


# ---------------------------------------------------------------------------
# format_usage_report
# ---------------------------------------------------------------------------


class TestFormatUsageReport:
    def _stage_data(self):
        return {
            "planning": {
                "input_tokens": 1000,
                "output_tokens": 500,
                "cache_read_tokens": 200,
                "cache_create_tokens": 100,
                "message_count": 5,
                "messages": [],
            },
        }

    def test_basic_report(self):
        report = format_usage_report(self._stage_data(), 0.05, "Usage Test")
        assert "Usage Test" in report
        assert "planning" in report
        assert "Total" in report
        assert "1,500 total tokens" in report
        assert "1,800 total tokens" not in report

    def test_with_stage_durations(self):
        report = format_usage_report(
            self._stage_data(),
            0.05,
            "Usage",
            step_durations={"planning": 120},
        )
        assert "Duration" in report


# ---------------------------------------------------------------------------
# _build_board_rows
# ---------------------------------------------------------------------------


class TestBuildBoardRows:
    def test_empty(self):
        rows, links, counts = _build_board_rows([])
        assert rows == []
        assert links == []
        assert counts == {}

    def test_single_ticket(self):
        tickets = [
            {
                "file": "queue/test.md",
                "status": "queued",
                "step": "planning",
                "steps_completed": ["setup"],
                "last_update": "2025-01-15T10:00:00Z",
                "priority": "high",
            }
        ]
        rows, links, counts = _build_board_rows(tickets)
        assert len(rows) == 1
        assert counts == {"queued": 1}
        # Row: (name, prio, status, step, endpoints, updated, error, criteria)
        assert rows[0][0] == "test"
        assert rows[0][1] == "high"
        assert rows[0][2] == "queued"
        # No tickets_dir → no link
        assert links == [""]

    def test_acceptance_progress_is_visible_in_status(self):
        tickets = [
            {
                "file": "review/partial.md",
                "status": "review",
                "acceptance_state": "published-project",
                "steps_completed": [],
            },
            {
                "file": "done/cleanup.md",
                "status": "done",
                "acceptance_state": "accepted",
                "steps_completed": [],
            },
        ]

        rows, _links, counts = _build_board_rows(tickets)

        assert rows[0][2] == "review/published-project"
        assert rows[1][2] == "done/cleanup-pending"
        assert counts == {"review": 1, "done": 1}

    def test_link_for_existing_file(self, tmp_path):
        (tmp_path / "queue").mkdir()
        ticket_md = tmp_path / "queue" / "test.md"
        ticket_md.write_text("# test")
        tickets = [
            {
                "file": "queue/test.md",
                "status": "queued",
                "steps_completed": [],
            }
        ]
        _rows, links, _counts = _build_board_rows(tickets, tickets_dir=tmp_path)
        assert links == [ticket_md.resolve().as_uri()]

    def test_no_link_for_missing_file(self, tmp_path):
        tickets = [
            {
                "file": "queue/gone.md",
                "status": "queued",
                "steps_completed": [],
            }
        ]
        _rows, links, _counts = _build_board_rows(tickets, tickets_dir=tmp_path)
        assert links == [""]

    def test_vscode_uses_plain_basename_not_osc8(self, tmp_path, monkeypatch):
        """In VS Code, the name becomes the plain basename with no OSC 8 link.

        VS Code ignores OSC 8 file:// links but auto-links plain paths, so the
        board hands it a clickable ``slug.md`` instead (microsoft/vscode#176812).
        """
        (tmp_path / "queue").mkdir()
        (tmp_path / "queue" / "test.md").write_text("# test")
        tickets = [{"file": "queue/test.md", "status": "queued", "steps_completed": []}]

        # Force the VS Code + colors-on path regardless of the test tty.
        monkeypatch.setattr("booley.ticket_board.reporting.COLORS_ENABLED", True)
        monkeypatch.setattr("booley.ticket_board.reporting.in_vscode", lambda: True)

        rows, links, _counts = _build_board_rows(tickets, tickets_dir=tmp_path)
        assert rows[0][0] == "test.md"  # basename with extension, not the bare stem
        assert links == [""]  # no OSC 8 wrapper — VS Code links the plain path itself

    def test_non_vscode_keeps_osc8_name_link(self, tmp_path, monkeypatch):
        """Outside VS Code, the compact stem + OSC 8 file:// link is preserved."""
        (tmp_path / "queue").mkdir()
        ticket_md = tmp_path / "queue" / "test.md"
        ticket_md.write_text("# test")
        tickets = [{"file": "queue/test.md", "status": "queued", "steps_completed": []}]

        monkeypatch.setattr("booley.ticket_board.reporting.COLORS_ENABLED", True)
        monkeypatch.setattr("booley.ticket_board.reporting.in_vscode", lambda: False)

        rows, links, _counts = _build_board_rows(tickets, tickets_dir=tmp_path)
        assert rows[0][0] == "test"
        assert links == [ticket_md.resolve().as_uri()]


# ---------------------------------------------------------------------------
# display_board
# ---------------------------------------------------------------------------


class TestDisplayBoard:
    def test_empty_board(self, capsys):
        display_board([])
        out = capsys.readouterr().out
        assert "empty" in out.lower()

    def test_with_tickets(self, capsys):
        tickets = [
            {
                "file": "queue/a.md",
                "status": "queued",
                "step": "",
                "steps_completed": [],
                "last_update": "",
                "priority": "medium",
            },
            {
                "file": "active/b.md",
                "status": "running",
                "step": "planning",
                "steps_completed": ["setup"],
                "last_update": "2025-01-15T10:00:00Z",
                "priority": "high",
            },
        ]
        display_board(tickets)
        out = capsys.readouterr().out
        assert "2 tickets" in out

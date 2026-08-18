"""Tests for ticket_board.pipeline_metrics: metrics computation and report formatting."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from booley.ticket_board.run_metrics import (
    format_metrics_report,
)

# ---------------------------------------------------------------------------
# format_metrics_report
# ---------------------------------------------------------------------------


class TestFormatMetricsReport:
    def _make_metrics(self, **overrides):
        """Build a minimal metrics dict for testing the formatter."""
        defaults = {
            "tickets": [],
            "summary": {
                "total_tickets": 0,
                "done": 0,
                "blocked": 0,
                "review": 0,
                "running": 0,
                "queued": 0,
                "waiting": 0,
                "success_rate": 0,
                "total_cost": 0.0,
                "total_tokens": 0,
                "avg_cost_per_ticket": 0.0,
                "avg_duration_completed": 0.0,
            },
            "stage_stats": {},
            "failure_modes": {},
            "failure_errors": [],
            "type_stats": {},
        }
        defaults.update(overrides)
        return defaults

    def test_empty_metrics(self):
        report = format_metrics_report(self._make_metrics())
        assert "Run Metrics Dashboard" in report
        assert "Overview" in report

    def test_with_tickets(self):
        metrics = self._make_metrics(
            summary={
                "total_tickets": 5,
                "done": 3,
                "blocked": 1,
                "review": 1,
                "running": 0,
                "queued": 0,
                "waiting": 0,
                "success_rate": 75,
                "total_cost": 12.50,
                "total_tokens": 500000,
                "avg_cost_per_ticket": 3.13,
                "avg_duration_completed": 1800,
            },
            tickets=[
                {
                    "slug": "t1",
                    "status": "done",
                    "type": "feature",
                    "priority": "high",
                    "steps_completed": 5,
                    "cost": 5.0,
                    "total_tokens": 200000,
                    "duration_secs": 1200,
                },
            ],
        )
        report = format_metrics_report(metrics)
        assert "75%" in report
        assert "$12.50" in report

    def test_failure_hotspots_section(self):
        metrics = self._make_metrics(
            failure_modes={"sim-debug-loop": 3, "implementation": 1},
        )
        report = format_metrics_report(metrics)
        assert "Failure Hotspots" in report
        assert "sim-debug-loop" in report

    def test_type_stats_section(self):
        metrics = self._make_metrics(
            type_stats={
                "feature": {
                    "count": 3,
                    "done": 2,
                    "blocked": 1,
                    "total_cost": 10.0,
                    "total_duration": 3600,
                },
            },
        )
        report = format_metrics_report(metrics)
        assert "By Ticket Type" in report
        assert "feature" in report

    def test_cost_per_ticket_section(self):
        metrics = self._make_metrics(
            tickets=[
                {
                    "slug": "expensive",
                    "status": "done",
                    "type": "bugfix",
                    "priority": "high",
                    "steps_completed": 10,
                    "cost": 15.0,
                    "total_tokens": 1000000,
                    "duration_secs": 3600,
                },
            ],
        )
        report = format_metrics_report(metrics)
        assert "Cost Per Ticket" in report
        assert "expensive" in report

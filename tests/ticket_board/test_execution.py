"""Tests for ticket_board.pipeline: step logic, classification, resume detection."""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from booley.ticket_board.execution import (
    classify_tickets,
    next_from_planned,
    resume_detect,
    select_mutation_config,
)

# ---------------------------------------------------------------------------
# next_from_planned
# ---------------------------------------------------------------------------


class TestNextFromPlanned:
    def test_returns_next_step(self):
        stages = ["setup", "planning", "implementation"]
        assert next_from_planned(stages, "planning") == "implementation"

    def test_returns_none_at_end(self):
        stages = ["setup", "planning"]
        assert next_from_planned(stages, "planning") is None

    def test_returns_none_for_unknown(self):
        stages = ["setup", "planning"]
        assert next_from_planned(stages, "unknown") is None

    def test_first_step(self):
        stages = ["setup", "planning", "implementation"]
        assert next_from_planned(stages, "setup") == "planning"

    def test_single_step(self):
        assert next_from_planned(["only"], "only") is None


# ---------------------------------------------------------------------------
# select_mutation_config
# ---------------------------------------------------------------------------


class TestSelectMutationConfig:
    def test_returns_first(self):
        assert select_mutation_config(["config_a", "config_b"]) == "config_a"

    def test_empty_list(self):
        assert select_mutation_config([]) is None

    def test_single_config(self):
        assert select_mutation_config(["only"]) == "only"


# ---------------------------------------------------------------------------
# classify_tickets
# ---------------------------------------------------------------------------


class TestClassifyTickets:
    def test_empty_list(self):
        result = classify_tickets([], logs_dir=Path("/tmp/logs"))
        assert result["executable"] == []
        assert result["blocked"] == []

    def test_queued_is_executable(self):
        tickets = [{"status": "queued", "file": "queue/t.md"}]
        result = classify_tickets(tickets, logs_dir=Path("/tmp/logs"))
        assert len(result["executable"]) == 1

    def test_blocked_is_blocked(self):
        tickets = [{"status": "blocked", "file": "blocked/t.md"}]
        result = classify_tickets(tickets, logs_dir=Path("/tmp/logs"))
        assert len(result["blocked"]) == 1

    def test_review_is_review(self):
        tickets = [{"status": "review", "file": "review/t.md"}]
        result = classify_tickets(tickets, logs_dir=Path("/tmp/logs"))
        assert len(result["review"]) == 1

    def test_waiting_is_waiting(self):
        tickets = [{"status": "waiting", "file": "waiting/t.md"}]
        result = classify_tickets(tickets, logs_dir=Path("/tmp/logs"))
        assert len(result["waiting"]) == 1

    def test_queued_with_unmet_deps_goes_to_waiting(self):
        tickets = [
            {"status": "queued", "file": "queue/t.md", "dependencies": ["dep-slug"]},
        ]
        result = classify_tickets(tickets, logs_dir=Path("/tmp/logs"))
        assert len(result["executable"]) == 0
        assert len(result["waiting"]) == 1

    def test_queued_with_met_deps_is_executable(self):
        tickets = [
            {"status": "done", "file": "done/dep-slug.md", "feature_branch": "dep-slug"},
            {"status": "queued", "file": "queue/t.md", "dependencies": ["dep-slug"]},
        ]
        result = classify_tickets(tickets, logs_dir=Path("/tmp/logs"))
        assert len(result["executable"]) == 1

    def test_executable_sorted_by_priority_then_creation(self):
        """Priority outranks creation time in claim order (F-51).

        The regression: a `medium` ticket created 2s earlier was claimed ahead
        of a `high` one because nothing made the order total and the caller
        shuffled it away.
        """
        tickets = [
            {
                "status": "queued",
                "file": "queue/older-medium.md",
                "slug": "older-medium",
                "priority": "medium",
                "created": "2026-07-26T11:00:00Z",
            },
            {
                "status": "queued",
                "file": "queue/newer-high.md",
                "slug": "newer-high",
                "priority": "high",
                "created": "2026-07-26T11:00:02Z",
            },
            {
                "status": "queued",
                "file": "queue/oldest-low.md",
                "slug": "oldest-low",
                "priority": "low",
                "created": "2026-07-26T10:00:00Z",
            },
        ]
        result = classify_tickets(tickets, logs_dir=Path("/tmp/logs"))
        assert [t["slug"] for t in result["executable"]] == [
            "newer-high",
            "older-medium",
            "oldest-low",
        ]

    def test_equal_priority_breaks_tie_on_creation_time(self):
        """Same priority => oldest first; an undated ticket sorts last (F-51)."""
        tickets = [
            {"status": "queued", "file": "queue/c.md", "slug": "c", "priority": "medium"},
            {
                "status": "queued",
                "file": "queue/b.md",
                "slug": "b",
                "priority": "medium",
                "created": "2026-07-26T11:00:05Z",
            },
            {
                "status": "queued",
                "file": "queue/a.md",
                "slug": "a",
                "priority": "medium",
                "created": "2026-07-26T11:00:01Z",
            },
        ]
        result = classify_tickets(tickets, logs_dir=Path("/tmp/logs"))
        assert [t["slug"] for t in result["executable"]] == ["a", "b", "c"]

    def test_running_recent_is_active(self, tmp_path: Path):
        """Running ticket with recent timestamp should be active, not orphaned."""
        now = datetime.now(UTC)
        tickets = [
            {
                "status": "running",
                "file": "active/t.md",
                "last_update": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
        ]
        logs_dir = tmp_path / "logs"
        logs_dir.mkdir()
        result = classify_tickets(tickets, orphan_threshold_min=30, logs_dir=logs_dir)
        assert len(result["active"]) == 1
        assert len(result["orphaned"]) == 0

    def test_running_stale_is_orphaned(self, tmp_path: Path):
        """Running ticket with old timestamp and no lock PID should be orphaned."""
        old = datetime.now(UTC) - timedelta(hours=2)
        tickets = [
            {
                "status": "running",
                "file": "active/t.md",
                "last_update": old.strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
        ]
        logs_dir = tmp_path / "logs"
        (logs_dir / "t").mkdir(parents=True)
        result = classify_tickets(tickets, orphan_threshold_min=30, logs_dir=logs_dir)
        assert len(result["orphaned"]) == 1

    def test_executable_sorted_by_priority(self):
        tickets = [
            {"status": "queued", "file": "queue/low.md", "priority": "low"},
            {"status": "queued", "file": "queue/high.md", "priority": "high"},
            {"status": "queued", "file": "queue/med.md", "priority": "medium"},
        ]
        result = classify_tickets(tickets, logs_dir=Path("/tmp/logs"))
        slugs = [Path(t["file"]).stem for t in result["executable"]]
        assert slugs == ["high", "med", "low"]


# ---------------------------------------------------------------------------
# resume_detect
# ---------------------------------------------------------------------------


class TestResumeDetect:
    def test_fresh_queued(self):
        entry = {"status": "queued", "steps_completed": [], "feature_branch": "feat"}
        result = resume_detect(entry)
        assert result["action"] == "fresh"
        assert result["stage"] == "setup"
        assert result["feature_branch"] == "feat"

    def test_continue_running(self):
        entry = {
            "status": "running",
            "steps_completed": ["setup", "planning"],
            "feature_branch": "feat",
        }
        result = resume_detect(entry)
        assert result["action"] == "continue"
        assert result["stage"] == "run-config"

    def test_complete_all_steps(self):
        from booley.ticket_board.constants import STEP_ORDER

        entry = {
            "status": "running",
            "steps_completed": list(STEP_ORDER),
            "feature_branch": "feat",
        }
        result = resume_detect(entry)
        assert result["action"] == "complete"

    def test_resume_blocked(self):
        entry = {
            "status": "blocked",
            "steps_completed": ["setup"],
            "blocked_step": "planning",
            "feature_branch": "feat",
        }
        result = resume_detect(entry)
        assert result["action"] == "resume_blocked"
        assert result["stage"] == "planning"
        assert "blocked_reason" in result["clear_fields"]

    def test_queued_with_steps_and_blocked_step(self):
        entry = {
            "status": "queued",
            "steps_completed": ["setup", "planning"],
            "blocked_step": "implementation",
            "feature_branch": "feat",
        }
        result = resume_detect(entry)
        assert result["action"] == "resume_blocked"
        assert result["stage"] == "implementation"

    def test_queued_with_steps_no_block(self):
        entry = {
            "status": "queued",
            "steps_completed": ["setup", "planning"],
            "feature_branch": "feat",
        }
        result = resume_detect(entry)
        assert result["action"] == "continue"
        assert result["stage"] == "run-config"

    def test_valid_ticket_type_no_warning(self, capsys):
        entry = {
            "status": "queued",
            "steps_completed": [],
            "feature_branch": "f",
            "type": "bugfix",
        }
        result = resume_detect(entry)
        assert result["action"] == "fresh"
        captured = capsys.readouterr()
        assert "Warning" not in captured.err

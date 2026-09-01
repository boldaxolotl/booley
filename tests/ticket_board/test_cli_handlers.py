"""Tests for ticket_board.cli_handlers: command handler dispatch."""

from __future__ import annotations

import json
import sys
from argparse import Namespace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from booley.criteria.state import DevelopmentState
from booley.ticket_board.acceptance_ledger import freeze_acceptance
from booley.ticket_board.cli_handlers import (
    _cmd_board,
    _cmd_classify,
    _cmd_endpoint_table,
    _cmd_next_step_or_steps,
    _cmd_parse_ticket,
    _cmd_read_board,
    _cmd_show,
    _cmd_slug,
    _cmd_timing,
    _cmd_validate_logs,
)
from booley.ticket_board.paths import existing_runtime_file

from .conftest import make_ticket_file

# ---------------------------------------------------------------------------
# _cmd_slug
# ---------------------------------------------------------------------------


class TestCmdSlug:
    def test_slug_output(self, tio, capsys):
        args = Namespace(summary="Fix counter overflow bug")
        rc = _cmd_slug(tio, args)
        assert rc == 0
        out = capsys.readouterr().out.strip()
        assert out == "fix-counter-overflow-bug"


# ---------------------------------------------------------------------------
# _cmd_board
# ---------------------------------------------------------------------------


class TestCmdBoard:
    def test_empty_board(self, tio, capsys):
        args = Namespace()
        rc = _cmd_board(tio, args)
        assert rc == 0
        out = capsys.readouterr().out
        assert "empty" in out.lower()

    def test_board_with_tickets(self, tio, capsys):
        make_ticket_file(tio, "queue", "test-ticket")
        args = Namespace()
        rc = _cmd_board(tio, args)
        assert rc == 0
        out = capsys.readouterr().out
        assert "test-ticket" in out


# ---------------------------------------------------------------------------
# _cmd_read_board
# ---------------------------------------------------------------------------


class TestCmdReadBoard:
    def test_empty_board_json(self, tio, capsys):
        args = Namespace()
        rc = _cmd_read_board(tio, args)
        assert rc == 0
        out = capsys.readouterr().out
        data = json.loads(out)
        assert data["tickets"] == []

    def test_board_with_ticket(self, tio, capsys):
        make_ticket_file(tio, "queue", "my-ticket")
        args = Namespace()
        rc = _cmd_read_board(tio, args)
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert len(data["tickets"]) == 1


# ---------------------------------------------------------------------------
# _cmd_parse_ticket
# ---------------------------------------------------------------------------


class TestCmdParseTicket:
    def test_parse_existing_ticket(self, tio, capsys):
        path = make_ticket_file(tio, "queue", "test-parse")
        args = Namespace(path=str(path))
        rc = _cmd_parse_ticket(tio, args)
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert "fields" in data
        assert data["fields"]["type"] == "feature"

    def test_parse_missing_file(self, tio, capsys):
        args = Namespace(path="/nonexistent/file.md")
        rc = _cmd_parse_ticket(tio, args)
        assert rc == 2
        data = json.loads(capsys.readouterr().out)
        assert "error" in data


# ---------------------------------------------------------------------------
# _cmd_classify
# ---------------------------------------------------------------------------


class TestCmdClassify:
    def test_classify_empty(self, tio, capsys):
        args = Namespace(format="json")
        rc = _cmd_classify(tio, args)
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert "executable" in data

    def test_classify_counts_format(self, tio, capsys):
        make_ticket_file(tio, "queue", "ticket-a")
        args = Namespace(format="counts")
        rc = _cmd_classify(tio, args)
        assert rc == 0
        out = capsys.readouterr().out
        assert "executable=" in out

    def test_classify_with_queued_ticket(self, tio, capsys):
        make_ticket_file(tio, "queue", "exec-ticket")
        args = Namespace(format="json")
        rc = _cmd_classify(tio, args)
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert len(data["executable"]) == 1


# ---------------------------------------------------------------------------
# _cmd_next_step_or_steps
# ---------------------------------------------------------------------------


class TestCmdNextStage:
    def test_next_stage_returns_next(self, tio, capsys):
        args = Namespace(type_or_slug="feature", current="planning", skip="")
        rc = _cmd_next_step_or_steps(tio, args, "next-step")
        assert rc == 0
        out = capsys.readouterr().out.strip()
        assert out == "run-config"

    def test_next_stage_done(self, tio, capsys):
        args = Namespace(type_or_slug="feature", current="review", skip="")
        rc = _cmd_next_step_or_steps(tio, args, "next-step")
        assert rc == 0
        out = capsys.readouterr().out.strip()
        assert out == "done"

    def test_stages_lists_all(self, tio, capsys):
        args = Namespace(type_or_slug="bugfix", current="setup", skip="")
        rc = _cmd_next_step_or_steps(tio, args, "stages")
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert "setup" in data
        assert "planning" in data

    def test_next_stage_with_skip(self, tio, capsys):
        args = Namespace(type_or_slug="feature", current="planning", skip="run-config")
        rc = _cmd_next_step_or_steps(tio, args, "next-step")
        assert rc == 0
        out = capsys.readouterr().out.strip()
        assert out == "implementation"

    def test_invalid_type_and_no_ticket(self, tio, capsys):
        args = Namespace(type_or_slug="nonexistent", current="planning", skip="")
        rc = _cmd_next_step_or_steps(tio, args, "next-step")
        assert rc == 1


# ---------------------------------------------------------------------------
# _cmd_validate_logs
# ---------------------------------------------------------------------------


class TestCmdValidateLogs:
    def test_ticket_not_found(self, tio, capsys):
        args = Namespace(slug="nonexistent")
        rc = _cmd_validate_logs(tio, args)
        assert rc == 1
        data = json.loads(capsys.readouterr().out)
        assert "error" in data


# ---------------------------------------------------------------------------
# _cmd_show
# ---------------------------------------------------------------------------


class TestCmdShow:
    def test_no_slug_aliases_board(self, tio, capsys):
        # With no slug, show is a plain alias for board (empty here).
        rc = _cmd_show(tio, Namespace(slug=None))
        assert rc == 0
        assert "empty" in capsys.readouterr().out.lower()

    def test_known_slug_prints_paths_and_criteria(self, tio, capsys):
        make_ticket_file(
            tio,
            "queue",
            "show-me",
            extra_fields={
                "criteria": {
                    "mandatory": {"sim_pass": ["tb/foo_tb.sv @ default @ all @ pass -> pass"]},
                    "optional": {"lint_clean": ["lint -> pass"]},
                },
            },
        )
        rc = _cmd_show(tio, Namespace(slug="show-me"))
        assert rc == 0
        out = capsys.readouterr().out
        for label in ("ticket:", "file:", "logs:", "worktree:", "branch:", "criteria:"):
            assert label in out

    def test_unknown_slug_returns_2(self, tio, capsys):
        rc = _cmd_show(tio, Namespace(slug="nope"))
        assert rc == 2

    def test_done_ticket_reads_accepted_snapshot_after_runtime_cleanup(self, tio, capsys):
        make_ticket_file(tio, "done", "completed")
        state_path = existing_runtime_file(tio.logs_dir, "completed", "booley_state.json")
        state = DevelopmentState.load(state_path)
        state.slug = "completed"
        state.init_criteria({"sim_pass": True}, strict=True)
        state.set_criterion("sim_pass", True, detail={"passed_tests": ["smoke"]})
        state.save()
        freeze_acceptance(
            tio.logs_dir / "completed",
            state,
            execution_id="run-1",
            target_contract=None,
        )
        state_path.unlink()

        rc = _cmd_show(tio, Namespace(slug="completed"))

        assert rc == 0
        assert "mandatory 1/1 met" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# _cmd_endpoint_table
# ---------------------------------------------------------------------------


def _write_timeline(tio, slug: str, timeline: list) -> None:
    """Drop a booley_state.json with *timeline* at the runtime path show/table read."""
    path = existing_runtime_file(tio.logs_dir, slug, "booley_state.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"timeline": timeline}), encoding="utf-8")


class TestCmdEndpointTable:
    def test_reports_endpoints_and_total(self, tio, capsys):
        _write_timeline(
            tio,
            "timed",
            [
                {
                    "flow": "sim",
                    "exit_code": 0,
                    "duration_s": 12.5,
                    "cost_usd": 0.02,
                    "criteria_set": ["sim_pass"],
                },
                {
                    "flow": "synth",
                    "exit_code": 1,
                    "duration_s": 30.0,
                    "cost_usd": 0.05,
                    "criteria_set": [],
                },
            ],
        )
        rc = _cmd_endpoint_table(tio, Namespace(slug="timed", by_endpoint=True, save=False))
        assert rc == 0
        out = capsys.readouterr().out
        assert "sim" in out
        assert "synth" in out
        assert "Total" in out

    def test_absent_timeline_returns_2(self, tio, capsys):
        rc = _cmd_endpoint_table(tio, Namespace(slug="no-state", by_endpoint=True, save=False))
        assert rc == 2

    def test_timing_by_endpoint_routes_to_table(self, tio, capsys):
        _write_timeline(
            tio,
            "routed",
            [
                {
                    "flow": "sim",
                    "exit_code": 0,
                    "duration_s": 1.0,
                    "cost_usd": 0.01,
                    "criteria_set": [],
                },
            ],
        )
        rc = _cmd_timing(tio, Namespace(slug="routed", by_endpoint=True, save=False))
        assert rc == 0
        assert "sim" in capsys.readouterr().out

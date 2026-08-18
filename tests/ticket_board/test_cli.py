"""Tests for ticket_board.cli: parser construction and main() dispatch."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from booley.ticket_board.cli import HANDLERS, build_parser, main

# ---------------------------------------------------------------------------
# Parser construction
# ---------------------------------------------------------------------------


class TestBuildParser:
    def test_parser_builds(self):
        parser = build_parser()
        assert parser is not None
        assert parser.prog == "ticket_board"

    def test_all_handlers_have_subparsers(self):
        """Every key in HANDLERS should correspond to a registered subcommand."""
        parser = build_parser()
        # Extract subparser choices from the _SubParsersAction
        choices = set()
        for action in parser._subparsers._actions:
            if hasattr(action, "choices") and action.choices is not None:
                choices.update(action.choices.keys())
        assert choices, "No subparsers found"
        for cmd in HANDLERS:
            assert cmd in choices, f"Handler '{cmd}' has no subparser"

    def test_slug_subcommand_parses(self):
        parser = build_parser()
        args = parser.parse_args(["slug", "Fix counter overflow"])
        assert args.command == "slug"
        assert args.summary == "Fix counter overflow"

    def test_next_step_subcommand(self):
        parser = build_parser()
        args = parser.parse_args(["next-step", "bugfix", "planning"])
        assert args.command == "next-step"
        assert args.type_or_slug == "bugfix"
        assert args.current == "planning"
        assert args.skip == ""

    def test_next_step_with_skip(self):
        parser = build_parser()
        args = parser.parse_args(
            ["next-step", "feature", "planning", "--skip", "rtl-review-1,tb-review"]
        )
        assert args.skip == "rtl-review-1,tb-review"

    def test_update_board_set_fields(self):
        parser = build_parser()
        args = parser.parse_args(
            ["update-board", "my-slug", "--set", "step=planning", "status=running"]
        )
        assert args.command == "update-board"
        assert args.slug == "my-slug"
        assert args.set == ["step=planning", "status=running"]

    def test_update_board_reset_steps_mutual_exclusion(self):
        """--reset-steps and --reset-steps-from are mutually exclusive."""
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(
                ["update-board", "slug", "--reset-steps", "--reset-steps-from", "planning"]
            )

    def test_block_subcommand(self):
        parser = build_parser()
        args = parser.parse_args(
            ["block", "fix-bug", "--reason", "sim fail", "--step", "sim-debug-loop"]
        )
        assert args.slug == "fix-bug"
        assert args.reason == "sim fail"
        assert args.step == "sim-debug-loop"

    def test_create_file_subcommand(self):
        parser = build_parser()
        args = parser.parse_args(
            [
                "create-file",
                "new-feature",
                "--summary",
                "Add widget",
                "--type",
                "feature",
                "--branch",
                "master",
            ]
        )
        assert args.slug == "new-feature"
        assert args.summary == "Add widget"
        assert args.ticket_type == "feature"

    def test_approve_defaults(self):
        parser = build_parser()
        args = parser.parse_args(["approve", "fix-thing"])
        assert args.actor == "ticket-triage"
        assert args.detail == "user approved merge"

    def test_no_command_defaults_to_none(self):
        parser = build_parser()
        args = parser.parse_args([])
        assert args.command is None


# ---------------------------------------------------------------------------
# main() dispatch
# ---------------------------------------------------------------------------


class TestMainDispatch:
    @patch("booley.ticket_board.cli.detect_tickets_dir")
    @patch("booley.ticket_board.cli.TicketIO")
    def test_default_command_is_board(self, mock_tio_cls, mock_detect, tmp_path):
        mock_detect.return_value = tmp_path
        mock_tio = MagicMock()
        mock_tio_cls.return_value = mock_tio
        mock_handler = MagicMock(return_value=0)
        with patch.dict(HANDLERS, {"board": mock_handler}):
            main([])
            mock_handler.assert_called_once()
            # Verify it was called with the tio instance and a Namespace
            call_args = mock_handler.call_args[0]
            assert call_args[0] is mock_tio

    @patch("booley.ticket_board.cli.detect_tickets_dir")
    @patch("booley.ticket_board.cli.TicketIO")
    def test_slug_command(self, mock_tio_cls, mock_detect, tmp_path, capsys):
        mock_detect.return_value = tmp_path
        mock_tio_cls.return_value = MagicMock()
        rc = main(["slug", "Hello World Test"])
        assert rc == 0
        out = capsys.readouterr().out.strip()
        assert out == "hello-world-test"

    @patch("booley.ticket_board.cli.detect_tickets_dir")
    @patch("booley.ticket_board.cli.TicketIO")
    def test_slug_arg_strips_md_suffix(self, mock_tio_cls, mock_detect, tmp_path):
        """A copied-from-board ``<slug>.md`` reaches the handler as a bare slug."""
        mock_detect.return_value = tmp_path
        mock_tio_cls.return_value = MagicMock()
        mock_handler = MagicMock(return_value=0)
        with patch.dict(HANDLERS, {"show": mock_handler}):
            main(["show", "my-feature.md"])
        args = mock_handler.call_args[0][1]
        assert args.slug == "my-feature"

    @patch("booley.ticket_board.cli.detect_tickets_dir")
    @patch("booley.ticket_board.cli.TicketIO")
    def test_type_or_slug_arg_strips_md_suffix(self, mock_tio_cls, mock_detect, tmp_path):
        """The next-step/steps ``type_or_slug`` arg also accepts a ``.md`` name."""
        mock_detect.return_value = tmp_path
        mock_tio_cls.return_value = MagicMock()
        mock_handler = MagicMock(return_value=0)
        with patch.dict(HANDLERS, {"steps": mock_handler}):
            main(["steps", "my-feature.md"])
        args = mock_handler.call_args[0][1]
        assert args.type_or_slug == "my-feature"

    @patch("booley.ticket_board.cli.detect_tickets_dir")
    @patch("booley.ticket_board.cli.TicketIO")
    def test_unknown_handler_prints_help(self, mock_tio_cls, mock_detect, tmp_path, capsys):
        mock_detect.return_value = tmp_path
        mock_tio_cls.return_value = MagicMock()
        # Temporarily remove a handler to test fallthrough
        with patch.dict(HANDLERS, clear=True):
            rc = main(["board"])
        assert rc == 1

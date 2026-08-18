"""Tests for filesystem failure scenarios during critical ticket state transitions.

Targets "silent state corruption" bugs where partial failures leave
inconsistent state in progress.json or logs/.
"""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Ensure ticket_board is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from booley.ticket_board import PROGRESS_DEFAULTS, TicketIO, save_progress
from booley.ticket_board.operations import op_reset
from booley.ticket_board.paths import human_log_file

# ---------------------------------------------------------------------------
# Helpers (mirrors ticket_board test patterns)
# ---------------------------------------------------------------------------


def make_tio(tmp_path):
    """Create a tickets dir with subdirectories and return a TicketIO instance."""
    tickets_dir = tmp_path / "tickets"
    for d in [
        "board/queue",
        "board/waiting",
        "board/active",
        "board/blocked",
        "board/review",
        "board/done",
        "board/archived",
    ]:
        (tickets_dir / d).mkdir(parents=True, exist_ok=True)
    (tickets_dir / "logs").mkdir(parents=True, exist_ok=True)
    return TicketIO(tickets_dir)


def make_ticket_file(tio, subdir, slug, extra_fields=""):
    """Create a ticket .md file under tickets_dir/board/<subdir>/<slug>.md."""
    content = (
        "---\n"
        f"summary: {slug.replace('-', ' ')}\n"
        "type: feature\n"
        "branch: master\n"
        "scope_current:\n  - rtl/foo.sv\n"
        "scope_new: []\n"
        "test: {tb/foo_tb.sv@config_a/v01: pass}\n"
        f"{extra_fields}"
        "---\n"
        "## Description\nSome work.\n"
    )
    subdir_full = f"board/{subdir}" if not subdir.startswith("board/") else subdir
    d = tio.tickets_dir / subdir_full
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{slug}.md"
    p.write_text(content, encoding="utf-8")
    return p


def set_progress(tio, slug, overrides):
    """Write a progress.json with PROGRESS_DEFAULTS + overrides."""
    import copy

    progress = copy.deepcopy(PROGRESS_DEFAULTS)
    progress.update(overrides)
    save_progress(tio.logs_dir, slug, progress)


# ---------------------------------------------------------------------------
# TestOpRejectPartialFailure
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# TestOpResetAuditTrail
# ---------------------------------------------------------------------------


class TestOpResetAuditTrail:
    """Bug/behavior: op_reset does shutil.rmtree(log_dir) which destroys
    transitions.log — the entire audit trail. After reset, only the
    reset entry remains."""

    def test_transitions_log_destroyed(self, tmp_path):
        """After op_reset, old transition entries are gone.
        Only the reset entry itself survives.

        This documents known behavior: the full audit trail is lost on reset.
        A production system would typically preserve or archive it.
        """
        tio = make_tio(tmp_path)
        slug = "test-reset-audit"

        make_ticket_file(tio, "active", slug)
        set_progress(
            tio,
            slug,
            {
                "step": "implementation",
                "steps_completed": ["planning"],
            },
        )

        # Write several transition entries (simulating normal lifecycle)
        log_dir = tio.logs_dir / slug
        log_dir.mkdir(parents=True, exist_ok=True)
        transitions_log = human_log_file(tio.logs_dir, slug, "transitions.log")
        transitions_log.parent.mkdir(parents=True, exist_ok=True)
        transitions_log.write_text(
            "2026-01-01T00:00:00Z | queued:init -> running:init | ticket-execute | picked up\n"
            "2026-01-01T01:00:00Z | running:init -> running:planning | ticket-execute | step complete\n"
            "2026-01-01T02:00:00Z | running:planning -> running:implementation | ticket-execute | step complete\n",
            encoding="utf-8",
        )

        # Verify 3 entries exist before reset
        lines_before = [l for l in transitions_log.read_text().strip().splitlines() if l.strip()]
        assert len(lines_before) == 3

        # Patch git ops to avoid real git calls
        with patch("booley.ticket_board.operations.cleanup_worktree_and_branch"):
            result = op_reset(tio, slug)

        assert result is True

        # After reset, transitions.log should exist with all 3 original entries
        # PLUS the reset entry (audit trail preserved after fix)
        transitions_log = human_log_file(tio.logs_dir, slug, "transitions.log")
        assert transitions_log.exists(), "transitions.log should survive reset"

        lines_after = [l for l in transitions_log.read_text().strip().splitlines() if l.strip()]
        assert len(lines_after) == 4, (
            f"Expected 4 entries (3 original + 1 reset), got {len(lines_after)}: {lines_after}"
        )
        assert "user reset ticket" in lines_after[-1]
        # Original audit trail is preserved
        assert "picked up" in lines_after[0]

    def test_ticket_md_preserved_after_reset(self, tmp_path):
        """op_reset preserves the ticket.md snapshot in logs/."""
        tio = make_tio(tmp_path)
        slug = "test-reset-preserve"

        make_ticket_file(tio, "active", slug)
        set_progress(tio, slug, {"step": "planning"})

        # Create ticket.md snapshot in logs (as init_ticket would)
        log_dir = tio.logs_dir / slug
        log_dir.mkdir(parents=True, exist_ok=True)
        ticket_md = log_dir / "ticket.md"
        ticket_md.write_text("# Original ticket snapshot\n", encoding="utf-8")

        with patch("booley.ticket_board.operations.cleanup_worktree_and_branch"):
            result = op_reset(tio, slug)

        assert result is True
        # ticket.md should be preserved
        assert ticket_md.exists()
        assert "Original ticket snapshot" in ticket_md.read_text()


# ---------------------------------------------------------------------------
# TestBlockTicketCascadingFailure
# ---------------------------------------------------------------------------


class TestBlockTicketCascadingFailure:
    """Tests that errors in block_ticket propagate correctly to the caller.

    If ticket_cli.block() raises, the developer's catch-all may try
    fail_ticket() as fallback. If that also fails, the ticket is stranded
    in active/. We test the propagation at the blocking.py level."""

    @patch(
        "booley.harness.blocking.ticket_cli.block",
        side_effect=RuntimeError("CLI subprocess crashed"),
    )
    def test_block_ticket_propagates_cli_error(self, mock_block, sample_ctx):
        """When ticket_cli.block raises, block_ticket must propagate the error
        (not swallow it), so the caller can attempt fallback handling."""
        from booley.harness.blocking import block_ticket

        with pytest.raises(RuntimeError, match="CLI subprocess crashed"):
            block_ticket(sample_ctx, reason="need info", step="planning")

        # The CLI was called (and raised)
        mock_block.assert_called_once()

    @patch(
        "booley.harness.blocking.ticket_cli.block", side_effect=RuntimeError("block also crashed")
    )
    def test_fail_ticket_propagates_cli_error(self, mock_block, sample_ctx):
        """If fail_ticket's CLI call also raises, that error propagates too.
        This is the cascading failure scenario: block fails, then fail (which
        delegates to block) also fails, leaving the ticket stranded in active/."""
        from booley.harness.blocking import fail_ticket

        with pytest.raises(RuntimeError, match="block also crashed"):
            fail_ticket(sample_ctx, error="original error", step="planning")

        mock_block.assert_called_once()

    @patch("booley.harness.blocking.ticket_cli.block", side_effect=RuntimeError("block crashed"))
    def test_block_then_fail_both_raise(self, mock_block, sample_ctx):
        """Simulates the full cascading failure: block raises, caller tries
        fail as fallback (which also delegates to block), fail also raises.
        Ticket stays in active/."""
        from booley.harness.blocking import block_ticket, fail_ticket

        # First: block_ticket fails
        with pytest.raises(RuntimeError, match="block crashed"):
            block_ticket(sample_ctx, reason="ambiguous", step="planning")

        # Caller's fallback: try to fail the ticket — also raises (delegates to block)
        with pytest.raises(RuntimeError, match="block crashed"):
            fail_ticket(sample_ctx, error="block failed", step="planning")

        # Both calls went through ticket_cli.block
        assert mock_block.call_count == 2

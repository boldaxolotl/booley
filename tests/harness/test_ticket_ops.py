"""Tests for harness._ticket_ops — ticket operations protocol and DI."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from booley.harness._ticket_ops import (
    DirectTicketOps,
    TicketCLIError,
    TicketOps,
    _check,
    get_ticket_ops,
    set_ticket_ops,
)

# ---------------------------------------------------------------------------
# TicketCLIError
# ---------------------------------------------------------------------------


class TestTicketCLIError:
    def test_attributes(self):
        err = TicketCLIError("activate", 2, "some error")
        assert err.subcommand == "activate"
        assert err.returncode == 2
        assert err.stderr == "some error"

    def test_message_format(self):
        err = TicketCLIError("block", 1, "nope")
        assert "ticket_board block" in str(err)
        assert "rc=1" in str(err)
        assert "nope" in str(err)

    def test_inherits_exception(self):
        assert issubclass(TicketCLIError, Exception)


# ---------------------------------------------------------------------------
# _check helper
# ---------------------------------------------------------------------------


class TestCheckHelper:
    def test_ok_true_no_raise(self):
        _check(True, "activate", "slug-1")  # should not raise

    def test_ok_false_raises(self):
        with pytest.raises(TicketCLIError) as exc_info:
            _check(False, "activate", "slug-1")
        assert exc_info.value.subcommand == "activate"
        assert exc_info.value.returncode == 2
        assert "slug-1" in exc_info.value.stderr


# ---------------------------------------------------------------------------
# TicketOps protocol
# ---------------------------------------------------------------------------


class TestTicketOpsProtocol:
    def test_direct_implements_protocol(self):
        """DirectTicketOps must satisfy the TicketOps runtime protocol."""
        assert isinstance(DirectTicketOps(), TicketOps)


# ---------------------------------------------------------------------------
# get_ticket_ops / set_ticket_ops (DI)
# ---------------------------------------------------------------------------


class TestTicketOpsDI:
    def setup_method(self):
        # Reset global state before each test
        set_ticket_ops(None)

    def teardown_method(self):
        set_ticket_ops(None)

    def test_default_is_direct(self):
        ops = get_ticket_ops()
        assert isinstance(ops, DirectTicketOps)

    def test_set_and_get_custom(self):
        custom = MagicMock(spec=TicketOps)
        set_ticket_ops(custom)
        assert get_ticket_ops() is custom

    def test_reset_to_default(self):
        custom = MagicMock(spec=TicketOps)
        set_ticket_ops(custom)
        set_ticket_ops(None)
        ops = get_ticket_ops()
        assert isinstance(ops, DirectTicketOps)

    def test_singleton_behavior(self):
        """get_ticket_ops() returns the same instance on repeated calls."""
        a = get_ticket_ops()
        b = get_ticket_ops()
        assert a is b


# ---------------------------------------------------------------------------
# DirectTicketOps._tio
# ---------------------------------------------------------------------------


class TestDirectTicketOpsTio:
    def test_tio_honors_project_dir_env(self, tmp_path: Path, monkeypatch):
        """Regression (ADR 0028): _tio must honor BOOLEY_PROJECT_DIR, not
        hand-join project_root/.booley_project. In the devcontainer those are
        two bind mounts of the same dir; when _tio picked the /work mount while
        the harness resolved paths via the /booley-project mount, shutil.move
        crossed the two and raised EXDEV. Both sides must agree on the env dir.
        """
        proj = tmp_path / "elsewhere" / "proj-data"
        (proj / "tickets").mkdir(parents=True)
        monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(proj))
        ops = DirectTicketOps()
        tio = ops._tio(tmp_path)  # project_root differs from the env dir
        assert tio.tickets_dir == proj / "tickets"

    def test_tio_prefers_booley_project(self, tmp_path: Path, monkeypatch):
        """Without env overrides, should use .booley_project if it exists."""
        monkeypatch.delenv("BOOLEY_PROJECT_DIR", raising=False)
        monkeypatch.delenv("TICKETS_DIR", raising=False)
        bp = tmp_path / ".booley_project" / "tickets"
        bp.mkdir(parents=True)
        ops = DirectTicketOps()
        tio = ops._tio(tmp_path)
        assert "booley_project" in str(tio.tickets_dir)

    def test_tio_falls_back_to_booley_project(self, tmp_path: Path, monkeypatch):
        """Without env overrides, fall back to .booley/project if .booley_project missing."""
        monkeypatch.delenv("BOOLEY_PROJECT_DIR", raising=False)
        monkeypatch.delenv("TICKETS_DIR", raising=False)
        bp = tmp_path / ".booley" / "project" / "tickets"
        bp.mkdir(parents=True)
        ops = DirectTicketOps()
        tio = ops._tio(tmp_path)
        assert "project" in str(tio.tickets_dir)


# ---------------------------------------------------------------------------
# DirectTicketOps.parse_ticket
# ---------------------------------------------------------------------------


class TestDirectTicketOpsParseTicket:
    def test_file_not_found_raises(self, tmp_path: Path):
        ops = DirectTicketOps()
        with pytest.raises(TicketCLIError, match="not found"):
            ops.parse_ticket(tmp_path, str(tmp_path / "nonexistent.md"))


# ---------------------------------------------------------------------------
# DirectTicketOps.validate_ticket
# ---------------------------------------------------------------------------


class TestDirectTicketOpsValidateTicket:
    def test_file_not_found(self, tmp_path: Path):
        ops = DirectTicketOps()
        result = ops.validate_ticket(tmp_path, str(tmp_path / "nope.md"))
        assert result["errors"]
        assert "not found" in result["errors"][0].lower()


# ---------------------------------------------------------------------------
# DirectTicketOps.generate_slug
# ---------------------------------------------------------------------------


class TestDirectTicketOpsGenerateSlug:
    def test_generates_slug(self, tmp_path: Path):
        ops = DirectTicketOps()
        with patch("booley.ticket_board.helpers.generate_slug", return_value="fix-fsm-overflow"):
            slug = ops.generate_slug(tmp_path, "Fix FSM overflow bug")
        assert slug == "fix-fsm-overflow"

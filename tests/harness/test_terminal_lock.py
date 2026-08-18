"""Tests for terminal.py — thread safety and output formatting.

Verifies that concurrent calls to terminal output functions don't interleave,
and that individual functions produce the expected format.
"""

from __future__ import annotations

import io
import re
import threading
from unittest.mock import patch

from booley.harness import terminal

# ---------------------------------------------------------------------------
# ANSI stripping helper
# ---------------------------------------------------------------------------

_ANSI_RE = re.compile(r"\033\[[0-9;]*m")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


# ---------------------------------------------------------------------------
# Thread safety — concurrent calls must not interleave
# ---------------------------------------------------------------------------

_NUM_THREADS = 8
_CALLS_PER_THREAD = 20


def _writer(barrier: threading.Barrier, buf: io.StringIO, thread_id: int) -> None:
    """Each thread writes tagged lines via different terminal functions."""
    barrier.wait()  # synchronise start so contention is maximised
    for i in range(_CALLS_PER_THREAD):
        tag = f"T{thread_id}-{i}"
        # Rotate across several functions to exercise different lock paths
        match i % 4:
            case 0:
                terminal.status(tag)
            case 1:
                terminal.step_line(tag)
            case 2:
                terminal.heartbeat_line(tag, "0m01s")
            case 3:
                terminal.endpoint_heartbeat(tag, 61.0)


def test_concurrent_output_no_interleaving():
    """Lines printed from multiple threads must be atomic (no partial lines)."""
    buf = io.StringIO()
    barrier = threading.Barrier(_NUM_THREADS)
    threads = [
        threading.Thread(target=_writer, args=(barrier, buf, tid)) for tid in range(_NUM_THREADS)
    ]

    with patch(
        "builtins.print",
        side_effect=lambda *a, **kw: buf.write(" ".join(str(x) for x in a) + "\n"),
    ):
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

    lines = buf.getvalue().splitlines()
    # We expect exactly _NUM_THREADS * _CALLS_PER_THREAD non-empty lines
    assert len(lines) == _NUM_THREADS * _CALLS_PER_THREAD

    # Every line must contain exactly one T<id>-<seq> tag — if lines
    # interleaved, we'd see zero or multiple tags on a single line.
    tag_re = re.compile(r"T\d+-\d+")
    for line in lines:
        plain = _strip_ansi(line)
        tags = tag_re.findall(plain)
        assert len(tags) == 1, f"Interleaved or missing tag in line: {line!r}"


# ---------------------------------------------------------------------------
# endpoint_box_open formatting
# ---------------------------------------------------------------------------


class TestEndpointBoxOpen:
    def test_basic(self, capsys):
        terminal.endpoint_box_open("lint")
        out = _strip_ansi(capsys.readouterr().out)
        assert "┌─" in out
        assert "lint" in out

    def test_with_config(self, capsys):
        terminal.endpoint_box_open("sim", target="lite")
        out = _strip_ansi(capsys.readouterr().out)
        assert "sim [lite]" in out
        assert "┌─" in out

    def test_reviewer_uses_dedicated_color(self, capsys):
        with patch("booley.harness.colors.COLORS_ENABLED", True):
            terminal.endpoint_box_open("reviewer", target="setup")
        out = capsys.readouterr().out
        assert "\033[38;5;141m" in out
        assert "reviewer [setup]" in _strip_ansi(out)


# ---------------------------------------------------------------------------
# endpoint_box_close formatting
# ---------------------------------------------------------------------------


class TestEndpointBoxClose:
    def test_pass_with_display_lines(self, capsys):
        terminal.endpoint_box_close(
            "lint",
            "lite",
            exit_code=0,
            duration_s=42.0,
            display_lines=["0 warnings", "0 errors"],
        )
        out = _strip_ansi(capsys.readouterr().out)
        # Display lines printed with box-drawing pipe prefix
        assert "│ 0 warnings" in out
        assert "│ 0 errors" in out
        # Status icon
        assert "[PASS]" in out
        # Duration
        assert "42s" in out
        # Closing bar with label
        assert "lint [lite]" in out

    def test_fail_icon(self, capsys):
        terminal.endpoint_box_close(
            "sim",
            None,
            exit_code=1,
            duration_s=120.0,
        )
        out = _strip_ansi(capsys.readouterr().out)
        assert "[FAIL]" in out
        # Duration >= 60s should use minute format
        assert "2.0m" in out

    def test_error_icon(self, capsys):
        terminal.endpoint_box_close(
            "sim",
            None,
            exit_code=2,
            duration_s=5.0,
        )
        out = _strip_ansi(capsys.readouterr().out)
        assert "[ERR]" in out

    def test_cost_shown(self, capsys):
        terminal.endpoint_box_close(
            "tb_coder",
            None,
            exit_code=0,
            duration_s=30.0,
            cost_usd=1.23,
        )
        out = _strip_ansi(capsys.readouterr().out)
        assert "$1.23" in out

    def test_no_display_lines(self, capsys):
        """When display_lines is None, only the status + closing bar appear."""
        terminal.endpoint_box_close(
            "lint",
            None,
            exit_code=0,
            duration_s=10.0,
        )
        out = _strip_ansi(capsys.readouterr().out)
        lines = [l for l in out.splitlines() if l.strip()]
        # Status line + closing bar only (no display content lines)
        assert len(lines) == 2
        assert "└─" in lines[-1]


# ---------------------------------------------------------------------------
# agent_text formatting
# ---------------------------------------------------------------------------


class TestAgentText:
    def test_single_line(self, capsys):
        terminal.agent_text("thinking hard")
        out = _strip_ansi(capsys.readouterr().out)
        assert ". thinking hard" in out

    def test_multiline(self, capsys):
        terminal.agent_text("line one\nline two\nline three")
        out = _strip_ansi(capsys.readouterr().out)
        lines = [l for l in out.splitlines() if l.strip()]
        assert len(lines) == 3
        for line in lines:
            assert line.strip().startswith(". ")

    def test_empty_string(self, capsys):
        terminal.agent_text("")
        out = capsys.readouterr().out
        # "".splitlines() is [], so nothing is printed
        assert out == ""


# ---------------------------------------------------------------------------
# endpoint_heartbeat formatting
# ---------------------------------------------------------------------------


class TestEndpointHeartbeat:
    def test_under_one_minute(self, capsys):
        terminal.endpoint_heartbeat("lint", 45.0)
        out = _strip_ansi(capsys.readouterr().out)
        assert "lint" in out
        assert "0m45s" in out

    def test_over_one_minute(self, capsys):
        terminal.endpoint_heartbeat("sim", 125.0)
        out = _strip_ansi(capsys.readouterr().out)
        assert "2m05s" in out

    def test_exact_minute(self, capsys):
        terminal.endpoint_heartbeat("sim", 180.0)
        out = _strip_ansi(capsys.readouterr().out)
        assert "3m00s" in out

    def test_zero(self, capsys):
        terminal.endpoint_heartbeat("tb_coder", 0.0)
        out = _strip_ansi(capsys.readouterr().out)
        assert "0m00s" in out

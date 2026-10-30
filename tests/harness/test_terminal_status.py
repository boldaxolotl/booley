"""Tests for the shared status-line helpers (``booley.harness.terminal_status``)."""

from __future__ import annotations

import importlib
import importlib.util
import io
import os
import sys

import pytest

from booley.harness import colors

_GLYPHS = {
    "pass": "✓",
    "warn": "!",
    "fail": "✗",
    "skip": "·",
    "note": "·",
    "waived": "~",
    "running": "▸",
}


def _status_module():
    """Import ``terminal_status`` lazily so the red run collects before the module exists."""
    assert importlib.util.find_spec("booley.harness.terminal_status"), (
        "shared status-line helper missing"
    )
    return importlib.import_module("booley.harness.terminal_status")


@pytest.mark.parametrize("status_name", list(_GLYPHS))
def test_status_line_has_one_glyph_in_a_fixed_column(status_name, monkeypatch):
    monkeypatch.setattr(colors, "COLORS_ENABLED", False)
    ts = _status_module()

    line = ts.status_line(ts.LineStatus(status_name), "message text")

    assert line == f"  {_GLYPHS[status_name]}  message text"
    assert line.index("message text") == 5


def test_check_id_is_dimmed_and_right_aligned_without_brackets(monkeypatch):
    monkeypatch.setattr(colors, "COLORS_ENABLED", True)
    ts = _status_module()

    monkeypatch.setattr(ts, "_line_width", lambda: 60)

    line = ts.status_line(ts.LineStatus.WARN, "trace unavailable", check_id="sim.trace")

    assert line.endswith("\033[2msim.trace\033[0m")
    plain = colors.strip_ansi(line)
    assert len(plain) == 60
    assert plain.endswith("  sim.trace")
    assert "[" not in plain


def test_hint_sits_under_the_message_column(monkeypatch):
    monkeypatch.setattr(colors, "COLORS_ENABLED", False)
    ts = _status_module()

    row = ts.status_line(ts.LineStatus.FAIL, "broken")
    hint = ts.hint_line("repair it")

    assert hint == "     → repair it"
    assert hint.index("→") == row.index("broken")


def test_glyph_lines_survive_a_cp1252_stdout_after_utf8_setup(monkeypatch):
    """Windows consoles start as cp1252; the CLI's UTF-8 setup must carry the glyphs."""
    from booley.harness import booley as cli

    ts = _status_module()
    monkeypatch.setenv("PYTHONUTF8", "1")
    raw = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(raw, encoding="cp1252"))
    monkeypatch.setattr(sys, "stderr", io.TextIOWrapper(io.BytesIO(), encoding="cp1252"))

    cli._force_utf8()
    for status in ts.LineStatus:
        print(ts.status_line(status, "row"))
    sys.stdout.flush()

    written = raw.getvalue().decode("utf-8")
    assert all(glyph in written for glyph in _GLYPHS.values())


@pytest.mark.parametrize(("columns", "expected_width"), [(80, 80), (140, 100)])
def test_ids_end_at_the_terminal_width_capped_on_a_tty(columns, expected_width, monkeypatch):
    monkeypatch.setattr(colors, "COLORS_ENABLED", False)
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(
        "shutil.get_terminal_size", lambda _fallback: os.terminal_size((columns, 24))
    )
    ts = _status_module()

    assert len(ts.status_line(ts.LineStatus.WARN, "msg", check_id="a.b")) == expected_width

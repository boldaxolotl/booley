"""Tests for presentation.colors — terminal color utilities."""

from __future__ import annotations

import importlib

import pytest

from booley.presentation import colors

# Expected values are ANSI SGR sequences (ECMA-48), the terminal's own contract.
STYLED = [
    (colors.red, "\033[31mx\033[0m"),
    (colors.green, "\033[32mx\033[0m"),
    (colors.yellow, "\033[33mx\033[0m"),
    (colors.gray, "\033[90mx\033[0m"),
    (colors.bold, "\033[1mx\033[0m"),
    (colors.dim, "\033[2mx\033[0m"),
    (colors.bold_green, "\033[1m\033[32mx\033[0m"),
    (colors.bold_red, "\033[1m\033[31mx\033[0m"),
]


@pytest.mark.parametrize(("style", "styled"), STYLED)
def test_style_applies_only_while_colors_are_enabled(monkeypatch, style, styled):
    monkeypatch.setattr(colors, "COLORS_ENABLED", True)
    assert style("x") == styled
    monkeypatch.setattr(colors, "COLORS_ENABLED", False)
    assert style("x") == "x"


@pytest.mark.parametrize(
    ("text", "cells"),
    [
        ("hello", 5),
        ("", 0),
        ("\033[1m\033[0m", 0),
        ("abc\033[31mde\033[0m", 5),
        ("\033[38;5;123mx\033[1;31my\033[0m", 2),
        ("\033]8;;file:///tmp/t.md\033\\hello\033]8;;\033\\", 5),
        ("\033]8;;file:///tmp/t.md\007hello\033]8;;\007", 5),
    ],
)
def test_len_visible_ignores_style_and_hyperlink_sequences(text, cells):
    assert colors.len_visible(text) == cells


@pytest.mark.parametrize(
    ("enabled", "uri", "expected"),
    [
        (True, "file:///tmp/t.md", "\033]8;;file:///tmp/t.md\033\\name\033]8;;\033\\"),
        (False, "file:///tmp/t.md", "name"),
        (True, "", "name"),
    ],
)
def test_hyperlink_wraps_only_with_colors_and_a_uri(monkeypatch, enabled, uri, expected):
    monkeypatch.setattr(colors, "COLORS_ENABLED", enabled)
    assert colors.hyperlink("name", uri) == expected


@pytest.mark.parametrize(
    ("term_program", "expected"),
    [("vscode", True), ("vsCode", True), ("iTerm.app", False), (None, False)],
)
def test_in_vscode_reads_term_program(monkeypatch, term_program, expected):
    if term_program is None:
        monkeypatch.delenv("TERM_PROGRAM", raising=False)
    else:
        monkeypatch.setenv("TERM_PROGRAM", term_program)
    assert colors.in_vscode() is expected


@pytest.mark.parametrize(
    ("no_color", "term", "expected"),
    [("1", "xterm", False), (None, "dumb", False), (None, "xterm", True)],
    ids=["no-color", "dumb-terminal", "capable-terminal"],
)
def test_import_decides_color_from_the_terminal(monkeypatch, no_color, term, expected):
    """NO_COLOR and TERM=dumb switch color off even on a TTY stdout."""
    # Reloading re-runs the import-time decision; teardown restores the original flag.
    monkeypatch.setattr(colors, "COLORS_ENABLED", colors.COLORS_ENABLED)
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    monkeypatch.setattr(colors.sys.stdout, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(colors.sys, "platform", "linux")
    monkeypatch.setenv("TERM", term)
    if no_color is None:
        monkeypatch.delenv("NO_COLOR", raising=False)
    else:
        monkeypatch.setenv("NO_COLOR", no_color)

    assert importlib.reload(colors).COLORS_ENABLED is expected

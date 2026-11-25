"""Tests for the shared terminal table renderer (``booley.presentation.terminal_table``)."""

from __future__ import annotations

import os
import shutil
import sys

import pytest

from booley.presentation import colors
from booley.presentation.terminal_table import render_table, width_budget


@pytest.fixture
def plain(monkeypatch):
    monkeypatch.setattr(colors, "COLORS_ENABLED", False)


def _plain(lines: list[str]) -> list[str]:
    return [colors.strip_ansi(line) for line in lines]


@pytest.mark.parametrize("color", [True, False], ids=["color", "plain"])
def test_colored_cells_align_by_visible_width(monkeypatch, color):
    monkeypatch.setattr(colors, "COLORS_ENABLED", color)
    rows = [
        [colors.green("x"), colors.bold("yyyy"), "z"],
        ["longer", "q", colors.dim("w")],
    ]
    lines = render_table(("A", "BB", "C"), rows)

    assert any("\x1b" in line for line in lines) is color
    assert _plain(lines) == [
        "  A        BB     C",
        "  ──────   ────   ─",
        "  x        yyyy   z",
        "  longer   q      w",
    ]


def test_capped_cell_is_cut_with_ellipsis(monkeypatch):
    monkeypatch.setattr(colors, "COLORS_ENABLED", True)
    rows = [["abcdefghij", "1"], [colors.green("klmnopqrst"), "2"], ["abc", "3"]]
    lines = render_table(("H", "N"), rows, max_widths={0: 5})

    assert len(lines) == 2 + len(rows)  # never wraps: one line per row
    assert _plain(lines)[2:] == ["  abcd…   1", "  klmn…   2", "  abc     3"]


def test_cap_cut_never_leaves_whitespace_before_the_ellipsis(plain):
    lines = render_table(("H",), [["ab cdefgh"]], max_widths={0: 4})

    assert lines[2] == "  ab…"


def test_cap_below_one_is_refused():
    with pytest.raises(ValueError, match="at least 1"):
        render_table(("H",), [["a"]], max_widths={0: 0})


@pytest.mark.parametrize("color", [True, False], ids=["color", "plain"])
def test_no_line_has_trailing_whitespace(monkeypatch, color):
    monkeypatch.setattr(colors, "COLORS_ENABLED", color)
    rows = [
        ["name", "value", "x"],
        ["longer-name", "v", ""],  # empty last cell
        ["short"],  # row shorter than the header
        ["a", "b", "c", "ignored extra cell"],
        ["", ""],  # nothing to show at all
    ]
    lines = render_table(("NAME", "VALUE", "LAST"), rows)

    assert len(lines) == 2 + len(rows)
    for line in _plain(lines):
        assert line == line.rstrip()
    assert _plain(lines)[3] == "  longer-name   v"
    assert _plain(lines)[4] == "  short"
    assert _plain(lines)[-1] == ""
    assert "ignored" not in "".join(lines)


def test_width_caps_only_the_widest_columns(plain):
    rows = [["w" * 30, "m" * 10, "abc"], ["short", "m", "x"]]
    lines = render_table(("A", "B", "C"), rows, width=40)

    # 40 - 2 indent - 2 gaps of 3 = 32 cells for columns: the widest shrinks to 19.
    assert max(len(line) for line in lines) <= 40
    assert lines[2] == "  " + "w" * 18 + "…" + "   " + "m" * 10 + "   abc"
    assert lines[3].startswith("  short" + " " * 17 + "m")

    narrow = render_table(("A", "B", "C"), rows, width=5)  # cannot fit: floors, no crash
    assert narrow[2] == "  …   …   …"

    unbounded = render_table(("A", "B", "C"), rows)
    assert unbounded[2].startswith("  " + "w" * 30)


def test_columns_and_cuts_count_terminal_cells(plain):
    rows = [["日本語", "x"], ["ab", "y"]]  # 6 cells, not 3 characters

    lines = render_table(("H", "N"), rows)
    assert lines[3] == "  ab       y"

    cut = render_table(("H", "N"), rows, max_widths={0: 5})
    assert cut[2] == "  日本…   x"
    assert colors.len_visible("é") == 1  # combining mark takes no cell


@pytest.mark.parametrize(
    ("tty", "columns", "expected"), [(False, 72, None), (True, 72, 72)], ids=["pipe", "tty"]
)
def test_width_budget(monkeypatch, tty, columns, expected):
    monkeypatch.setenv("COLUMNS", "300")  # misleading: the stubbed size wins
    monkeypatch.setattr(sys.stdout, "isatty", lambda: tty)
    monkeypatch.setattr(shutil, "get_terminal_size", lambda *_: os.terminal_size((columns, 24)))

    assert width_budget() == expected

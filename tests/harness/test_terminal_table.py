"""Tests for the shared terminal table renderer (``booley.harness.terminal_table``)."""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import os
import shutil
import sys

import pytest

from booley.harness import colors


def _renderer():
    """Import ``render_table`` lazily so the file collects before the module exists."""
    assert importlib.util.find_spec("booley.harness.terminal_table"), (
        "shared table renderer missing"
    )
    return importlib.import_module("booley.harness.terminal_table").render_table


def _plain(lines: list[str]) -> list[str]:
    return [colors.strip_ansi(line) for line in lines]


def _styled_rows() -> list[list[str]]:
    """Rows whose cells carry ANSI styling when colors are enabled."""
    return [
        [colors.green("x"), colors.bold("yyyy"), "z"],
        ["longer", "q", colors.dim("w")],
    ]


def test_colored_cells_align_by_visible_width(monkeypatch):
    monkeypatch.setattr(colors, "COLORS_ENABLED", True)
    lines = _renderer()(("A", "BB", "C"), _styled_rows())

    assert any("\x1b" in line for line in lines)
    assert _plain(lines) == [
        "  A        BB     C",
        "  ──────   ────   ─",
        "  x        yyyy   z",
        "  longer   q      w",
    ]


def test_capped_cell_is_cut_with_ellipsis(monkeypatch):
    monkeypatch.setattr(colors, "COLORS_ENABLED", True)
    rows = [["abcdefghij", "1"], [colors.green("klmnopqrst"), "2"], ["abc", "3"]]
    lines = _renderer()(("H", "N"), rows, max_widths={0: 5})

    assert len(lines) == 2 + len(rows)  # never wraps: one line per row
    assert _plain(lines)[2:] == ["  abcd…   1", "  klmn…   2", "  abc     3"]


def test_cap_cut_never_leaves_whitespace_before_the_ellipsis(monkeypatch):
    monkeypatch.setattr(colors, "COLORS_ENABLED", False)
    lines = _renderer()(("H",), [["ab cdefgh"]], max_widths={0: 4})

    assert lines[2] == "  ab…"


def test_cap_below_one_is_refused():
    with pytest.raises(ValueError, match="at least 1"):
        _renderer()(("H",), [["a"]], max_widths={0: 0})


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
    lines = _renderer()(("NAME", "VALUE", "LAST"), rows)

    assert len(lines) == 2 + len(rows)
    for line in _plain(lines):
        assert line == line.rstrip()
    assert _plain(lines)[3] == "  longer-name   v"
    assert _plain(lines)[4] == "  short"
    assert _plain(lines)[-1] == ""
    assert "ignored" not in "".join(lines)


def test_plain_output_when_color_is_off(monkeypatch):
    render_table = _renderer()
    monkeypatch.setattr(colors, "COLORS_ENABLED", True)
    colored = render_table(("A", "BB", "C"), _styled_rows())
    monkeypatch.setattr(colors, "COLORS_ENABLED", False)
    plain = render_table(("A", "BB", "C"), _styled_rows())

    assert not any("\x1b" in line for line in plain)
    assert plain == _plain(colored)


def _fitting_renderer():
    """``render_table``, asserting it accepts a total ``width`` budget."""
    render_table = _renderer()
    assert "width" in inspect.signature(render_table).parameters, "render_table has no width"
    return render_table


def test_width_caps_only_the_widest_columns(monkeypatch):
    monkeypatch.setattr(colors, "COLORS_ENABLED", False)
    rows = [["w" * 30, "m" * 10, "abc"], ["short", "m", "x"]]
    lines = _fitting_renderer()(("A", "B", "C"), rows, width=40)

    # 40 - 2 indent - 2 gaps of 3 = 32 cells for columns: the widest shrinks to 19.
    assert max(len(line) for line in lines) <= 40
    assert lines[2] == "  " + "w" * 18 + "…" + "   " + "m" * 10 + "   abc"
    assert lines[3].startswith("  short" + " " * 17 + "m")

    narrow = _fitting_renderer()(("A", "B", "C"), rows, width=5)  # cannot fit: floors, no crash
    assert narrow[2] == "  …   …   …"

    unbounded = _fitting_renderer()(("A", "B", "C"), rows)
    assert unbounded[2].startswith("  " + "w" * 30)


def test_columns_and_cuts_count_terminal_cells(monkeypatch):
    monkeypatch.setattr(colors, "COLORS_ENABLED", False)
    render_table = _renderer()
    rows = [["日本語", "x"], ["ab", "y"]]  # 6 cells, not 3 characters

    lines = render_table(("H", "N"), rows)
    assert lines[3] == "  ab       y"

    cut = render_table(("H", "N"), rows, max_widths={0: 5})
    assert cut[2] == "  日本…   x"
    assert colors.len_visible("e\u0301") == 1  # combining mark takes no cell


@pytest.mark.parametrize(
    ("tty", "columns", "expected"), [(False, 72, None), (True, 72, 72)], ids=["pipe", "tty"]
)
def test_width_budget(monkeypatch, tty, columns, expected):
    module = importlib.import_module("booley.harness.terminal_table")
    assert hasattr(module, "width_budget"), "width_budget missing"
    monkeypatch.setenv("COLUMNS", "300")  # misleading: the stubbed size wins
    monkeypatch.setattr(sys.stdout, "isatty", lambda: tty)
    monkeypatch.setattr(shutil, "get_terminal_size", lambda *_: os.terminal_size((columns, 24)))

    assert module.width_budget() == expected

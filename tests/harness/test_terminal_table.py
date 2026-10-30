"""Tests for the shared terminal table renderer (``booley.harness.terminal_table``)."""

from __future__ import annotations

import importlib
import importlib.util

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

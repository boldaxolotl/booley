"""Tests for harness.render_md — markdown to ANSI terminal rendering."""

from __future__ import annotations

import inspect
import re
from unittest.mock import patch

import booley.harness.colors as colors_mod
from booley.harness import render_md
from booley.harness.render_md import (
    _is_separator,
    _parse_table_row,
    render,
)

# ---------------------------------------------------------------------------
# inline
# ---------------------------------------------------------------------------


class TestInline:
    def test_bold(self):
        """**text** should trigger bold styling."""
        with patch.object(colors_mod, "COLORS_ENABLED", False):
            assert render_md.inline("**bold**") == "bold"

    def test_italic(self):
        """*text* should trigger dim styling."""
        with patch.object(colors_mod, "COLORS_ENABLED", False):
            assert render_md.inline("*italic*") == "italic"

    def test_backtick(self):
        """`code` should trigger cyan styling."""
        with patch.object(colors_mod, "COLORS_ENABLED", False):
            assert render_md.inline("`code`") == "code"

    def test_mixed(self):
        with patch.object(colors_mod, "COLORS_ENABLED", False):
            result = render_md.inline("**bold** and `code`")
            assert "bold" in result
            assert "code" in result

    def test_no_formatting(self):
        assert render_md.inline("plain text") == "plain text"


# ---------------------------------------------------------------------------
# _is_separator / _parse_table_row
# ---------------------------------------------------------------------------


class TestTableHelpers:
    def test_separator_basic(self):
        assert _is_separator("| --- | --- |") is True

    def test_separator_with_alignment(self):
        assert _is_separator("| :--- | :---: | ---: |") is True

    def test_not_separator(self):
        assert _is_separator("| foo | bar |") is False

    def test_parse_table_row(self):
        cells = _parse_table_row("| A | B | C |")
        assert cells == ["A", "B", "C"]

    def test_parse_table_row_no_outer_pipes(self):
        cells = _parse_table_row("A | B | C")
        assert cells == ["A", "B", "C"]


# ---------------------------------------------------------------------------
# _render_table
# ---------------------------------------------------------------------------


class TestRenderTable:
    def test_basic_table(self):
        rows = [
            "| Name | Value |",
            "| --- | --- |",
            "| foo | 42 |",
            "| bar | 99 |",
        ]
        with patch.object(colors_mod, "COLORS_ENABLED", False):
            result = render("\n".join(rows)).splitlines()
        assert len(result) == 4  # header + separator + 2 data rows
        # Data rows contain cell values
        assert "foo" in result[2]
        assert "42" in result[2]
        assert "bar" in result[3]

    def test_table_with_fewer_cells(self):
        """Rows with fewer cells than header should not crash."""
        rows = [
            "| A | B | C |",
            "| --- | --- | --- |",
            "| x |",  # fewer cells
        ]
        with patch.object(colors_mod, "COLORS_ENABLED", False):
            result = render("\n".join(rows)).splitlines()
        assert len(result) >= 3

    def test_cheat_table_layout_is_kept_without_trailing_whitespace(self):
        md = "| Name | Value |\n|---|---|\n| **foo** | `42` |\n| barbaz | 9 |"
        with patch.object(colors_mod, "COLORS_ENABLED", False):
            lines = render(md).splitlines()
        assert lines == [
            "  Name     Value",
            "  ──────   ─────",
            "  foo      42",
            "  barbaz   9",
        ]

    def test_header_and_rule_use_the_shared_palette(self):
        md = "| Name | Value |\n|---|---|\n| foo | 42 |"
        with patch.object(colors_mod, "COLORS_ENABLED", True):
            lines = render(md).splitlines()
            expected_header = colors_mod.bold_accent("Name")
            expected_rule = colors_mod.chrome("────")
        assert expected_header in lines[0]
        assert lines[1].startswith("  " + expected_rule[: expected_rule.index("─")])


# ---------------------------------------------------------------------------
# render (full pipeline)
# ---------------------------------------------------------------------------


class TestRender:
    def _render_plain(self, md: str) -> str:
        with patch.object(colors_mod, "COLORS_ENABLED", False):
            return render(md)

    def test_h2(self):
        result = self._render_plain("## Title")
        assert "Title" in result
        # H2 gets an underline
        assert "─" in result

    def test_h3(self):
        result = self._render_plain("### Subtitle")
        assert "Subtitle" in result

    def test_h4(self):
        result = self._render_plain("#### Small")
        assert "Small" in result

    def test_bullet(self):
        result = self._render_plain("- item one")
        assert "•" in result
        assert "item one" in result

    def test_nested_bullet(self):
        result = self._render_plain("  - nested")
        assert "•" in result
        assert "nested" in result

    def test_plain_text(self):
        result = self._render_plain("Just normal text.")
        assert "Just normal text." in result

    def test_table_integration(self):
        md = "| A | B |\n| --- | --- |\n| 1 | 2 |"
        result = self._render_plain(md)
        assert "A" in result
        assert "1" in result

    def test_pipe_rows_without_separator(self):
        """Pipe rows not forming a valid table should be treated as inline text."""
        md = "| not a table |\n| also not |"
        result = self._render_plain(md)
        assert "not a table" in result

    def test_empty_input(self):
        result = self._render_plain("")
        assert result == "\n"

    def test_trailing_newline(self):
        result = self._render_plain("hello")
        assert result.endswith("\n")

    def test_heading_is_followed_by_one_blank_line(self):
        assert self._render_plain("### H\n\nbody") == "\nH\n\nbody\n"
        assert self._render_plain("text\n### H\nbody") == "text\n\nH\n\nbody\n"
        assert self._render_plain("text\n\n### H\n\n\nbody") == "text\n\nH\n\nbody\n"

    def _render_wrapped(self, md: str, width: int) -> list[str]:
        assert "width" in inspect.signature(render).parameters, "render has no width"
        return render(md, width=width).splitlines()

    def test_prose_and_bullets_wrap_within_width(self):
        prose = "alpha beta gamma delta epsilon zeta eta theta iota kappa"
        md = f"{prose}\n  indented {prose}\n- {prose}\n  - {prose}\nshort\n{'x' * 30}"
        with patch.object(colors_mod, "COLORS_ENABLED", False):
            lines = self._render_wrapped(md, 24)

        assert all(len(line) <= 24 for line in lines)
        assert " ".join(lines[0:3]) == prose
        assert lines[3].startswith("  indented") and lines[4].startswith("  ")
        bullets = [i for i, line in enumerate(lines) if "•" in line]
        assert lines[bullets[0] + 1].startswith("    ") and "•" not in lines[bullets[0] + 1]
        assert lines[bullets[1]].startswith("    •")  # nested bullet keeps its indent
        assert "short" in lines
        assert lines[-2:] == ["x" * 24, "x" * 6]  # a word wider than the line splits

    def test_wrap_closes_and_reopens_style_across_breaks(self):
        assert "width" in inspect.signature(render).parameters, "render has no width"
        with patch.object(colors_mod, "COLORS_ENABLED", True):
            lines = render("  `one two three four` end", width=12).splitlines()
        assert len(lines) == 3
        assert all(colors_mod.len_visible(line) <= 12 for line in lines)
        assert all(colors_mod.active_style(line) == "" for line in lines)  # no bleed
        # The span continues on line 2, so it reopens after the indent; "end" is outside it.
        assert re.match(r"  \x1b\[[0-9;]*mthree four", lines[1])
        assert lines[2] == "  end"

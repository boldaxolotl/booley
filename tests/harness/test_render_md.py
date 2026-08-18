"""Tests for harness.render_md — markdown to ANSI terminal rendering."""

from __future__ import annotations

from unittest.mock import patch

import booley.harness.colors as colors_mod
from booley.harness.render_md import (
    _inline,
    _is_separator,
    _parse_table_row,
    _render_table,
    render,
)

# ---------------------------------------------------------------------------
# _inline
# ---------------------------------------------------------------------------


class TestInline:
    def test_bold(self):
        """**text** should trigger bold styling."""
        with patch.object(colors_mod, "COLORS_ENABLED", False):
            assert _inline("**bold**") == "bold"

    def test_italic(self):
        """*text* should trigger dim styling."""
        with patch.object(colors_mod, "COLORS_ENABLED", False):
            assert _inline("*italic*") == "italic"

    def test_backtick(self):
        """`code` should trigger cyan styling."""
        with patch.object(colors_mod, "COLORS_ENABLED", False):
            assert _inline("`code`") == "code"

    def test_mixed(self):
        with patch.object(colors_mod, "COLORS_ENABLED", False):
            result = _inline("**bold** and `code`")
            assert "bold" in result
            assert "code" in result

    def test_no_formatting(self):
        assert _inline("plain text") == "plain text"


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
            result = _render_table(rows)
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
            result = _render_table(rows)
        assert len(result) >= 3


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

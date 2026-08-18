"""Tests for harness.colors — terminal color utilities."""

from __future__ import annotations

from unittest.mock import patch

import pytest

import booley.harness.colors as colors_mod
from booley.harness.colors import (
    _ANSI_RE,
    _FG,
    _style,
    bold,
    bold_green,
    bold_red,
    dim,
    gray,
    green,
    hyperlink,
    len_visible,
    red,
    yellow,
)

# ---------------------------------------------------------------------------
# _style
# ---------------------------------------------------------------------------


class TestStyle:
    def test_returns_plain_when_colors_disabled(self):
        with patch.object(colors_mod, "COLORS_ENABLED", False):
            assert _style("hello", "\033[1m") == "hello"

    def test_wraps_with_codes_when_enabled(self):
        with patch.object(colors_mod, "COLORS_ENABLED", True):
            result = _style("hello", "\033[1m")
            assert result.startswith("\033[1m")
            assert result.endswith("\033[0m")
            assert "hello" in result

    def test_multiple_codes(self):
        with patch.object(colors_mod, "COLORS_ENABLED", True):
            result = _style("x", "\033[1m", "\033[36m")
            assert result.startswith("\033[1m\033[36m")


# ---------------------------------------------------------------------------
# Color functions (when enabled)
# ---------------------------------------------------------------------------


class TestColorFunctions:
    @pytest.fixture(autouse=True)
    def enable_colors(self):
        with patch.object(colors_mod, "COLORS_ENABLED", True):
            yield

    @pytest.mark.parametrize(
        "fn,color_key",
        [
            (red, "red"),
            (green, "green"),
            (yellow, "yellow"),
            (gray, "gray"),
        ],
    )
    def test_single_color_wraps(self, fn, color_key):
        result = fn("test")
        assert _FG[color_key] in result
        assert "\033[0m" in result
        assert "test" in result

    @pytest.mark.parametrize("fn", [bold_green, bold_red])
    def test_bold_color_has_bold_code(self, fn):
        result = fn("test")
        assert "\033[1m" in result

    def test_bold(self):
        assert "\033[1m" in bold("x")

    def test_dim(self):
        assert "\033[2m" in dim("x")


# ---------------------------------------------------------------------------
# Color functions (when disabled)
# ---------------------------------------------------------------------------


class TestColorFunctionsDisabled:
    @pytest.fixture(autouse=True)
    def disable_colors(self):
        with patch.object(colors_mod, "COLORS_ENABLED", False):
            yield

    @pytest.mark.parametrize(
        "fn",
        [
            red,
            green,
            yellow,
            gray,
            bold,
            dim,
            bold_green,
            bold_red,
        ],
    )
    def test_returns_plain_text(self, fn):
        assert fn("hello") == "hello"


# ---------------------------------------------------------------------------
# len_visible
# ---------------------------------------------------------------------------


class TestLenVisible:
    def test_plain_string(self):
        assert len_visible("hello") == 5

    def test_strips_ansi(self):
        colored = "\033[1m\033[36mhello\033[0m"
        assert len_visible(colored) == 5

    def test_empty_string(self):
        assert len_visible("") == 0

    def test_only_ansi(self):
        assert len_visible("\033[1m\033[0m") == 0

    def test_mixed_content(self):
        # "abc" + ANSI + "de" -> visible = 5
        s = "abc\033[31mde\033[0m"
        assert len_visible(s) == 5

    def test_strips_osc8_hyperlink(self):
        s = "\033]8;;file:///tmp/t.md\033\\hello\033]8;;\033\\"
        assert len_visible(s) == 5

    def test_strips_osc8_bel_terminated(self):
        s = "\033]8;;file:///tmp/t.md\007hello\033]8;;\007"
        assert len_visible(s) == 5

    def test_hyperlinked_colored_text(self):
        with patch.object(colors_mod, "COLORS_ENABLED", True):
            s = hyperlink(bold("hello"), "file:///tmp/t.md")
        assert len_visible(s) == 5


# ---------------------------------------------------------------------------
# hyperlink
# ---------------------------------------------------------------------------


class TestHyperlink:
    def test_wraps_with_osc8_when_enabled(self):
        with patch.object(colors_mod, "COLORS_ENABLED", True):
            result = hyperlink("name", "file:///tmp/t.md")
        assert result == "\033]8;;file:///tmp/t.md\033\\name\033]8;;\033\\"

    def test_plain_when_disabled(self):
        with patch.object(colors_mod, "COLORS_ENABLED", False):
            assert hyperlink("name", "file:///tmp/t.md") == "name"

    def test_plain_when_no_uri(self):
        with patch.object(colors_mod, "COLORS_ENABLED", True):
            assert hyperlink("name", "") == "name"


# ---------------------------------------------------------------------------
# in_vscode
# ---------------------------------------------------------------------------


class TestInVscode:
    def test_true_for_vscode_term_program(self, monkeypatch):
        monkeypatch.setenv("TERM_PROGRAM", "vscode")
        assert colors_mod.in_vscode() is True

    def test_case_insensitive(self, monkeypatch):
        monkeypatch.setenv("TERM_PROGRAM", "vsCode")
        assert colors_mod.in_vscode() is True

    def test_false_for_other_terminal(self, monkeypatch):
        monkeypatch.setenv("TERM_PROGRAM", "iTerm.app")
        assert colors_mod.in_vscode() is False

    def test_false_when_unset(self, monkeypatch):
        monkeypatch.delenv("TERM_PROGRAM", raising=False)
        assert colors_mod.in_vscode() is False


# ---------------------------------------------------------------------------
# _ANSI_RE
# ---------------------------------------------------------------------------


class TestAnsiRegex:
    @pytest.mark.parametrize(
        "code",
        [
            "\033[0m",
            "\033[1m",
            "\033[31m",
            "\033[1;31m",
            "\033[38;5;123m",
        ],
    )
    def test_matches_valid_codes(self, code):
        assert _ANSI_RE.match(code) is not None

    def test_no_match_plain(self):
        assert _ANSI_RE.search("plain text") is None

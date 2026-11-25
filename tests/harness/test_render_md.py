"""Tests for harness.render_md — markdown to ANSI terminal rendering.

The suite-wide ``_terminal_as_under_capture`` fixture turns colors off; the
``colored`` fixture turns them back on where a test checks the styling.
"""

from __future__ import annotations

import re

import pytest

import booley.presentation.colors as colors_mod
from booley.harness.render_md import inline, render


@pytest.fixture
def colored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(colors_mod, "COLORS_ENABLED", True)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("**bold**", lambda: colors_mod.bold("bold")),
        ("*italic*", lambda: colors_mod.dim("italic")),
        ("`code`", lambda: colors_mod.accent("code")),
        ("**b** and `c`", lambda: f"{colors_mod.bold('b')} and {colors_mod.accent('c')}"),
        ("plain text", lambda: "plain text"),
    ],
    ids=["bold", "italic", "code", "mixed", "plain"],
)
@pytest.mark.usefixtures("colored")
def test_inline_styles(text, expected):
    assert inline(text) == expected()


@pytest.mark.parametrize(
    ("md", "expected"),
    [
        ("## Title", "Title\n─────\n"),
        ("#### Small", "Small\n"),
        ("- item one", "  • item one\n"),
        ("  - nested", "    • nested\n"),
        ("Just normal text.", "Just normal text.\n"),
        ("| not a table |\n| also not |", "| not a table |\n| also not |\n"),
        ("", "\n"),
        (
            "| Name | Value |\n|---|---|\n| **foo** | `42` |\n| barbaz | 9 |",
            "  Name     Value\n  ──────   ─────\n  foo      42\n  barbaz   9\n",
        ),
        ("| A | B | C\n| :--- | :---: | ---: |\n| x |", "  A   B   C\n  ─   ─   ─\n  x\n"),
    ],
    ids=[
        "h2",
        "h4",
        "bullet",
        "nested-bullet",
        "prose",
        "pipes-without-separator",
        "empty",
        "table",
        "aligned-table-short-row",
    ],
)
def test_render_plain(md: str, expected: str) -> None:
    assert render(md) == expected


def test_heading_is_followed_by_one_blank_line():
    assert render("### H\n\nbody") == "\nH\n\nbody\n"
    assert render("text\n### H\nbody") == "text\n\nH\n\nbody\n"
    assert render("text\n\n### H\n\n\nbody") == "text\n\nH\n\nbody\n"


@pytest.mark.usefixtures("colored")
def test_table_header_and_rule_use_the_shared_palette():
    lines = render("| Name | Value |\n|---|---|\n| foo | 42 |").splitlines()
    expected_rule = colors_mod.chrome("────")
    assert colors_mod.bold_accent("Name") in lines[0]
    assert lines[1].startswith("  " + expected_rule[: expected_rule.index("─")])


def test_prose_and_bullets_wrap_within_width():
    prose = "alpha beta gamma delta epsilon zeta eta theta iota kappa"
    md = f"{prose}\n  indented {prose}\n- {prose}\n  - {prose}\nshort\n{'x' * 30}"
    lines = render(md, width=24).splitlines()

    assert all(len(line) <= 24 for line in lines)
    assert " ".join(lines[0:3]) == prose
    assert lines[3].startswith("  indented") and lines[4].startswith("  ")
    bullets = [i for i, line in enumerate(lines) if "•" in line]
    assert lines[bullets[0] + 1].startswith("    ") and "•" not in lines[bullets[0] + 1]
    assert lines[bullets[1]].startswith("    •")  # nested bullet keeps its indent
    assert "short" in lines
    assert lines[-2:] == ["x" * 24, "x" * 6]  # a word wider than the line splits


@pytest.mark.usefixtures("colored")
def test_wrap_closes_and_reopens_style_across_breaks():
    lines = render("  `one two three four` end", width=12).splitlines()
    assert len(lines) == 3
    assert all(colors_mod.len_visible(line) <= 12 for line in lines)
    assert all(colors_mod.active_style(line) == "" for line in lines)  # no bleed
    # The span continues on line 2, so it reopens after the indent; "end" is outside it.
    assert re.match(r"  \x1b\[[0-9;]*mthree four", lines[1])
    assert lines[2] == "  end"

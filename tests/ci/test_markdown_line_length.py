"""Markdown line-length gate: changed prose lines over 80 columns fail a branch."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from tests.git_support import git, repository
from tests.subprocess_budget import run_bounded_child

from booley.dev_support import markdown_line_length as gate

_SOURCE_ROOT = Path(__file__).resolve().parents[2]
_LAUNCHER = _SOURCE_ROOT / ".github/scripts/markdown_line_length.py"
_LONG = ("word " * 20).strip() + " end"  # 104 columns


def _write(repo: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(text.encode("utf-8"))


def _commit(repo: Path, files: dict[str, str], message: str) -> None:
    _write(repo, files)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)


@pytest.fixture
def repo(tmp_path: Path, isolated_git_attributes: None) -> Path:
    root = repository(tmp_path / "repo")
    return root


def _run(repo: Path, *args: str) -> tuple[int, str, str]:
    result = run_bounded_child([sys.executable, "-B", str(_LAUNCHER), *args], cwd=repo)
    return result.returncode, result.stdout, result.stderr


def test_branch_line_over_80_columns_fails_and_80_columns_passes(repo: Path) -> None:
    """Catches a `>=` boundary and a message that omits the path, line or length."""
    _commit(repo, {"doc.md": "short\n"}, "base")
    git(repo, "switch", "-q", "-c", "codex/feature")
    eighty, eighty_one = "x " * 39 + "xx", "x " * 39 + "xxx"
    assert (len(eighty), len(eighty_one)) == (80, 81)
    _commit(repo, {"doc.md": f"short\n{eighty}\n{eighty_one}\n"}, "branch")

    code, out, _ = _run(repo)

    assert code == 1
    assert "doc.md:3: 81 columns (limit 80)" in out
    assert "doc.md:2:" not in out
    assert out.count("columns (limit 80)") == 1


def test_unchanged_over_long_lines_pass(repo: Path) -> None:
    """Catches hunk ranges instead of added lines, and lost rename detection."""
    git(repo, "config", "diff.interHunkContext", "10")
    _commit(repo, {"doc.md": f"one\n{_LONG}\nthree\n", "moved.md": f"{_LONG}\n"}, "base")
    git(repo, "switch", "-q", "-c", "codex/feature")
    git(repo, "mv", "moved.md", "renamed.md")
    _commit(repo, {"doc.md": f"ONE\n{_LONG}\nTHREE\n"}, "branch")

    code, out, _ = _run(repo)

    assert code == 0, out
    assert "2 changed line(s) in 1 file(s)" in out


def test_generator_and_test_owned_markdown_is_not_checked(repo: Path) -> None:
    """Generated, fixture and changelog-fragment Markdown is owned elsewhere."""
    _commit(repo, {"keep.md": "x\n"}, "base")
    git(repo, "switch", "-q", "-c", "codex/feature")
    owned = ["CHANGELOG.md", "changelog.d/1.fix.md", "tests/fixtures/ticket_guidance/x.md"]
    _commit(repo, dict.fromkeys(owned, _LONG + "\n"), "owned")
    assert _run(repo)[0] == 0

    _commit(repo, {"changelog.d/README.md": _LONG + "\n"}, "readme")
    code, out, _ = _run(repo)
    assert code == 1
    assert "changelog.d/README.md:1: " in out


def test_added_line_that_looks_like_a_diff_header_is_content(repo: Path) -> None:
    """Catches `++ x` read as a `+++ ` file header, and form-feed line drift."""
    _commit(repo, {"doc.md": "x\n"}, "base")
    git(repo, "switch", "-q", "-c", "codex/feature")
    _commit(repo, {"doc.md": f"x\n++ counter\na\x0cb\r\n{_LONG}\n"}, "branch")

    code, out, _ = _run(repo)

    assert code == 1, out
    assert "doc.md:4: 103 columns (limit 80)" in out


def test_explicit_base_and_git_failures(repo: Path) -> None:
    """`--base` is honored; an unusable base or missing history exits 2."""
    _commit(repo, {"doc.md": "x\n"}, "base")
    base = git(repo, "rev-parse", "HEAD")
    _commit(repo, {"doc.md": f"x\n{_LONG}\n"}, "more")
    assert _run(repo, "--base", base)[0] == 1
    code, _, err = _run(repo, "--base", "no-such-rev")
    assert code == 2
    assert err.startswith("markdown line length: ")
    # Detached orphan history: no merge base with main.
    git(repo, "switch", "-q", "--orphan", "other")
    _commit(repo, {"o.md": "x\n"}, "orphan")
    code, _, err = _run(repo)
    assert code == 2
    assert "shares no history" in err


_FENCE = "`" * 3


_EXEMPT_CASES: list[list[str]] = [
    [f"{_FENCE}", _LONG, f"{_FENCE}"],
    ["~~~", _LONG, "~~~"],
    [f"> {_FENCE}", f"> {_LONG}", f"> {_FENCE}"],
    [f"{_FENCE}text", _LONG],  # unclosed fence runs to the end
    [f"- {_FENCE}text", "  " + _LONG, f"  {_FENCE}"],
    [f"{_FENCE}", "<!-- BEGIN GENERATED: x -->", _LONG, f"{_FENCE}"],
    ["| " + _LONG + " |"],
    ["<tr><td>" + _LONG + "</td></tr>"],
    ["## " + _LONG],
    ["#" * 6 + " " + _LONG],
    ["<https://example.com/" + "a" * 90 + ">"],
    ["https://example.com/" + "a" * 90],
    ["- [" + _LONG + "](https://example.com/x)"],
    ["1. [text](https://example.com/" + "a" * 90 + ")."],
    ["![alt](https://example.com/" + "a" * 90 + ")"],
    ["[![" + _LONG + "](img.svg#gh-light-mode-only)](img.svg)"],
    ["[" + "t" * 85 + "][label]"],
    ["[label]: https://example.com/" + "a" * 90],
    [_LONG[:70] + " see https://example.com/" + "a" * 90],
    [_LONG[:70] + " see `" + "a" * 90 + "`."],
    ["---", "description: " + _LONG, "---", "prose"],
    ["<!-- BEGIN GENERATED: x -->", _LONG, "<!-- END GENERATED: x -->"],
]


def test_exempt_lines_pass() -> None:
    """Each exemption passes with every over-80 line marked changed."""
    for lines in _EXEMPT_CASES:
        text = "\n".join(lines) + "\n"
        assert gate.long_lines(text, set(range(1, len(lines) + 1))) == [], lines


def test_prose_past_a_closed_fence_or_short_token_fails() -> None:
    """Exemptions end: after a closed fence, an inline span, a token ending early."""
    cases = [
        [f"{_FENCE}", "code", f"{_FENCE}", _LONG],
        [f"{_FENCE}literal{_FENCE}", _LONG],
        ["~~~", "code", "~~~~", _LONG],
        [_LONG[:30] + " `code` " + _LONG[:60]],
        [_LONG[:30] + " https://example.com/x " + _LONG[:60]],
        ["---", "a", "---", "---", _LONG],
        ["text", "---", _LONG],
        ["<!-- BEGIN GENERATED: x -->", "<!-- END GENERATED: x -->", _LONG],
        ["#hashtag " + _LONG],
        ["[![alt](img.svg)](img.svg) " + _LONG],
        ["| a", _LONG],
        ["[label]: ok", _LONG],
        [f"{_FENCE}", "<!-- BEGIN GENERATED: x -->", f"{_FENCE}", _LONG],
        [f"> {_FENCE}", "> code", "", _LONG],
    ]
    for lines in cases:
        text = "\n".join(lines) + "\n"
        found = gate.long_lines(text, set(range(1, len(lines) + 1)))
        assert [number for number, _ in found] == [len(lines)], lines

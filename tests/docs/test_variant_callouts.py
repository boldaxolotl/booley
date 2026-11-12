"""Variant callouts in docs/user/ carry a fixed label, a variant link, and block formatting.

Rule 26 of docs/internals/DOCS_PRINCIPLES.md keeps text that applies to one
variant (stealth mode, Windows, a cocotb testbench, host-installed licensed
tools) out of the main flow, in a ``<details markdown>`` callout. This guard
checks the shape of every callout: the label is one of four fixed strings, the
body links the variant's page, and the block formatting survives the site's
Python-Markdown renderer. Which passages belong in a callout stays an editorial
judgment; no test can decide that.
"""

from __future__ import annotations

import difflib
import re
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from markdown_it import MarkdownIt

from booley.runtime.docs_links import SITE_URL, USER_DOCS

ROOT = Path(__file__).resolve().parents[2]
USER_DOC_DIR = ROOT / "docs/user"

# Label -> (user doc the callout must link, anchor it must link or None for any).
_VARIANTS: dict[str, tuple[str, str | None]] = {
    "In stealth mode": ("STEALTH.md", None),
    "On Windows": ("WINDOWS.md", None),
    "With a cocotb testbench": ("TARGETS.md", "cocotb-targets-python-testbenches"),
    "With host-installed licensed tools": ("SANDBOX.md", "commercial-eda-provisioning"),
}

# Non-variant ``<summary>`` labels allowed in docs/user/. Add a label here to
# use a collapsible block that is not a variant callout; it skips the link and
# formatting checks. Empty today.
OTHER_LABELS: frozenset[str] = frozenset()

# Casefolded fragment -> canonical label, used only to name the expected label
# in a failure message. Order decides ties.
_VOCABULARY: tuple[tuple[str, str], ...] = (
    ("stealth", "In stealth mode"),
    ("cocotb", "With a cocotb testbench"),
    ("python", "With a cocotb testbench"),
    ("windows", "On Windows"),
    ("licens", "With host-installed licensed tools"),
    ("licenc", "With host-installed licensed tools"),
    ("vivado", "With host-installed licensed tools"),
    ("commercial", "With host-installed licensed tools"),
    ("host", "With host-installed licensed tools"),
)

_SUMMARY = re.compile(r"^\s*<summary>(.*)</summary>\s*$")
_FENCE = re.compile(r"^ *(```+|~~~+)(.*)$")
_DETAILS = "<details markdown>"
_SITE_DOC_FILES = {doc.slug: doc.file for doc in USER_DOCS}


def _expected_label(label: str) -> str | None:
    """Name the fixed label a non-canonical ``label`` most likely meant."""
    folded = label.casefold()
    for fragment, canonical in _VOCABULARY:
        if fragment in folded:
            return canonical
    close = difflib.get_close_matches(label, _VARIANTS, n=1, cutoff=0.6)
    return close[0] if close else None


def _link_targets(body: str, doc: str) -> set[tuple[str, str]]:
    """Return ``(user doc file, anchor)`` for each link in ``body`` that points at a user doc."""
    targets: set[tuple[str, str]] = set()
    for token in MarkdownIt().parse(body):
        for child in token.children or []:
            if child.type != "link_open":
                continue
            href = child.attrGet("href")
            parts = urlsplit(href if isinstance(href, str) else "")
            if parts.scheme in {"http", "https"}:
                if f"{parts.scheme}://{parts.netloc}/" != SITE_URL:
                    continue
                segments = [part for part in parts.path.split("/") if part]
                if len(segments) == 2 and segments[0] == "docs" and segments[1] in _SITE_DOC_FILES:
                    targets.add((_SITE_DOC_FILES[segments[1]], parts.fragment))
            elif not parts.scheme and not parts.netloc:
                name = Path(parts.path).name if parts.path else doc
                targets.add((name, parts.fragment))
    return targets


def _formatting_problems(lines: list[str], start: int, end: int) -> list[str]:
    """Return block-formatting problems of the callout on ``lines[start:end + 1]``.

    ``start`` is the ``<summary>`` line index (the details line sits before
    it) and ``end`` the ``</details>`` line index.
    """
    problems: list[str] = []
    details = start - 1
    if details < 0 or lines[details] != _DETAILS:
        problems.append(f"put '{_DETAILS}' at column 0 on the line before the summary")
        return problems
    if details > 0 and lines[details - 1].strip():
        problems.append(f"leave a blank line before '{_DETAILS}'")
    if start + 1 >= end or lines[start + 1].strip():
        problems.append("leave a blank line after the summary")
    if end - 1 > start and lines[end - 1].strip():
        problems.append("leave a blank line before '</details>'")
    if lines[end] != "</details>":
        problems.append("put '</details>' at column 0")
    if end + 1 < len(lines) and lines[end + 1].strip():
        problems.append("leave a blank line after '</details>'")
    return problems


def callout_problems(
    text: str, doc: str, other_labels: frozenset[str] | set[str] = OTHER_LABELS
) -> list[str]:
    """Return one message per variant-callout defect in ``text``, the content of ``docs/user/<doc>``."""
    lines = text.splitlines()
    problems: list[str] = []
    fence: str | None = None
    for index, line in enumerate(lines):
        fence_match = _FENCE.match(line)
        if fence:
            # Only a bare fence line closes a block: a fence with an info string opens none.
            closes = (
                fence_match
                and fence_match.group(1).startswith(fence)
                and not fence_match.group(2).strip()
            )
            fence = None if closes else fence
            continue
        if fence_match:
            fence = fence_match.group(1)
            continue
        summary = _SUMMARY.match(line)
        if not summary:
            if "<summary" in line:
                problems.append(
                    f"docs/user/{doc}:{index + 1}: put '<summary>label</summary>' alone on its line"
                )
            continue
        label = summary.group(1).strip()
        where = f"docs/user/{doc}:{index + 1}"
        if label not in _VARIANTS:
            if label not in other_labels:
                expected = _expected_label(label)
                hint = (
                    f"use '{expected}'"
                    if expected
                    else "use one of the four variant labels or add it to OTHER_LABELS"
                )
                problems.append(f"{where}: summary '{label}' is not a fixed variant label; {hint}")
            continue
        end = next(
            (n for n in range(index + 1, len(lines)) if lines[n].strip() == "</details>"), None
        )
        if end is None:
            problems.append(f"{where}: '{label}' callout has no closing '</details>'")
            continue
        for problem in _formatting_problems(lines, index, end):
            problems.append(f"{where}: '{label}' callout: {problem}")
        target_doc, anchor = _VARIANTS[label]
        links = _link_targets("\n".join(lines[index + 1 : end]), doc)
        if not any(
            file == target_doc and (not anchor or fragment == anchor) for file, fragment in links
        ):
            suffix = f"#{anchor}" if anchor else ""
            problems.append(f"{where}: '{label}' callout must link {target_doc}{suffix}")
    return problems


def _callout(label: str, body: str, *, indent: str = "") -> str:
    """Return a well-formed callout document around ``body``."""
    return (
        f"Baseline text.\n\n{indent}{_DETAILS}\n{indent}<summary>{label}</summary>\n\n"
        f"{body}\n\n</details>\n\nMore text.\n"
    )


_LINKS = {
    "In stealth mode": "[Stealth Mode](STEALTH.md)",
    "On Windows": "[Windows](WINDOWS.md#every-file-shows-as-modified-inside-the-container)",
    "With a cocotb testbench": "[Cocotb Targets](TARGETS.md#cocotb-targets-python-testbenches)",
    "With host-installed licensed tools": "[Commercial EDA](SANDBOX.md#commercial-eda-provisioning)",
}


@pytest.mark.parametrize("label", list(_VARIANTS))
def test_well_formed_callout_passes(label: str) -> None:
    assert callout_problems(_callout(label, f"Text. {_LINKS[label]}"), "X.md") == []


@pytest.mark.parametrize(
    "link",
    [
        "[Commercial EDA](SANDBOX.md#commercial-eda-provisioning)",
        "[Commercial EDA](https://booley.dev/docs/sandbox/#commercial-eda-provisioning)",
    ],
)
def test_variant_link_forms_resolve(link: str) -> None:
    text = _callout("With host-installed licensed tools", f"Text. {link}")
    assert callout_problems(text, "X.md") == []


def test_same_page_anchor_counts_only_inside_the_variant_page() -> None:
    text = _callout("With host-installed licensed tools", "[Here](#commercial-eda-provisioning)")
    assert callout_problems(text, "SANDBOX.md") == []
    [problem] = callout_problems(text, "X.md")
    assert "must link SANDBOX.md#commercial-eda-provisioning" in problem


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("On windows", "On Windows"),
        ("On Windos", "On Windows"),
        ("In Stealth Mode", "In stealth mode"),
        ("Cocotb", "With a cocotb testbench"),
        ("With licensed tools", "With host-installed licensed tools"),
    ],
)
def test_non_canonical_variant_label_fails(label: str, expected: str) -> None:
    problems = callout_problems(_callout(label, "Text."), "X.md")
    assert problems == [
        f"docs/user/X.md:4: summary '{label}' is not a fixed variant label; use '{expected}'"
    ]


def test_unknown_label_fails_unless_allowlisted() -> None:
    text = _callout("Reproduce", "Steps.")
    [problem] = callout_problems(text, "X.md")
    assert "use one of the four variant labels or add it to OTHER_LABELS" in problem
    assert callout_problems(text, "X.md", other_labels={"Reproduce"}) == []


def test_callout_without_variant_link_fails() -> None:
    [problem] = callout_problems(_callout("On Windows", "No link here."), "X.md")
    assert problem == "docs/user/X.md:4: 'On Windows' callout must link WINDOWS.md"


def test_callout_linking_another_variant_fails() -> None:
    body = "See [Stealth Mode](STEALTH.md) and [site](https://example.com/WINDOWS.md)."
    [problem] = callout_problems(_callout("On Windows", body), "X.md")
    assert "must link WINDOWS.md" in problem


def test_cocotb_callout_needs_the_cocotb_section_anchor() -> None:
    body = "[Targets](TARGETS.md#fpga-target)"
    [problem] = callout_problems(_callout("With a cocotb testbench", body), "X.md")
    assert "must link TARGETS.md#cocotb-targets-python-testbenches" in problem


def test_indented_callout_fails() -> None:
    text = _callout("On Windows", f"Text. {_LINKS['On Windows']}", indent="    ")
    [problem] = callout_problems(text, "X.md")
    assert "put '<details markdown>' at column 0" in problem


def test_callout_without_blank_lines_fails() -> None:
    text = (
        "Baseline.\n\n<details markdown>\n<summary>On Windows</summary>\n"
        f"Text. {_LINKS['On Windows']}\n</details>\n\nMore.\n"
    )
    problems = callout_problems(text, "X.md")
    assert any("blank line after the summary" in problem for problem in problems)
    assert any("blank line before '</details>'" in problem for problem in problems)


def test_callout_without_outer_blank_lines_fails() -> None:
    body = f"Text. {_LINKS['On Windows']}"
    text = (
        "Baseline.\n<details markdown>\n<summary>On Windows</summary>\n\n"
        f"{body}\n\n</details>\nMore.\n"
    )
    problems = callout_problems(text, "X.md")
    assert any("blank line before '<details markdown>'" in problem for problem in problems)
    assert any("blank line after '</details>'" in problem for problem in problems)


def test_unclosed_callout_fails() -> None:
    text = "Baseline.\n\n<details markdown>\n<summary>On Windows</summary>\n\nText.\n"
    [problem] = callout_problems(text, "X.md")
    assert "no closing '</details>'" in problem


def test_callout_example_inside_a_code_fence_is_ignored() -> None:
    text = "```markdown\n<details markdown>\n<summary>On windows</summary>\n```\n"
    assert callout_problems(text, "X.md") == []


def test_fence_with_info_string_does_not_end_a_code_block() -> None:
    text = "```markdown\n```text\n<summary>On windows</summary>\n```\n"
    assert callout_problems(text, "X.md") == []


@pytest.mark.parametrize(
    "line",
    [
        "<details markdown><summary>On windows</summary>",
        '<summary class="x">On windows</summary>',
    ],
)
def test_summary_not_alone_on_its_line_fails(line: str) -> None:
    [problem] = callout_problems(f"Baseline.\n\n{line}\n", "X.md")
    assert problem == "docs/user/X.md:3: put '<summary>label</summary>' alone on its line"


@pytest.mark.parametrize("doc", sorted(USER_DOC_DIR.glob("*.md")), ids=lambda path: path.name)
def test_user_doc_variant_callouts(doc: Path) -> None:
    assert callout_problems(doc.read_text(encoding="utf-8"), doc.name) == []

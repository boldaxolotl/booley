"""docs/user/ Markdown renders the same on GitHub and on the project site.

The site (website/hooks/user_docs.py, ADR 0076) renders the user docs with
Python-Markdown, which needs list-item block content indented four spaces;
GitHub accepts less. The hook also rewrites links so they work on the site.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path, PurePosixPath

import pytest

ROOT = Path(__file__).resolve().parents[2]
_MARKER = re.compile(r"^( *)(?:[-*+]|\d+[.)]) +\S")
_FENCE = re.compile(r"^ *(```+|~~~+)")


def _shallow_list_blocks(text: str) -> list[int]:
    """Return line numbers of list-item blocks indented under 4 spaces past their marker.

    A block starts after a blank line, or is a fence, table, quote, or nested
    list. Wrapped paragraph lines are lazy continuations and may sit anywhere.
    """
    offenders: list[int] = []
    markers: list[int] = []  # indents of the open list items, innermost last
    fence = None
    after_blank = True
    for number, line in enumerate(text.splitlines(), 1):
        fence_match = _FENCE.match(line)
        if fence:
            fence = None if fence_match and fence_match.group(1).startswith(fence) else fence
            continue
        if not line.strip():
            after_blank = True
            continue
        indent = len(line) - len(line.lstrip(" "))
        marker = _MARKER.match(line)
        block = after_blank or marker or fence_match or line.lstrip()[0] in "|>"
        while block and markers and indent <= markers[-1]:
            markers.pop()
        if block and markers and indent < markers[-1] + 4:
            offenders.append(number)
        if marker:
            markers.append(indent)
        if fence_match:
            fence = fence_match.group(1)
        after_blank = False
    return offenders


def test_detector_flags_shallow_blocks_only() -> None:
    text = (
        "1. step\n"
        "   wrapped line\n"  # lazy continuation: fine
        "\n"
        "   ```bash\n"  # line 4: fence 3 spaces under an ordered item
        "   echo\n"
        "   ```\n"
        "- item\n"
        "    | a |\n"  # 4 spaces: fine
        "  - nested\n"  # line 9: nested list 2 spaces under a bullet
    )

    assert _shallow_list_blocks(text) == [4, 9]


@pytest.mark.parametrize("doc", sorted((ROOT / "docs/user").glob("*.md")), ids=lambda p: p.name)
def test_user_doc_list_blocks_are_indented_four_spaces(doc: Path) -> None:
    assert _shallow_list_blocks(doc.read_text(encoding="utf-8")) == []


def _hook():
    spec = importlib.util.spec_from_file_location(
        "site_user_docs_hook", ROOT / "website/hooks/user_docs.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("CONFIG.md#tests-teststoml", "CONFIG.md#tests-teststoml"),
        ("../internals/WHY.md#why-docker", "{blob}docs/internals/WHY.md#why-docker"),
        ("../../README.md", "{blob}README.md"),
        ("https://github.com/boldaxolotl/Booley/blob/main/docs/user/USAGE.md#a", "USAGE.md#a"),
        ("https://boldaxolotl.github.io/booley/docs/usage/#a", "USAGE.md#a"),
        ("https://boldaxolotl.github.io/booley/docs/assets/x.png", "assets/x.png"),
        ("https://boldaxolotl.github.io/booley/#limitations", None),  # not a user doc
    ],
)
def test_hook_points_doc_links_where_they_work_on_the_site(
    target: str, expected: str | None
) -> None:
    hook = _hook()
    expected = (expected or target).format(blob=hook.GITHUB_BLOB)

    assert hook.rewrite_links(f"[x]({target})", PurePosixPath("docs/user")) == f"[x]({expected})"


def test_hook_leaves_fenced_code_alone() -> None:
    fenced = "```\n[w](../internals/WHY.md)\n```"

    assert _hook().rewrite_links(fenced, PurePosixPath("docs/user")) == fenced


def test_hook_rejects_site_links_to_unpublished_docs() -> None:
    with pytest.raises(ValueError, match="no published user doc"):
        _hook().rewrite_links("[x](https://boldaxolotl.github.io/booley/docs/nope/)", None)

"""Agent-facing text cites only docs the Sandbox ships, or marks them host-side (issue #2495).

Skills, the cheatsheet, ``data/refs`` and generated config comments are read in the
Sandbox, where a repository checkout does not exist. A doc citation there must be
``/booley-docs/<FILE>`` (a shipped doc, with its section heading when it names one) or
carry the ``(host doc, not shipped)`` marker.
"""

from __future__ import annotations

import ast
import re
from functools import cache
from pathlib import Path

import pytest
from markdown_it import MarkdownIt
from tests.docs.test_links import _text_anchors
from tests.shipped_docs_support import rendered_shipped_docs

from booley.harness.init_cmd import BOOLEY_TOML_SKELETON, TESTS_TOML_SKELETON
from booley.harness.setup.scaffold import ScaffoldChoices, _booley_toml
from booley.runtime import docs_links, packaged_docs

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "src/booley/data"
SKILLS = DATA / "skills"
# Python modules whose string literals are agent-facing runtime messages.
MESSAGE_MODULES = (
    ROOT / "src/booley/specialists/review_contract.py",
    ROOT / "src/booley/runtime/project_worktree_pairing.py",
)

_FIXED_PATH = re.compile(r"/booley-docs/(?P<name>[\w-]+\.md)(?:#(?P<anchor>[\w-]+))?")
_SECTION = re.compile(r"""[`*_)\]]*\s*→\s*(?:"(?P<a>[^"]+)"|“(?P<b>[^”]+)”|`(?P<c>[^`]+)`)""")
_ARROW = re.compile(r"[`*_)\]]*\s*→")
_SITE_URL = re.compile(r"booley\.dev/docs/(?P<slug>[a-z0-9-]+)/?(?:#[\w-]*)?")
_ONE_LINERS = re.compile(r"troubleshooting_path|package_data_dir")
# How far back a site URL may look for the fixed-path citation it is the host location of.
_HOST_LOCATION_WINDOW = 200


def _doc_file_names() -> set[str]:
    """Return every user-doc file name, shipped or not (a bare mention is a citation)."""
    names = {doc.file for doc in docs_links.USER_DOCS}
    names |= {Path(source).name for source in packaged_docs.SHIPPED_DOCS}
    names |= {Path(target).name for target in packaged_docs.PLAIN_TEXT_TARGETS}
    return names


@cache
def _rendered_doc(name: str) -> str:
    """Return one shipped doc, by packaged file name, as the build renders it from its source.

    The packaged directory is Git-ignored build output, so it is never read (#3658).
    """
    return next(
        text for source, text in rendered_shipped_docs(ROOT).items() if Path(source).name == name
    )


def _headings(name: str) -> list[str]:
    """Return the lower-cased heading texts of one packaged doc (fenced ``#`` lines excluded)."""
    tokens = MarkdownIt().parse(_rendered_doc(name))
    return [
        "".join(child.content for child in tokens[index + 1].children or []).lower()
        for index, token in enumerate(tokens)
        if token.type == "heading_open"
    ]


def _is_marked(after: str) -> bool:
    """Return whether the text right after a citation carries the host-side marker."""
    marker = re.escape(packaged_docs.NOT_SHIPPED_MARKER)
    pattern = (
        r"""^[`*_)\]"'”\u2019.,;:\s]*(?:→\s*(?:"[^"]*"|“[^”]*”|`[^`]*`)[`*_)\]\s.,;:]*)?"""
        + marker
    )
    return re.match(pattern, after) is not None


def _fixed_path_problems(text: str, label: str) -> list[str]:
    problems: list[str] = []
    shipped = {Path(source).name for source in packaged_docs.SHIPPED_DOCS}
    for match in _FIXED_PATH.finditer(text):
        name, anchor = match["name"], match["anchor"]
        cite = f"{label}: {match.group(0)}"
        if name not in shipped:
            problems.append(f"{cite} is not a shipped doc")
            continue
        if anchor and anchor not in _text_anchors(_rendered_doc(name)):
            problems.append(f"{cite} has no such anchor")
        after = text[match.end() :]
        if section := _SECTION.match(after):
            wanted = (section["a"] or section["b"] or section["c"]).lower().replace("`", "")
            if not any(wanted in heading for heading in _headings(name)):
                problems.append(f'{cite} → "{wanted}" matches no heading')
        elif _ARROW.match(after):
            problems.append(f'{cite} → needs a quoted section: → "Heading"')
    return problems


def _bare_name_problems(text: str, label: str) -> list[str]:
    """Flag unmarked bare names and repo paths (``../docs/user/X.md``, GitHub URLs, any X)."""
    names = "|".join(re.escape(name) for name in sorted(_doc_file_names()))
    bare = re.compile(rf"(?<![\w/.-])(?:docs/(?:user|internals)/)?(?P<name>{names})(?![\w-])")
    # A repo path may follow a slash (a relative prefix, a GitHub URL) and name any doc.
    repo_path = re.compile(r"(?<![\w-])docs/(?:user|internals)/[\w-]+\.md")
    matches = [*bare.finditer(text), *repo_path.finditer(text)]
    problems = [
        f"{label}: {match.group(0)} is not cited as /booley-docs/<FILE> or marked "
        f"{packaged_docs.NOT_SHIPPED_MARKER}"
        for match in matches
        if not _is_marked(text[match.end() :])
    ]
    return list(dict.fromkeys(problems))


def _site_url_problems(text: str, label: str) -> list[str]:
    problems: list[str] = []
    slug_to_file = {doc.slug: doc.file for doc in docs_links.USER_DOCS}
    shipped = {Path(source).name for source in packaged_docs.SHIPPED_DOCS}
    for match in _SITE_URL.finditer(text):
        file = slug_to_file.get(match["slug"])
        window = text[max(0, match.start() - _HOST_LOCATION_WINDOW) : match.start()]
        host_location = file in shipped and f"/booley-docs/{file}" in window
        if not host_location and not _is_marked(text[match.end() :]):
            problems.append(
                f"{label}: {match.group(0)} needs {packaged_docs.NOT_SHIPPED_MARKER}, "
                "or the /booley-docs/<FILE> citation of the same shipped doc just before it"
            )
    return problems


def citation_problems(text: str, label: str, *, skill: bool = False) -> list[str]:
    """Return one line per doc citation in ``text`` that breaks the Sandbox citation rule."""
    normalized = " ".join(text.split())
    problems = [
        *_fixed_path_problems(normalized, label),
        *_bare_name_problems(normalized, label),
        *_site_url_problems(normalized, label),
    ]
    if skill and _ONE_LINERS.search(normalized):
        problems.append(
            f"{label}: locate docs at /booley-docs/<FILE>, not with a python one-liner"
        )
    return problems


def _string_literals(module: Path) -> list[str]:
    tree = ast.parse(module.read_text(encoding="utf-8"))
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]


def _scanned_texts() -> list[tuple[str, str, bool]]:
    """Return ``(label, text, is_skill)`` for every agent-facing text the rule covers."""
    texts: list[tuple[str, str, bool]] = []
    for path in sorted(SKILLS.rglob("*")):
        if path.is_file():
            try:
                texts.append((str(path.relative_to(DATA)), path.read_text(encoding="utf-8"), True))
            except UnicodeDecodeError:
                continue  # binary asset
    texts.append(("cheatsheet.md", (DATA / "cheatsheet.md").read_text(encoding="utf-8"), False))
    texts.extend(
        (str(path.relative_to(DATA)), path.read_text(encoding="utf-8"), False)
        for path in sorted((DATA / "refs").rglob("*.md"))
        if path.name != "CHANGELOG.md"
    )
    scaffold = _booley_toml(ScaffoldChoices("my_ip", "verilator", "sv", "verilator", True, None))
    texts.extend(
        [
            ("BOOLEY_TOML_SKELETON", BOOLEY_TOML_SKELETON, False),
            ("TESTS_TOML_SKELETON", TESTS_TOML_SKELETON, False),
            ("scaffold booley.toml", scaffold, False),
        ]
    )
    for module in MESSAGE_MODULES:
        texts.extend(
            (f"{module.name} literal", literal, False) for literal in _string_literals(module)
        )
    return texts


def test_agent_text_cites_shipped_docs_or_host_marker() -> None:
    texts = _scanned_texts()
    labels = " ".join(label for label, _, _ in texts)
    for expected in ("SKILL.md", "cheatsheet.md", "BOOLEY_TOML_SKELETON", "scaffold booley.toml"):
        assert expected in labels, f"the citation scan skipped {expected}"
    problems = [
        problem
        for label, text, skill in texts
        for problem in citation_problems(text, label, skill=skill)
    ]
    assert not problems, "\n".join(problems)


def test_guard_rejects_unmarked_unshipped_citation_and_missing_section() -> None:
    rejected = [
        ("see docs/user/SETUP.md for details", "SETUP.md is not cited"),
        ("see SETUP.md next", "SETUP.md is not cited"),
        ("see docs/internals/ARCHITECTURE.md", "docs/internals/ARCHITECTURE.md is not cited"),
        ("see ../docs/user/SETUP.md", "docs/user/SETUP.md is not cited"),
        (
            "see https://github.com/boldaxolotl/booley/blob/main/docs/user/SETUP.md",
            "docs/user/SETUP.md is not cited",
        ),
        ("the CONFIG.md file", "CONFIG.md is not cited"),
        ("see /booley-docs/NOPE.md", "is not a shipped doc"),
        ("see /booley-docs/CONFIG.md#no-such-anchor", "has no such anchor"),
        ('see /booley-docs/CONFIG.md → "No Such Heading"', "matches no heading"),
        ("see /booley-docs/CONFIG.md → protected_inputs", "needs a quoted section"),
        ("read https://booley.dev/docs/setup/ first", "needs (host doc, not shipped)"),
        ("read https://booley.dev/docs/config/ alone", "needs (host doc, not shipped)"),
        ("run troubleshooting_path()", "python one-liner"),
    ]
    for text, fragment in rejected:
        problems = citation_problems(text, "probe", skill=True)
        assert any(fragment in problem for problem in problems), (text, problems)


@pytest.mark.parametrize(
    "text",
    [
        "see SETUP.md (host doc, not shipped).",
        "see `docs/user/SETUP.md` (host doc, not shipped)",
        "the install guide at https://booley.dev/docs/install/ (host doc, not shipped),",
        '`SETUP.md` → "First run" (host doc, not shipped)',
        'see `/booley-docs/CONFIG.md` → "Additional Protected Inputs".',
        "see /booley-docs/CONFIG.md → “Feedback”",
        "see /booley-docs/CONFIG.md#additional-protected-inputs-projectprotected_inputs",
        "`/booley-docs/CONFIG.md` (online: https://booley.dev/docs/config/)",
        "the docs site https://booley.dev/docs/ has one page per doc",
        "CONFIG.md-like names in TESTS_TEMPLATE.toml and ../SETUP-PLAN.md are not doc names",
    ],
)
def test_guard_accepts_shipped_citations_and_marked_host_docs(text: str) -> None:
    assert citation_problems(text, "probe", skill=True) == []


def test_setup_step0_and_booley_toml_cite_the_fixed_path() -> None:
    fixed = docs_links.sandbox_doc_path
    setup = (SKILLS / "booley-setup/SKILL.md").read_text(encoding="utf-8")
    step0 = (SKILLS / "booley-setup/steps/0-plan.md").read_text(encoding="utf-8")
    assert fixed("SUPPORTED-EDA-TOOLS.md") in step0
    assert "docs/user/" not in step0
    assert fixed("CONFIG.md") in setup
    assert fixed("FLOW-SIM.md") in BOOLEY_TOML_SKELETON
    scaffold = _booley_toml(ScaffoldChoices("my_ip", "verilator", "sv", "verilator", True, None))
    assert f"# Knob reference: {fixed('CONFIG.md')}" in scaffold
    for skill in ("booley-heal", "booley-goal"):
        text = (SKILLS / skill / "SKILL.md").read_text(encoding="utf-8")
        assert fixed("TROUBLESHOOTING.md") in text, skill

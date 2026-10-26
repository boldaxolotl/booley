"""Repository-local links in published Markdown resolve to files and anchors."""

from __future__ import annotations

import hashlib
import json
import subprocess
from functools import cache
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest
from markdown_it import MarkdownIt
from markdown_it.token import Token

from booley.runtime.docs_links import SITE_URL, USER_DOCS, github_anchor

REPO_ROOT = Path(__file__).resolve().parents[2]
GITHUB_REPOSITORY_PATH = "/boldaxolotl/Booley"
GITHUB_BLOB_PREFIX = "/boldaxolotl/Booley/blob/main/"
SITE = urlsplit(SITE_URL)
SITE_DOCS_PREFIX = f"{SITE.path}docs/"
USER_DOC_FILES = {doc.slug: doc.file for doc in USER_DOCS}


def _frozen_upstream_documents() -> set[Path]:
    """Pinned upstream text retains upstream links; never fetch its linked RTL."""
    spec = REPO_ROOT / "qa/missions/uart/spec"
    manifest = json.loads((spec / "corpus-manifest.json").read_text(encoding="utf-8"))
    documents = set()
    for entry in manifest["files"]:
        path = _repository_target(spec / entry["path"], entry["path"])
        assert hashlib.sha256(path.read_bytes()).hexdigest() == entry["sha256"]
        if path.suffix == ".md":
            documents.add(path)
    return documents


def _markdown_files() -> list[Path]:
    """Return versioned and pending non-ignored Markdown, excluding worktrees."""
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "--", "*.md"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    frozen = _frozen_upstream_documents()
    return [
        REPO_ROOT / name
        for name in result.stdout.splitlines()
        if not name.startswith(".worktrees/")
        and (REPO_ROOT / name).is_file()
        and (REPO_ROOT / name).resolve() not in frozen
    ]


def _destinations(document: Path) -> list[str]:
    tokens = MarkdownIt().parse(document.read_text(encoding="utf-8"))
    destinations: list[str] = []
    for token in tokens:
        for child in token.children or []:
            if child.type == "link_open":
                destinations.append(child.attrGet("href") or "")
            elif child.type == "image":
                destinations.append(child.attrGet("src") or "")
    return destinations


def _code_spans(document: Path) -> list[str]:
    tokens = MarkdownIt().parse(document.read_text(encoding="utf-8"))
    return [
        child.content
        for token in tokens
        for child in token.children or []
        if child.type == "code_inline"
    ]


def _repository_target(target: Path, destination: str) -> Path:
    """Resolve one target and reject links that escape the repository."""
    resolved = target.resolve()
    if resolved == REPO_ROOT or REPO_ROOT in resolved.parents:
        return resolved
    raise ValueError(f"destination escapes repository: {destination}")


def _site_doc_path(site_path: str) -> Path | None:
    """Map a project-site URL path under ``docs/`` to its source in ``docs/user/``."""
    if not site_path.startswith(SITE_DOCS_PREFIX):
        return None  # home page, videos, and other site-only pages
    page = unquote(site_path.removeprefix(SITE_DOCS_PREFIX))
    if page.startswith("assets/"):
        return REPO_ROOT / "docs/user" / page
    slug = page.strip("/")
    if slug not in USER_DOC_FILES:
        raise ValueError(f"unknown user-doc slug: {slug}")
    return REPO_ROOT / "docs/user" / USER_DOC_FILES[slug]


def _web_path(netloc: str, path: str) -> Path | None:
    """Map a project-site or GitHub blob URL to its repository source, if any."""
    if netloc == SITE.netloc:
        return _site_doc_path(path)
    if netloc != "github.com":
        return None
    if path.rstrip("/") == GITHUB_REPOSITORY_PATH:
        return REPO_ROOT / "README.md"
    if path.startswith(GITHUB_BLOB_PREFIX):
        return REPO_ROOT / unquote(path.removeprefix(GITHUB_BLOB_PREFIX))
    return None


def _repository_path(document: Path, destination: str) -> Path | None:
    parsed = urlsplit(destination)
    if parsed.scheme in {"http", "https"}:
        target = _web_path(parsed.netloc.casefold(), parsed.path)
        return None if target is None else _repository_target(target, destination)
    if parsed.scheme or parsed.netloc or parsed.path.startswith("/"):
        return None
    if not parsed.path:
        return document if parsed.fragment else None
    target = document.parent / unquote(parsed.path)
    return _repository_target(target, destination)


def _heading_text(token: Token) -> str:
    """Return rendered text from a Markdown heading's inline token."""
    return "".join(child.content for child in token.children or [] if child.type != "html_inline")


class _HtmlAnchors(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.anchors: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for name, value in attrs:
            if name == "id" and value:
                self.anchors.add(value)


def _html_anchors(tokens: list[Token]) -> set[str]:
    parser = _HtmlAnchors()
    for token in tokens:
        for child in (token, *(token.children or [])):
            if child.type in {"html_block", "html_inline"}:
                parser.feed(child.content)
    parser.close()
    return parser.anchors


@cache
def _markdown_anchors(document: Path) -> frozenset[str]:
    """Return HTML IDs and GitHub-style headings, including duplicate suffixes."""
    tokens = MarkdownIt().parse(document.read_text(encoding="utf-8"))
    anchors: set[str] = set()
    for index, token in enumerate(tokens[:-1]):
        if token.type != "heading_open":
            continue
        base = github_anchor(_heading_text(tokens[index + 1]))
        anchor = base
        suffix = 0
        while anchor in anchors:
            suffix += 1
            anchor = f"{base}-{suffix}"
        if anchor:
            anchors.add(anchor)
    return frozenset(anchors | _html_anchors(tokens))


def _destination_failure(target: Path, destination: str) -> str | None:
    if not target.exists():
        return str(target)
    fragment = unquote(urlsplit(destination).fragment)
    if (
        fragment
        and target.suffix.casefold() == ".md"
        and fragment not in _markdown_anchors(target)
    ):
        return f"{target} has no anchor #{fragment}"
    return None


@pytest.mark.parametrize(
    "destination",
    [
        "../outside.md",
        "https://github.com/boldaxolotl/Booley/blob/main/%2E%2E/outside.md",
        f"{SITE_URL}docs/assets/%2E%2E/%2E%2E/%2E%2E/%2E%2E/outside.md",
    ],
)
def test_repository_paths_cannot_escape(destination: str) -> None:
    with pytest.raises(ValueError, match="destination escapes repository"):
        _repository_path(REPO_ROOT / "README.md", destination)


@pytest.mark.parametrize(
    ("destination", "target"),
    [
        (f"{SITE_URL}docs/setup/#the-booley-setup-skill", "docs/user/SETUP.md"),
        (f"{SITE_URL}docs/assets/booley-screenshot.png", "docs/user/assets/booley-screenshot.png"),
        (f"{SITE_URL}videos/", None),
    ],
)
def test_site_urls_resolve_to_user_doc_sources(destination: str, target: str | None) -> None:
    resolved = _repository_path(REPO_ROOT / "README.md", destination)
    assert resolved == (None if target is None else REPO_ROOT / target)


def test_unknown_site_doc_slug_fails() -> None:
    with pytest.raises(ValueError, match="unknown user-doc slug: nope"):
        _repository_path(REPO_ROOT / "README.md", f"{SITE_URL}docs/nope/")


def test_heading_anchors_follow_github_ids(tmp_path: Path) -> None:
    document = tmp_path / "headings.md"
    document.write_text(
        "# Auth & billing\n"
        "## How Booley asks for a waveform (`[flows.sim].trace_args`)\n"
        "## Repeated\n"
        "## Repeated\n",
        encoding="utf-8",
    )
    assert _markdown_anchors(document) == {
        "auth--billing",
        "how-booley-asks-for-a-waveform-flowssimtrace_args",
        "repeated",
        "repeated-1",
    }


@pytest.mark.parametrize(
    ("markup", "resolves"),
    [
        ('<details id="reproduce" markdown="1">\n<summary>Reproduce</summary>\n</details>', True),
        ('Text <a id="reproduce"></a>.', True),
        ('```html\n<a id="reproduce"></a>\n```', False),
        ('`<a id="reproduce"></a>`', False),
    ],
)
def test_html_anchor_links_ignore_code_examples(
    tmp_path: Path, markup: str, resolves: bool
) -> None:
    document = tmp_path / "anchors.md"
    document.write_text(markup + "\n", encoding="utf-8")

    failure = _destination_failure(document, "#reproduce")

    assert failure == (None if resolves else f"{document} has no anchor #reproduce")


def test_repository_local_markdown_links_resolve() -> None:
    failures: list[str] = []
    for document in _markdown_files():
        source = document.relative_to(REPO_ROOT)
        for destination in _destinations(document):
            try:
                target = _repository_path(document, destination)
            except ValueError as exc:
                failures.append(f"{source}: {destination} -> {exc}")
                continue
            if target is not None and (reason := _destination_failure(target, destination)):
                failures.append(f"{source}: {destination} -> {reason}")
    assert not failures, "broken repository-local links:\n" + "\n".join(failures)


def test_only_frozen_upstream_markdown_is_excluded() -> None:
    documents = set(_markdown_files())
    assert not documents.intersection(_frozen_upstream_documents())
    assert REPO_ROOT / "qa/missions/uart/evaluator/CONTRACT.md" in documents
    assert REPO_ROOT / "qa/missions/uart/spec/timing-addendum.md" in documents
    assert len(_frozen_upstream_documents()) == 5


def test_agent_instruction_document_paths_resolve() -> None:
    agents = REPO_ROOT / "AGENTS.md"
    doc_paths = [
        REPO_ROOT / span
        for span in _code_spans(agents)
        if span.startswith("docs/") and span.endswith(".md")
    ]
    missing = [path.relative_to(REPO_ROOT) for path in doc_paths if not path.is_file()]
    assert not missing, f"missing documents referenced by AGENTS.md: {missing}"


# The site moved from GitHub's project-site host to its own domain. GitHub
# redirects the old URLs, but new text must name SITE_URL so that links do not
# depend on that redirect. These files name the old host on purpose.
_RETIRED_SITE_HOST = "boldaxolotl.github.io"
_RETIRED_HOST_ALLOWED = {
    "CHANGELOG.md",  # release history records the move
    "src/booley/data/refs/CHANGELOG.md",  # packaged copy of the release history
    "docs/adr/0076-publish-user-docs-on-the-project-site.md",  # amendment records the move
    ".github/workflows/pages.yml",  # documents the redirect setting
    "tests/docs/test_links.py",  # this guard
}


def test_tracked_files_name_the_site_by_its_current_domain() -> None:
    """Old-host links pass the link check above (another host), so catch them here."""
    result = subprocess.run(
        ["git", "grep", "-l", "--fixed-strings", _RETIRED_SITE_HOST],
        cwd=REPO_ROOT,
        check=False,  # exit 1 means no match, checked below
        capture_output=True,
        text=True,
    )
    assert result.returncode in (0, 1), result.stderr
    stale = sorted(set(result.stdout.splitlines()) - _RETIRED_HOST_ALLOWED)
    assert not stale, f"replace {_RETIRED_SITE_HOST} links with {SITE_URL} in: {stale}"

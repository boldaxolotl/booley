"""Repository-local links in published Markdown resolve to files and anchors."""

from __future__ import annotations

import subprocess
from functools import cache
from pathlib import Path
from unicodedata import category
from urllib.parse import unquote, urlsplit

import pytest
from markdown_it import MarkdownIt
from markdown_it.token import Token

REPO_ROOT = Path(__file__).resolve().parents[2]
GITHUB_REPOSITORY_PATH = "/boldaxolotl/Booley"
GITHUB_BLOB_PREFIX = "/boldaxolotl/Booley/blob/main/"


def _markdown_files() -> list[Path]:
    """Return versioned and pending non-ignored Markdown, excluding worktrees."""
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "--", "*.md"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return [
        REPO_ROOT / name
        for name in result.stdout.splitlines()
        if not name.startswith(".worktrees/") and (REPO_ROOT / name).is_file()
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


def _repository_path(document: Path, destination: str) -> Path | None:
    parsed = urlsplit(destination)
    if parsed.scheme in {"http", "https"}:
        if parsed.netloc.casefold() != "github.com":
            return None
        if parsed.path.rstrip("/") == GITHUB_REPOSITORY_PATH:
            target = REPO_ROOT / "README.md"
        elif parsed.path.startswith(GITHUB_BLOB_PREFIX):
            target = REPO_ROOT / unquote(parsed.path.removeprefix(GITHUB_BLOB_PREFIX))
        else:
            return None
        return _repository_target(target, destination)
    if parsed.scheme or parsed.netloc or parsed.path.startswith("/"):
        return None
    if not parsed.path:
        return document if parsed.fragment else None
    target = document.parent / unquote(parsed.path)
    return _repository_target(target, destination)


def _heading_text(token: Token) -> str:
    """Return rendered text from a Markdown heading's inline token."""
    return "".join(child.content for child in token.children or [] if child.type != "html_inline")


def _github_slug(text: str) -> str:
    """Approximate GitHub's heading-ID rules for the syntax used in this repo."""
    return "".join(
        "-" if char.isspace() else char
        for char in text.strip().lower()
        if char in {"-", "_"} or char.isspace() or category(char)[0] in {"L", "M", "N"}
    )


@cache
def _markdown_anchors(document: Path) -> frozenset[str]:
    """Return GitHub-style heading anchors, including duplicate suffixes."""
    tokens = MarkdownIt().parse(document.read_text(encoding="utf-8"))
    anchors: set[str] = set()
    for index, token in enumerate(tokens[:-1]):
        if token.type != "heading_open":
            continue
        base = _github_slug(_heading_text(tokens[index + 1]))
        anchor = base
        suffix = 0
        while anchor in anchors:
            suffix += 1
            anchor = f"{base}-{suffix}"
        if anchor:
            anchors.add(anchor)
    return frozenset(anchors)


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
    ],
)
def test_repository_paths_cannot_escape(destination: str) -> None:
    with pytest.raises(ValueError, match="destination escapes repository"):
        _repository_path(REPO_ROOT / "README.md", destination)


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


def test_agent_instruction_document_paths_resolve() -> None:
    agents = REPO_ROOT / "AGENTS.md"
    doc_paths = [
        REPO_ROOT / span
        for span in _code_spans(agents)
        if span.startswith("docs/") and span.endswith(".md")
    ]
    missing = [path.relative_to(REPO_ROOT) for path in doc_paths if not path.is_file()]
    assert not missing, f"missing documents referenced by AGENTS.md: {missing}"

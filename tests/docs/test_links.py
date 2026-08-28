"""Repository-local links in published Markdown resolve to real files."""

from __future__ import annotations

import subprocess
from pathlib import Path
from urllib.parse import unquote, urlsplit

from markdown_it import MarkdownIt

REPO_ROOT = Path(__file__).resolve().parents[2]
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


def _repository_path(document: Path, destination: str) -> Path | None:
    parsed = urlsplit(destination)
    if parsed.scheme in {"http", "https"}:
        if parsed.netloc != "github.com" or not parsed.path.startswith(GITHUB_BLOB_PREFIX):
            return None
        return REPO_ROOT / unquote(parsed.path.removeprefix(GITHUB_BLOB_PREFIX))
    if parsed.scheme or parsed.netloc or not parsed.path or parsed.path.startswith("/"):
        return None
    return (document.parent / unquote(parsed.path)).resolve()


def test_repository_local_markdown_links_resolve() -> None:
    failures: list[str] = []
    for document in _markdown_files():
        for destination in _destinations(document):
            target = _repository_path(document, destination)
            if target is not None and not target.exists():
                source = document.relative_to(REPO_ROOT)
                failures.append(f"{source}: {destination} -> {target}")
    assert not failures, "broken repository-local links:\n" + "\n".join(failures)

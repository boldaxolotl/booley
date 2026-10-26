"""Publish the repository's user docs (docs/user/) on the project site.

docs/user/*.md stays the only authored copy (ADR 0076). This MkDocs hook adds
each file to the build as a virtual page ``docs/<NAME>.md`` served at
``/docs/<slug>/``, so the relative links between the user docs keep working
and MkDocs validates their anchors. Strict-mode warnings therefore name the
virtual path: ``docs/USAGE.md`` means ``docs/user/USAGE.md``.

The slug table comes from src/booley/runtime/docs_links.py, the same table
Booley's printed links use. It is loaded by path because the site toolchain
does not install booley.

After the build, Pagefind indexes the site for the header search field.
"""

from __future__ import annotations

import html
import importlib.util
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

REPO = Path(__file__).resolve().parents[2]
USER_DOCS_DIR = REPO / "docs" / "user"
GITHUB_BLOB = "https://github.com/boldaxolotl/booley/blob/main/"


def _load_docs_links():
    path = REPO / "src" / "booley" / "runtime" / "docs_links.py"
    spec = importlib.util.spec_from_file_location("booley_docs_links", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load the docs slug table from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # NamedTuple needs its module importable by name
    spec.loader.exec_module(module)
    return module


docs_links = _load_docs_links()
_FILE_BY_SLUG = {doc.slug: doc.file for doc in docs_links.USER_DOCS}
# Inline Markdown link and image destinations: ](target) or ](<target>).
_LINK = re.compile(r"(\]\(\s*<?)([^)\s>]+)")
_FENCE = re.compile(r"^ *(```+|~~~+)")
_GITHUB_USER_DOCS = re.compile(
    r"^https://github\.com/boldaxolotl/booley/blob/main/docs/user/(?P<path>[^#?]+)",
    re.IGNORECASE,
)


# ------------------------------------------------------------ link rewriting
def _site_relative(url: str) -> str | None:
    """Map a public URL of a user doc (GitHub or site) to the virtual docs/ path."""
    parts = urlsplit(url)
    fragment = f"#{parts.fragment}" if parts.fragment else ""
    github = _GITHUB_USER_DOCS.match(url)
    if github:
        return github.group("path") + fragment
    site = urlsplit(docs_links.SITE_URL)
    if parts.netloc != site.netloc or not parts.path.startswith(f"{site.path}docs/"):
        return None
    rest = parts.path.removeprefix(f"{site.path}docs/")
    if rest.startswith("assets/"):
        return rest + fragment
    slug = rest.strip("/")
    if not slug:
        return None  # the landing page itself
    if slug not in _FILE_BY_SLUG:
        raise ValueError(f"no published user doc at {url}")
    return _FILE_BY_SLUG[slug] + fragment


def _rewrite_target(target: str, repo_dir: PurePosixPath | None) -> str:
    if "://" in target:
        return _site_relative(target) or target
    if repo_dir is None or target.startswith(("#", "mailto:")):
        return target
    path, hash_mark, fragment = target.partition("#")
    resolved = PurePosixPath(*_normalized_parts(repo_dir / path))
    if resolved.parts[:2] == ("docs", "user"):
        return target
    return f"{GITHUB_BLOB}{resolved}{hash_mark}{fragment}"


def _normalized_parts(path: PurePosixPath) -> list[str]:
    parts: list[str] = []
    for part in path.parts:
        if part == "..":
            if not parts:
                raise ValueError(f"link escapes the repository: {path}")
            parts.pop()
        elif part != ".":
            parts.append(part)
    return parts


def rewrite_links(markdown: str, repo_dir: PurePosixPath | None) -> str:
    """Point a docs page's links where they work on the site.

    - Public URLs of a user doc (GitHub blob or site) become relative virtual
      paths, so MkDocs checks the anchor and ``mkdocs serve`` stays local.
    - When ``repo_dir`` (the page's directory in the repository) is given,
      relative links leaving docs/user/ become GitHub URLs on ``main``.

    Fenced code blocks are left alone.
    """
    lines = markdown.split("\n")
    fence = None
    for index, line in enumerate(lines):
        match = _FENCE.match(line)
        if fence:
            fence = None if match and match.group(1).startswith(fence) else fence
            continue
        if match:
            fence = match.group(1)
            continue
        lines[index] = _LINK.sub(
            lambda m: m.group(1) + _rewrite_target(m.group(2), repo_dir), line
        )
    return "\n".join(lines)


# ------------------------------------------------------------- MkDocs events
def on_files(files, config):
    """Add docs/user/*.md as pages at docs/<slug>/ and docs/user/assets/ as files."""
    from mkdocs.structure.files import File

    for doc in docs_links.USER_DOCS:
        page = File.generated(
            config, f"docs/{doc.file}", abs_src_path=str(USER_DOCS_DIR / doc.file)
        )
        page.dest_uri = f"docs/{doc.slug}/index.html"
        files.append(page)
    for asset in sorted((USER_DOCS_DIR / "assets").iterdir()):
        files.append(File.generated(config, f"docs/assets/{asset.name}", abs_src_path=str(asset)))
    return files


def on_serve(server, config, builder):
    server.watch(str(USER_DOCS_DIR))
    server.watch(str(REPO / "src" / "booley" / "runtime" / "docs_links.py"))
    return server


def on_env(env, config, files):
    """Expose the docs reading order to the templates."""
    env.globals["user_docs"] = [
        {
            "title": doc.title,
            "section": doc.section,
            "child": doc.parent is not None,
            "url": f"docs/{doc.slug}/",
            "src_uri": f"docs/{doc.file}",
        }
        for doc in docs_links.USER_DOCS
    ]
    return env


def on_page_markdown(markdown, page, config, files):
    """Give docs pages their template, source link, and site-ready links."""
    src_uri = page.file.src_uri
    if not src_uri.startswith("docs/"):
        return markdown
    page.meta["template"] = "doc.html"
    page.meta["doc_section"] = True  # base.html marks the header's Docs link current
    name = src_uri.removeprefix("docs/")
    if name not in {doc.file for doc in docs_links.USER_DOCS}:
        return rewrite_links(markdown, None)  # the landing page: public URLs only
    page.meta["doc_source"] = f"{GITHUB_BLOB}docs/user/{name}"
    return rewrite_links(markdown, PurePosixPath("docs/user"))


def on_page_content(content, page, config, files):
    """Check heading IDs match GitHub's, and let wide tables scroll."""
    if page.meta.get("doc_source"):
        _check_github_anchors(page.toc, page.file.src_uri)
    if page.meta.get("doc_section"):
        content = content.replace("<table>", '<div class="doc-table"><table>')
        content = content.replace("</table>", "</table></div>")
    return content


def _check_github_anchors(toc, src_uri: str) -> None:
    """Fail the build when a site heading ID differs from GitHub's.

    Links into the docs (and printed docs_url anchors) must work on both. The
    usual cause is a repeated heading: GitHub suffixes ``-1``, the site ``_1``.
    """
    mismatches = []
    stack = list(toc)
    while stack:
        item = stack.pop()
        stack.extend(item.children)
        expected = docs_links.github_anchor(html.unescape(item.title))
        if item.id != expected:
            mismatches.append(f"{item.title!r}: site #{item.id}, GitHub #{expected}")
    if mismatches:
        raise ValueError(f"{src_uri} heading IDs differ from GitHub: {'; '.join(mismatches)}")


def on_post_build(config):
    """Index the built site for search; a failure fails the build."""
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pagefind",
            "--site",
            config.site_dir,
            "--exclude-selectors",
            ".headerlink",
        ],
        check=True,
        timeout=300,
    )

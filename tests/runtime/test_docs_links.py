"""User-doc links Booley prints point at published site pages (ADR 0076)."""

from __future__ import annotations

import ast
import re
from pathlib import Path

from tests.docs.test_links import _markdown_anchors

from booley.runtime.docs_links import USER_DOCS, docs_url

REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = REPO_ROOT / "src/booley"
USER_DOCS_DIR = REPO_ROOT / "docs/user"
_USER_DOC_PATH = re.compile(
    r"docs/user/|\b(?:" + "|".join(re.escape(doc.file) for doc in USER_DOCS) + r")\b"
)
# Modules whose string literals may name user docs by repo path or file name.
_REPO_PATH_ALLOWED = {
    # Agent-facing: Sandbox Specialists and agents read the Markdown in the repo.
    "specialists/review_contract.py",
    "runtime/project_worktree_pairing.py",
    # The slug table itself.
    "runtime/docs_links.py",
    # Locates the packaged TROUBLESHOOTING.md mirror on disk.
    "runtime/paths.py",
    # Maintainer tooling: syncs the packaged mirror / renders Markdown embedded in
    # docs/user/CLI-REFERENCE.md (a relative link there), not printed links.
    "dev_support/reference_docs.py",
    "dev_support/flow_specialist_reference.py",
}


def _python_sources() -> list[Path]:
    """Return Booley's Python sources; packaged data is agent-facing."""
    return [path for path in SOURCE_ROOT.rglob("*.py") if "data" not in path.parts]


def _docs_url_calls(tree: ast.AST) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "docs_url"
    ]


def _exempt_ids(tree: ast.AST) -> set[int]:
    """Return ``id()`` of docstrings and ``docs_url()`` arguments in one module."""
    ids = {id(arg) for call in _docs_url_calls(tree) for arg in call.args}
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                ids.add(id(body[0].value))
    return ids


def test_user_docs_table_matches_docs_user() -> None:
    files = [doc.file for doc in USER_DOCS]
    slugs = [doc.slug for doc in USER_DOCS]
    assert sorted(files) == sorted(path.name for path in USER_DOCS_DIR.glob("*.md"))
    assert len(set(slugs)) == len(slugs)


def test_child_docs_follow_their_parent_in_the_same_section() -> None:
    """The sidebar indents a child doc under the doc right before it."""
    for previous, doc in zip(USER_DOCS, USER_DOCS[1:], strict=False):
        if doc.parent is not None:
            assert (previous.file, previous.section) == (doc.parent, doc.section), doc.file
    assert USER_DOCS[0].parent is None


def test_docs_url_calls_name_published_docs_and_headings() -> None:
    failures = []
    calls = 0
    for path in _python_sources():
        for node in _docs_url_calls(ast.parse(path.read_text(encoding="utf-8"))):
            calls += 1
            args = [ast.literal_eval(arg) for arg in node.args]
            where = f"{path.relative_to(REPO_ROOT)}:{node.lineno}"
            try:
                docs_url(*args)
            except KeyError:
                failures.append(f"{where}: unpublished doc {args[0]}")
                continue
            if len(args) > 1 and args[1] not in _markdown_anchors(USER_DOCS_DIR / args[0]):
                failures.append(f"{where}: {args[0]} has no heading #{args[1]}")
    assert calls, "no docs_url() calls found"
    assert not failures, failures


def test_printed_messages_link_user_docs_on_the_site() -> None:
    offenders = []
    for path in _python_sources():
        module = path.relative_to(SOURCE_ROOT).as_posix()
        if module in _REPO_PATH_ALLOWED:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        exempt = _exempt_ids(tree)
        offenders += [
            f"{module}:{node.lineno}: {node.value!r}"
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in exempt
            and _USER_DOC_PATH.search(node.value)
        ]
    assert not offenders, "use booley.runtime.docs_links.docs_url():\n" + "\n".join(offenders)

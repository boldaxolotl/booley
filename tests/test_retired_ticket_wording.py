"""Guard: production strings use current vocabulary, not retired Ticket Mode wording.

Ticket Mode was removed (ADR 0067), but its message text lingered in ``--help``,
errors, logs, and generated files. This test scans every string literal in
``src/booley`` (docstrings excepted, since they are not shown to users, unless
argparse renders the module docstring as ``--help``) and every non-Python text
file for the words "ticket" and "tickets". Anything found must be on an
allowlist of legitimate remaining classes, each named below.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_PRODUCTION = _ROOT / "src" / "booley"
_RETIRED_WORD = re.compile(r"(?i)\btickets?\b")

# Allowed class: Doctor findings and remedies for leftover legacy ``tickets/`` data.
_DOCTOR_MIGRATION = (
    "tickets",
    "tickets.leftover-board",
    "tickets.leftover-guidance",
    "move any Ticket you still need into a Goalset or archive it, then delete ",
    "leftover Ticket Board data in ",
    "could not inspect leftover Ticket Board data in ",
    "leftover Ticket guidance in ",
    "could not read leftover Ticket guidance in ",
)
# Allowed class: gitignore / trackability patterns for the legacy ``tickets/`` directory.
_LEGACY_PATTERNS = (
    "tickets/board/",
    "tickets/state/",
    "tickets/waiver-candidates/",
    "tickets/logs/",
    "tickets/locks/",
    "tickets/history",
)

# path relative to src/booley -> string literals allowed there
_ALLOWED_LITERALS: dict[str, tuple[str, ...]] = {
    # Allowed class: banned commit-message token.
    "commit_policy/policy.py": ("ticket",),
    # Allowed class: internal identifiers and stored format keys.
    "criteria/freshness.py": ("ticket",),
    "criteria/unbound_ledger.py": ("ticket",),
    "evidence/review_receipt.py": ("ticket", "ticket.md"),
    "goals/input_view.py": ("ticket",),
    "runtime/_codex_backend.py": ("ticket",),
    "runtime/incontainer_git_identity.py": ("--ticket",),
    "harness/doctor.py": _DOCTOR_MIGRATION,
    "runtime/project_gitignore.py": _LEGACY_PATTERNS,
    "runtime/project_trackability.py": _LEGACY_PATTERNS,
}

# Non-Python files that may name the retired word, by allowed class.
_ALLOWED_TEXT_FILES = frozenset(
    {
        "data/refs/CHANGELOG.md",  # release history
        "data/refs/TROUBLESHOOTING.md",  # Doctor legacy migration findings
        "data/skills/booley-heal/SKILL.md",  # Doctor legacy migration remedies
        "feedback/GLOSSARY.md",  # Avoid list
        "runtime/worktree_create.sh",  # legacy tickets/ exclude patterns, --ticket flag
    }
)


def _help_docstring_ids(tree: ast.Module, source: str) -> set[int]:
    """Return ids of docstring nodes that are NOT user-visible (the ones to skip)."""
    skipped: set[int] = set()
    # argparse renders the module docstring as ``--help`` when it is the description.
    shows_module_doc = "description=__doc__" in source
    for node in ast.walk(tree):
        if not isinstance(
            node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef
        ):
            continue
        first = node.body[0] if node.body else None
        if not (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            continue
        if isinstance(node, ast.Module) and shows_module_doc:
            continue
        skipped.add(id(first.value))
    return skipped


def _python_hits() -> list[str]:
    hits: list[str] = []
    for path in sorted(_PRODUCTION.rglob("*.py")):
        relative = path.relative_to(_PRODUCTION).as_posix()
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        docstrings = _help_docstring_ids(tree, source)
        allowed = _ALLOWED_LITERALS.get(relative, ())
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                continue
            if id(node) in docstrings or not _RETIRED_WORD.search(node.value):
                continue
            if node.value not in allowed:
                hits.append(f"{relative}:{node.lineno} {node.value!r}")
    return hits


def _text_file_hits() -> list[str]:
    hits: list[str] = []
    for path in sorted(_PRODUCTION.rglob("*")):
        relative = path.relative_to(_PRODUCTION).as_posix()
        if not path.is_file() or path.suffix in {".py", ".pyc"} or relative in _ALLOWED_TEXT_FILES:
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            continue
        hits.extend(
            f"{relative}:{number} {line.strip()!r}"
            for number, line in enumerate(lines, 1)
            if _RETIRED_WORD.search(line)
        )
    return hits


def test_production_strings_use_current_vocabulary() -> None:
    hits = _python_hits() + _text_file_hits()
    assert not hits, "retired Ticket wording in production strings:\n" + "\n".join(hits)

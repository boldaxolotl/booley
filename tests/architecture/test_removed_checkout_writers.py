"""Writers under a checkout's Booley Directory create parents through ``make_directories``."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[2] / "src" / "booley"
AUDITED = (
    "bwave",
    "flows",
    "runtime/_claude_backend.py",
    "runtime/_claude_transcript_md.py",
    "runtime/_codex_backend.py",
    "runtime/_codex_transcript_md.py",
    "core/atomic_files.py",
    "runtime/booley_dir.py",
    "runtime/exception_diagnostics.py",
    "runtime/execution_records.py",
    "runtime/prompt_artifacts.py",
    "specialists",
)
# Working directories, temporary scratch and host roots: not reports, diagnostics or records.
ALLOWED = {
    ("bwave/waveform_store.py", "waveform_cache_dir"),
    ("bwave/waveform_store.py", "start_streaming_conversion"),
    ("flows/sim/compiler_cache.py", "_cache_blocker"),
    ("flows/sim/record/run_directory.py", "_create_run_directory"),
    ("flows/sim/record/run_directory.py", "_create_marked_chain"),
    ("runtime/_codex_backend.py", "_create_nested_codex_home"),
    ("specialists/specialist_workspace.py", "_copy_path"),
}


def _modules() -> list[Path]:
    paths: list[Path] = []
    for entry in AUDITED:
        root = SOURCE / entry
        paths.extend(sorted(root.rglob("*.py")) if root.is_dir() else [root])
    return paths


def _parent_creations(path: Path) -> list[tuple[str, int]]:
    """``(enclosing function, line)`` of every ``.mkdir(parents=True)`` call in *path*."""
    found: list[tuple[str, int]] = []

    def visit(node: ast.AST, function: str) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            function = node.name
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "mkdir"
            and any(
                keyword.arg == "parents"
                and isinstance(keyword.value, ast.Constant)
                and keyword.value.value is True
                for keyword in node.keywords
            )
        ):
            found.append((function, node.lineno))
        for child in ast.iter_child_nodes(node):
            visit(child, function)

    visit(ast.parse(path.read_text(encoding="utf-8")), "<module>")
    return found


@pytest.mark.parametrize("path", _modules(), ids=lambda path: path.relative_to(SOURCE).as_posix())
def test_audited_module_creates_no_parents_outside_the_allow_list(path):
    relative = path.relative_to(SOURCE).as_posix()
    offenders = [
        f"{relative}:{line} in {function}"
        for function, line in _parent_creations(path)
        if (relative, function) not in ALLOWED
    ]

    assert not offenders, "use make_directories (booley.runtime.filesystem_utils): " + ", ".join(
        offenders
    )

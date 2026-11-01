"""Goal Mode keeps one format per artifact and applies no migration-era rules (issue #2407)."""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

GOALS_SOURCE = Path(__file__).resolve().parents[2] / "src" / "booley" / "goals"
MIGRATION = "booley.runtime.booley_dir_migration"


def test_legacy_goal_presentation_module_is_gone() -> None:
    assert importlib.util.find_spec("booley.review.goal_presentation_v1") is None


def _migration_imports(path: Path) -> list[str]:
    """Every statement in *path*, lazy ones included, that imports the Directory migration."""
    found: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found += [alias.name for alias in node.names if alias.name == MIGRATION]
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            package, _, leaf = MIGRATION.rpartition(".")
            if node.module == MIGRATION or (
                node.module == package and any(alias.name == leaf for alias in node.names)
            ):
                found.append(node.module)
    return found


def test_goal_code_does_not_consult_the_directory_migration() -> None:
    sources = sorted(GOALS_SOURCE.rglob("*.py"))
    assert sources, "the Goal source tree moved; update this test"
    offenders = {
        str(path.relative_to(GOALS_SOURCE)): imports
        for path in sources
        if (imports := _migration_imports(path))
    }
    assert offenders == {}

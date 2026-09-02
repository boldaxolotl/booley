"""Shared assertions over Booley's production source dependency graph."""

from __future__ import annotations

from functools import cache
from pathlib import Path

from tests.architecture.import_graph import Dependency, analyze_imports, select_dependencies

REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = REPO_ROOT / "src" / "booley"


@cache
def production_dependencies() -> tuple[Dependency, ...]:
    """Parse the complete production tree once in this test process."""
    return analyze_imports(SOURCE_ROOT)


def assert_no_dependencies(
    *,
    paths: tuple[Path, ...] = (),
    source_prefixes: tuple[str, ...] = (),
    target_prefixes: tuple[str, ...],
) -> None:
    """Assert that selected production sources do not know selected targets."""
    selected_paths = {path.resolve() for path in paths}
    violations = select_dependencies(
        production_dependencies(),
        source_prefixes=source_prefixes,
        target_prefixes=target_prefixes,
    )
    if selected_paths:
        violations = tuple(item for item in violations if item.path in selected_paths)
    rendered = [
        f"{item.path.relative_to(REPO_ROOT)}:{item.line} {item.source} -> {item.target}"
        for item in violations
    ]
    assert not rendered, "dependency direction violations:\n" + "\n".join(rendered)

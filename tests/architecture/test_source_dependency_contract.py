from __future__ import annotations

from pathlib import Path

from tests.architecture.booley_contract import BOOLEY_SOURCE_DEPENDENCY_CONTRACT
from tests.architecture.contract import evaluate_contract, format_problems
from tests.architecture.import_graph import analyze_imports

_SOURCE_ROOT = Path(__file__).parents[2] / "src" / "booley"


def test_production_source_dependencies_obey_approved_contract() -> None:
    dependencies = analyze_imports(_SOURCE_ROOT)

    problems = evaluate_contract(dependencies, BOOLEY_SOURCE_DEPENDENCY_CONTRACT)

    assert not problems, "Source dependency contract failures:\n" + format_problems(problems)

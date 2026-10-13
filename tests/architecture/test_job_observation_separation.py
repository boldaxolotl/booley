"""Seeded proofs for the selected Runtime observation dependency directions."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.architecture.booley_contract import BOOLEY_SOURCE_DEPENDENCY_CONTRACT
from tests.architecture.contract import ArchitectureContract, evaluate_contract
from tests.architecture.import_graph import analyze_imports


@pytest.mark.parametrize(
    "source,target,rule",
    [
        ("runtime/consumer.py", "goals/store.py", "D36"),
        ("runtime/job_observation/__init__.py", "goals/binding.py", "D36"),
        ("runtime/job_observation/reader.py", "targets/catalog.py", "D37"),
        ("runtime/job_observation/reader.py", "harness/dashboard/jobs.py", "D6"),
        ("runtime/job_observation/reader.py", "mcp/job_lifecycle.py", "D5"),
        ("runtime/job_observation/reader.py", "specialists/specialist.py", "D5"),
    ],
)
@pytest.mark.parametrize("placement", ["ordinary", "deferred", "type-only"])
def test_observation_cannot_acquire_history_or_presentation_policy(
    tmp_path: Path, source: str, target: str, rule: str, placement: str
) -> None:
    root = tmp_path / "booley"
    root.mkdir()
    (root / "__init__.py").write_text("")
    target_path = root / target
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text("")
    module = "booley." + target.removesuffix(".py").replace("/", ".")
    statement = f"import {module}\n"
    if placement == "deferred":
        statement = "def read():\n    " + statement
    elif placement == "type-only":
        statement = "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    " + statement
    source_path = root / source
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_text(statement)
    contract = ArchitectureContract(rules=BOOLEY_SOURCE_DEPENDENCY_CONTRACT.rules)

    problems = evaluate_contract(analyze_imports(root), contract)

    assert rule in {problem.rule for problem in problems}

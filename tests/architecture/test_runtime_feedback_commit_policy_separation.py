"""Proofs for the issue #660 ownership directions."""

from pathlib import Path

import pytest

from tests.architecture.booley_contract import BOOLEY_SOURCE_DEPENDENCY_CONTRACT
from tests.architecture.contract import ArchitectureContract, evaluate_contract
from tests.architecture.import_graph import analyze_imports


@pytest.mark.parametrize(
    ("source", "target", "rule"),
    [
        ("runtime/consumer.py", "dev_support/provider.py", "D32"),
        ("feedback/consumer.py", "harness/provider.py", "D33"),
        ("commit_policy/consumer.py", "runtime/provider.py", "D34"),
        ("commit_policy/consumer.py", "harness/provider.py", "D34"),
        ("commit_policy/consumer.py", "dev_support/provider.py", "D34"),
    ],
)
@pytest.mark.parametrize(
    "statement",
    [
        "import {module}\n",
        "from {parent} import provider\n",
        "def deferred():\n    import {module}\n",
        "if enabled:\n    import {module}\n",
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import {module}\n",
        "from booley import {package}\n",
        "from ..{package} import provider\n",
    ],
)
def test_issue_660_directions_cover_every_static_import_form(
    tmp_path: Path,
    source: str,
    target: str,
    rule: str,
    statement: str,
) -> None:
    package = tmp_path / "booley"
    source_package = source.split("/", maxsplit=1)[0]
    target_package = target.split("/", maxsplit=1)[0]
    for relative in (
        "__init__.py",
        f"{source_package}/__init__.py",
        f"{target_package}/__init__.py",
        source,
        target,
    ):
        path = package / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("")
    module = "booley." + target.removesuffix(".py").replace("/", ".")
    parent, _, _name = module.rpartition(".")
    (package / source).write_text(
        statement.format(module=module, parent=parent, package=target_package)
    )

    contract = ArchitectureContract(rules=BOOLEY_SOURCE_DEPENDENCY_CONTRACT.rules)
    problems = evaluate_contract(analyze_imports(package), contract)

    matching = [problem for problem in problems if problem.rule == rule]
    assert matching
    assert all(problem.dependency is not None for problem in matching)
    assert all(
        problem.dependency is not None
        and problem.dependency.source == "booley." + source.removesuffix(".py").replace("/", ".")
        for problem in matching
    )

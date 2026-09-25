"""Proofs for Config/EDA direction rules and shared host mechanism ownership."""

from pathlib import Path

import pytest

from tests.architecture.booley_contract import BOOLEY_SOURCE_DEPENDENCY_CONTRACT
from tests.architecture.contract import ArchitectureContract, evaluate_contract
from tests.architecture.import_graph import Dependency, analyze_imports, top_level_package_sccs


@pytest.mark.parametrize(
    "source,target,rule", [("config", "eda", "D23"), ("eda", "runtime", "D24")]
)
@pytest.mark.parametrize(
    "location", ["module", "relative", "function", "conditional", "type", "initializer"]
)
def test_new_directions_cover_every_static_import_location(
    tmp_path, source, target, rule, location
):
    root = tmp_path / "booley"
    for package in (root, root / source, root / target):
        package.mkdir(exist_ok=True)
        (package / "__init__.py").touch()
    (root / target / "provider.py").write_text("value = 1\n")
    statement = f"from booley.{target} import provider\n"
    statements = {
        "module": statement,
        "relative": f"from ..{target} import provider\n",
        "function": f"def helper():\n    {statement}",
        "conditional": f"if False:\n    {statement}",
        "type": f"from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    {statement}",
        "initializer": statement,
    }
    filename = "__init__.py" if location == "initializer" else "consumer.py"
    (root / source / filename).write_text(statements[location])
    problems = evaluate_contract(analyze_imports(root), BOOLEY_SOURCE_DEPENDENCY_CONTRACT)
    assert rule in {problem.rule for problem in problems}


@pytest.mark.parametrize("owner", ["private_store", "file_lock", "resources"])
@pytest.mark.parametrize(
    "target", ["config.eda", "eda.provisioning.authority", "runtime.private_store"]
)
def test_neutral_mechanisms_cannot_reacquire_caller_policy(owner, target):
    dependency = Dependency(f"booley.core.{owner}", f"booley.{target}", Path("seed.py"), 1, 0)
    problems = evaluate_contract((dependency,), BOOLEY_SOURCE_DEPENDENCY_CONTRACT)
    assert "D25" in {problem.rule for problem in problems}


def test_downward_consumption_remains_allowed():
    pairs = [
        ("config.eda", "core.project_dir"),
        ("eda.provisioning.configuration", "config.eda"),
        ("eda.provisioning.authority", "core.private_store"),
        ("eda.provisioning.policies.vivado", "core.resources"),
        ("runtime.session_issuance", "eda.provisioning.session_requirements"),
        ("runtime.private_store", "core.private_store"),
        ("runtime.file_lock", "core.file_lock"),
        ("core.private_store", "core.file_lock"),
    ]
    dependencies = tuple(
        Dependency(f"booley.{source}", f"booley.{target}", Path("seed.py"), 1, 0)
        for source, target in pairs
    )
    contract = ArchitectureContract(rules=BOOLEY_SOURCE_DEPENDENCY_CONTRACT.rules)
    assert evaluate_contract(dependencies, contract) == ()


def test_approved_cyclic_groups_are_tightened_to_actual_groups():
    root = Path(__file__).resolve().parents[2] / "src" / "booley"
    actual = {
        frozenset(group)
        for group in top_level_package_sccs(analyze_imports(root))
        if len(group) > 1
    }
    assert set(BOOLEY_SOURCE_DEPENDENCY_CONTRACT.approved_sccs) == actual, (
        "Tighten approved SCC metadata after a split so separated groups cannot recombine"
    )


@pytest.mark.parametrize(
    "separated",
    [
        "audit",
        "commit_policy",
        "config",
        "core",
        "docker",
        "eda",
        "evidence",
        "presentation",
        "projects",
        "review",
    ],
)
def test_separated_packages_cannot_join_the_remaining_group(separated):
    dependencies = (
        Dependency("booley.runtime.seed", f"booley.{separated}.seed", Path("seed.py"), 1, 0),
        Dependency(f"booley.{separated}.seed", "booley.runtime.seed", Path("seed.py"), 2, 0),
    )
    contract = ArchitectureContract(approved_sccs=BOOLEY_SOURCE_DEPENDENCY_CONTRACT.approved_sccs)
    problems = evaluate_contract(dependencies, contract)
    assert len(problems) == 1
    assert problems[0].kind == "scc"

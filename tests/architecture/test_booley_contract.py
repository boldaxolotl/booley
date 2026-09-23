from __future__ import annotations

from pathlib import Path

import pytest

from tests.architecture.booley_contract import BOOLEY_SOURCE_DEPENDENCY_CONTRACT
from tests.architecture.contract import ArchitectureContract, evaluate_contract
from tests.architecture.import_graph import Dependency, analyze_imports


def test_flow_rule_selectors_preserve_same_flow_and_adapter_set_edges() -> None:
    allowed = (
        _dependency("booley.flows.sim.flow", "booley.flows.sim.backends.icarus"),
        _dependency(
            "booley.flows.sim.backends.cocotb",
            "booley.flows.sim.backends.cocotb_results",
        ),
        _dependency(
            "booley.flows.sim",
            "booley.flows.sim.flow",
            path="flows/sim/__init__.py",
        ),
        _dependency("booley.flows.shared.policy", "booley.flows.sim.flow"),
    )

    assert evaluate_contract(allowed, _directions_only()) == ()


@pytest.mark.parametrize(
    ("rule", "source", "target", "path"),
    (
        ("D29", "booley.bwave", "booley.flows", "bwave/__init__.py"),
        ("D29", "booley.bwave.waveform_store", "booley.flows.sim", "seed.py"),
        ("D27", "booley.targets", "booley.flows", "targets/__init__.py"),
        ("D27", "booley.targets.target_surface", "booley.flows.edam", "seed.py"),
        ("D27", "booley.targets.catalog", "booley.runtime.git", "seed.py"),
        ("D28", "booley.fusesoc", "booley.runtime", "fusesoc/__init__.py"),
        ("D28", "booley.fusesoc.core_security", "booley.runtime.git", "seed.py"),
        ("D18", "booley.config.agent", "booley.runtime.agent_backend", "seed.py"),
        ("D21", "booley.review.generation", "booley.ticket_board.io", "seed.py"),
        ("D21", "booley.review.generation", "booley.harness.booley", "seed.py"),
        ("D22", "booley.ticket_board.io", "booley.review.artifact", "seed.py"),
        ("D15", "booley.flows", "booley.mcp", "flows/__init__.py"),
        ("D15", "booley.flows.base", "booley.mcp.base", "flows/base.py"),
        ("D15", "booley.flows.sim.flow", "booley.mcp.schema_extractor", "seed.py"),
        ("D16", "booley.criteria.policy", "booley.flows.execution", "seed.py"),
        ("D30", "booley.criteria.policy", "booley.ticket_board.io", "seed.py"),
        ("D17", "booley.flows.base", "booley.ticket_board.io", "seed.py"),
        ("D1", "booley.audit.policy", "booley.mcp.registry", "seed.py"),
        ("D1", "booley.config.settings", "booley.harness.cli", "seed.py"),
        ("D1", "booley.fusesoc.target", "booley.specialists.reviewer", "seed.py"),
        ("D1", "booley.targets.registry", "booley.harness.cli", "seed.py"),
        ("D2", "booley.criteria.policy", "booley.harness.cli", "seed.py"),
        ("D2", "booley.criteria.policy", "booley.mcp.registry", "seed.py"),
        ("D2", "booley.criteria.policy", "booley.specialists.reviewer", "seed.py"),
        ("D3", "booley.specialists.reviewer", "booley.harness.cli", "seed.py"),
        ("D3", "booley.specialists.reviewer", "booley.mcp.server", "seed.py"),
        ("D4", "booley.mcp.registry", "booley.harness.cli", "seed.py"),
        ("D4", "booley.mcp.registry", "booley.specialists.reviewer", "seed.py"),
        ("D5", "booley.runtime.agent", "booley.mcp.registry", "seed.py"),
        ("D5", "booley.runtime.agent", "booley.specialists.reviewer", "seed.py"),
        ("D6", "booley.runtime.agent", "booley.harness.cli", "seed.py"),
        ("D14", "booley.runtime.agent", "booley.ticket_board.paths", "seed.py"),
        ("D7", "booley.flows.target_campaign", "booley.harness.cli", "seed.py"),
        ("D7", "booley.flows.target_criteria", "booley.mcp.registry", "seed.py"),
        (
            "D7",
            "booley.flows.target_test_suite",
            "booley.ticket_board.io",
            "seed.py",
        ),
        ("D8", "booley.flows.sim.flow", "booley.flows.lint.flow", "seed.py"),
        ("D8", "booley.flows.lint.flow", "booley.flows.fpga.flow", "seed.py"),
        ("D8", "booley.flows.fpga.flow", "booley.flows.synth.flow", "seed.py"),
        ("D8", "booley.flows.synth.flow", "booley.flows.sim.flow", "seed.py"),
        ("D9", "booley.flows", "booley.flows.sim.flow", "flows/__init__.py"),
        ("D9", "booley.flows.policy", "booley.flows.fpga.flow", "flows/policy.py"),
        (
            "D10",
            "booley.flows.sim.backends.cocotb",
            "booley.flows.sim.backends.icarus",
            "seed.py",
        ),
        (
            "D10",
            "booley.flows.sim.backends.cocotb_results",
            "booley.flows.sim.backends.icarus",
            "seed.py",
        ),
        (
            "D10",
            "booley.flows.sim.backends.icarus",
            "booley.flows.sim.backends.verilator",
            "seed.py",
        ),
        (
            "D10",
            "booley.flows.sim.backends.verilator",
            "booley.flows.sim.backends.cocotb",
            "seed.py",
        ),
        (
            "D10",
            "booley.flows.synth.backends.openroad.step",
            "booley.flows.synth.backends.yosys.step",
            "seed.py",
        ),
        (
            "D10",
            "booley.flows.synth.backends.yosys.step",
            "booley.flows.synth.backends.openroad.step",
            "seed.py",
        ),
        (
            "D11",
            "booley.flows.synth.backends.openroad.step",
            "booley.flows.synth.flow",
            "seed.py",
        ),
        (
            "D11",
            "booley.flows.synth.backends.yosys.step",
            "booley.flows.synth.flow",
            "seed.py",
        ),
        ("D12", "booley.targets.domain", "booley.fusesoc.registry", "seed.py"),
        ("D12", "booley.targets.selection", "booley.flows.sim.flow", "seed.py"),
        ("D12", "booley.targets.selection", "booley.flows.synth.flow", "seed.py"),
        ("D12", "booley.targets.selection", "booley.flows.fpga.flow", "seed.py"),
        ("D12", "booley.targets.selection", "booley.flows.lint.flow", "seed.py"),
        ("D12", "booley.targets.domain", "booley.targets.catalog", "seed.py"),
        ("D12", "booley.targets.selection", "booley.targets.target_surface", "seed.py"),
        ("D12", "booley.fusesoc.registry", "booley.targets.catalog", "seed.py"),
        ("D12", "booley.fusesoc.target_inspection", "booley.targets.target_surface", "seed.py"),
        ("D13", "booley.fusesoc.registry", "booley.flows.sim.flow", "seed.py"),
        ("D13", "booley.fusesoc.registry", "booley.flows.synth.flow", "seed.py"),
        ("D13", "booley.fusesoc.registry", "booley.flows.fpga.flow", "seed.py"),
        ("D13", "booley.fusesoc.registry", "booley.flows.lint.flow", "seed.py"),
    ),
)
def test_every_direction_rule_selector_family_finds_a_forbidden_edge(
    rule: str, source: str, target: str, path: str
) -> None:
    dependency = _dependency(source, target, path=path)

    problems = evaluate_contract((dependency,), _directions_only())

    assert rule in {problem.rule for problem in problems}


def _directions_only() -> ArchitectureContract:
    return ArchitectureContract(rules=BOOLEY_SOURCE_DEPENDENCY_CONTRACT.rules)


@pytest.mark.parametrize(
    ("source", "target", "rule"),
    [
        ("targets/probe.py", "flows/edam.py", "D27"),
        ("targets/__init__.py", "runtime/git.py", "D27"),
        ("fusesoc/core_security.py", "runtime/git.py", "D28"),
    ],
)
@pytest.mark.parametrize(
    "statement",
    [
        "import {module} as dependency\n",
        "def deferred():\n    import {module}\n",
        "if enabled:\n    import {module}\n",
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import {module}\n",
    ],
)
def test_target_separation_catches_all_source_import_locations(
    tmp_path: Path, source: str, target: str, rule: str, statement: str
) -> None:
    package = tmp_path / "booley"
    for relative in ("__init__.py", source, target):
        path = package / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("")
    module = "booley." + target.removesuffix(".py").replace("/", ".")
    (package / source).write_text(statement.format(module=module))
    problems = evaluate_contract(analyze_imports(package), _directions_only())
    assert rule in {problem.rule for problem in problems}


@pytest.mark.parametrize(
    "statement",
    [
        "import booley.ticket_board.io\n",
        "from booley.ticket_board.io import TicketIO\n",
        "def deferred():\n    from booley.ticket_board.io import TicketIO\n",
        "if enabled:\n    from booley.ticket_board.io import TicketIO\n",
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n"
        "    from booley.ticket_board.io import TicketIO\n",
        "from booley import ticket_board\n",
        "from ..ticket_board import io\n",
    ],
)
def test_d30_catches_every_ticket_board_import_form(tmp_path: Path, statement: str) -> None:
    package = tmp_path / "booley"
    for relative in (
        "__init__.py",
        "criteria/__init__.py",
        "criteria/policy.py",
        "ticket_board/__init__.py",
        "ticket_board/io.py",
    ):
        path = package / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("")
    source = package / "criteria/policy.py"
    source.write_text(statement)

    problems = evaluate_contract(analyze_imports(package), _directions_only())

    d30 = [problem for problem in problems if problem.rule == "D30"]
    assert d30
    dependencies = [problem.dependency for problem in d30]
    assert all(dependency is not None for dependency in dependencies)
    assert all(
        dependency is not None and dependency.source == "booley.criteria.policy"
        for dependency in dependencies
    )
    assert all(
        dependency is not None and dependency.target.startswith("booley.ticket_board")
        for dependency in dependencies
    )


def test_target_separation_allows_downward_mechanisms() -> None:
    dependencies = (
        _dependency("booley.targets.target_surface", "booley.core.build_paths"),
        _dependency("booley.fusesoc.core_security", "booley.core.scope_matching"),
        _dependency("booley.flows.sim.flow", "booley.core.build_paths"),
        _dependency("booley.runtime.git", "booley.core.scope_matching"),
        _dependency("booley.targets.catalog", "booley.fusesoc.fusesoc_registry"),
        _dependency("booley.fusesoc.fusesoc_registry", "booley.targets.domain"),
    )
    for dependency in dependencies:
        assert evaluate_contract((dependency,), _directions_only()) == ()


@pytest.mark.parametrize(
    "statement",
    [
        "import booley.flows.sim\n",
        "def deferred():\n    import booley.flows.sim\n",
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import booley.flows.sim\n",
    ],
)
def test_bwave_separation_catches_all_import_locations(tmp_path: Path, statement: str) -> None:
    package = tmp_path / "booley"
    for relative in ("__init__.py", "bwave/__init__.py", "bwave/consumer.py", "flows/sim.py"):
        path = package / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("")
    (package / "bwave/consumer.py").write_text(statement)

    problems = evaluate_contract(analyze_imports(package), _directions_only())

    assert "D29" in {problem.rule for problem in problems}


def test_approved_target_group_cannot_recombine_with_execution() -> None:
    contract = ArchitectureContract(
        approved_sccs=BOOLEY_SOURCE_DEPENDENCY_CONTRACT.approved_sccs,
    )
    separate = (
        _dependency("booley.targets.catalog", "booley.fusesoc.fusesoc_registry"),
        _dependency("booley.fusesoc.fusesoc_registry", "booley.targets.domain"),
        _dependency("booley.flows.edam", "booley.runtime.git"),
        _dependency("booley.runtime.git", "booley.flows.edam"),
    )
    assert evaluate_contract(separate, contract) == ()
    recombined = (
        *separate,
        _dependency("booley.targets.target_surface", "booley.flows.edam"),
        _dependency("booley.runtime.git", "booley.fusesoc.core_security"),
    )
    problems = evaluate_contract(recombined, contract)
    assert len(problems) == 1
    assert problems[0].kind == "scc"
    assert "booley.targets" in problems[0].message
    assert "booley.runtime" in problems[0].message


def _dependency(source: str, target: str, *, line: int = 1, path: str = "seed.py") -> Dependency:
    return Dependency(source, target, Path(path), line, 0)

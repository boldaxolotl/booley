from __future__ import annotations

from pathlib import Path

import pytest

from tests.architecture.booley_contract import BOOLEY_SOURCE_DEPENDENCY_CONTRACT
from tests.architecture.contract import ArchitectureContract, evaluate_contract
from tests.architecture.import_graph import Dependency


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


def _dependency(source: str, target: str, *, line: int = 1, path: str = "seed.py") -> Dependency:
    return Dependency(source, target, Path(path), line, 0)

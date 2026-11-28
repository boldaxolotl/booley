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
        ("D40", "booley.presentation.colors", "booley.harness.booley", "seed.py"),
        ("D40", "booley.presentation.colors", "booley.mcp.server", "seed.py"),
        ("D40", "booley.presentation.colors", "booley.runtime.git", "seed.py"),
        ("D40", "booley.presentation.colors", "booley.flows.base", "seed.py"),
        ("D29", "booley.bwave", "booley.flows", "bwave/__init__.py"),
        ("D29", "booley.bwave.waveform_store", "booley.flows.sim", "seed.py"),
        ("D27", "booley.targets", "booley.flows", "targets/__init__.py"),
        ("D27", "booley.targets.target_surface", "booley.flows.edam", "seed.py"),
        ("D27", "booley.targets.catalog", "booley.runtime.git", "seed.py"),
        ("D28", "booley.fusesoc", "booley.runtime", "fusesoc/__init__.py"),
        ("D28", "booley.fusesoc.core_security", "booley.runtime.git", "seed.py"),
        ("D18", "booley.config.agent", "booley.runtime.agent_backend", "seed.py"),
        ("D21", "booley.review.package", "booley.harness.booley", "seed.py"),
        ("D15", "booley.flows", "booley.mcp", "flows/__init__.py"),
        ("D15", "booley.flows.base", "booley.mcp.base", "flows/base.py"),
        ("D15", "booley.flows.sim.flow", "booley.mcp.schema_extractor", "seed.py"),
        ("D1", "booley.audit.policy", "booley.mcp.registry", "seed.py"),
        ("D1", "booley.config.settings", "booley.harness.cli", "seed.py"),
        ("D1", "booley.fusesoc.target", "booley.specialists.reviewer", "seed.py"),
        ("D1", "booley.targets.registry", "booley.harness.cli", "seed.py"),
        ("D3", "booley.specialists.reviewer", "booley.harness.cli", "seed.py"),
        ("D3", "booley.specialists.reviewer", "booley.mcp.server", "seed.py"),
        ("D4", "booley.mcp.registry", "booley.harness.cli", "seed.py"),
        ("D4", "booley.mcp.registry", "booley.specialists.reviewer", "seed.py"),
        ("D5", "booley.runtime.agent", "booley.mcp.registry", "seed.py"),
        ("D5", "booley.runtime.agent", "booley.specialists.reviewer", "seed.py"),
        ("D6", "booley.runtime.agent", "booley.harness.cli", "seed.py"),
        ("D5", "booley.runtime.agent", "booley.mcp.base", "seed.py"),
        ("D25", "booley.core.file_lock", "booley.mcp.server", "seed.py"),
        ("D7", "booley.flows.target_campaign", "booley.harness.cli", "seed.py"),
        (
            "D7",
            "booley.flows.target_test_suite",
            "booley.harness.booley",
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
        ("D41", "booley.runtime.agent", "booley.flows.sim.flow", "seed.py"),
        ("D19", "booley.eda.policy", "booley.flows.execution", "seed.py"),
        ("D20", "booley.eda.policy", "booley.runtime.session_issuance", "seed.py"),
        ("D20", "booley.eda.policy", "booley.runtime.issuance_invalidation", "seed.py"),
        ("D25", "booley.core.atomic_files", "booley.runtime.timefmt", "seed.py"),
        ("D23", "booley.config.seed", "booley.eda.provisioning.authority", "seed.py"),
        ("D24", "booley.eda.seed", "booley.runtime.agent", "seed.py"),
        ("D31", "booley.flows.sim.rogue", "booley.flows.sim.record.store", "seed.py"),
        ("D31", "booley.flows.sim.rogue", "booley.flows.sim.record.inspection", "seed.py"),
        ("D32", "booley.runtime.consumer", "booley.dev_support.provider", "seed.py"),
        ("D33", "booley.feedback.consumer", "booley.harness.provider", "seed.py"),
        ("D34", "booley.commit_policy.consumer", "booley.runtime.provider", "seed.py"),
        ("D34", "booley.commit_policy.consumer", "booley.harness.provider", "seed.py"),
        ("D34", "booley.commit_policy.consumer", "booley.dev_support.provider", "seed.py"),
        ("D37", "booley.runtime.job_observation.reader", "booley.targets.catalog", "seed.py"),
        ("D39", "booley.flows.rogue", "booley.harness.cli_goal_notice", "seed.py"),
        ("D39", "booley.flows.runtime_reality", "booley.harness.doctor", "seed.py"),
        ("D39", "booley.flows.runtime_reality", "booley.harness.setup.readiness", "seed.py"),
        ("D39", "booley.flows.fpga.vivado_startup", "booley.harness.doctor", "seed.py"),
        ("D39", "booley.flows.fpga.vivado_startup", "booley.harness.setup.readiness", "seed.py"),
        ("D42", "booley.flows.sim.rogue", "booley.flows.mutation.flow", "seed.py"),
        ("D42", "booley.flows.lint.rogue", "booley.flows.mutation.flow", "seed.py"),
        ("D42", "booley.flows.rogue", "booley.flows.mutation.flow", "seed.py"),
        ("D4", "booley.mcp.server_inspection", "booley.harness.setup.readiness", "seed.py"),
        ("D26", "booley.harness.host_diagnostics", "booley.harness.doctor", "seed.py"),
        ("D26", "booley.harness.setup.readiness", "booley.harness.init_cmd", "seed.py"),
        ("D26", "booley.runtime.inspection", "booley.harness.booley", "seed.py"),
        (
            "D26",
            "booley.flows.sim.backends.cocotb_target_checks",
            "booley.presentation.colors",
            "seed.py",
        ),
        ("D26", "booley.runtime.vaporview", "booley.harness.setup.common", "seed.py"),
        ("D26", "booley.flows.target_test_plan_inspection", "booley.harness.doctor", "seed.py"),
        ("D26", "booley.audit.core_tree_inspection", "booley.harness.init_cmd", "seed.py"),
        ("D26", "booley.runtime.core_repository_hygiene", "booley.harness.booley", "seed.py"),
        ("D26", "booley.runtime.core_resolution", "booley.presentation.colors", "seed.py"),
        ("D26", "booley.runtime.project_image", "booley.harness.setup.common", "seed.py"),
        ("D26", "booley.mcp.server_inspection", "booley.harness.doctor", "seed.py"),
        ("D26", "booley.flows.runtime_reality", "booley.harness.init_cmd", "seed.py"),
        ("D26", "booley.flows.fpga.vivado_startup", "booley.harness.booley", "seed.py"),
    ),
)
def test_every_direction_rule_selector_family_finds_a_forbidden_edge(
    rule: str, source: str, target: str, path: str
) -> None:
    dependency = _dependency(source, target, path=path)

    problems = evaluate_contract((dependency,), _directions_only())

    assert rule in {problem.rule for problem in problems}


def test_simulation_record_storage_rule_allows_public_facade_import(tmp_path: Path) -> None:
    root = _simulation_record_rule_seed(
        tmp_path,
        "flows/sim/rogue.py",
        "from booley.flows.sim import record\n",
    )

    problems = evaluate_contract(analyze_imports(root), BOOLEY_SOURCE_DEPENDENCY_CONTRACT)

    assert not [problem for problem in problems if problem.rule == "D31"]


def _directions_only() -> ArchitectureContract:
    return ArchitectureContract(rules=BOOLEY_SOURCE_DEPENDENCY_CONTRACT.rules)


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


def test_approved_target_group_cannot_recombine_with_execution() -> None:
    contract = ArchitectureContract(
        approved_sccs=BOOLEY_SOURCE_DEPENDENCY_CONTRACT.approved_sccs,
    )
    separate = (
        _dependency("booley.targets.catalog", "booley.fusesoc.fusesoc_registry"),
        _dependency("booley.fusesoc.fusesoc_registry", "booley.targets.domain"),
    )
    assert evaluate_contract(separate, contract) == ()
    recombined = (
        *separate,
        _dependency("booley.targets.target_surface", "booley.flows.edam"),
        _dependency("booley.flows.edam", "booley.fusesoc.core_security"),
    )
    problems = evaluate_contract(recombined, contract)
    assert len(problems) == 1
    assert problems[0].kind == "scc"
    assert "booley.targets" in problems[0].message
    assert "booley.flows" in problems[0].message


def _dependency(source: str, target: str, *, line: int = 1, path: str = "seed.py") -> Dependency:
    return Dependency(source, target, Path(path), line, 0)


def _simulation_record_rule_seed(tmp_path: Path, source: str, statement: str) -> Path:
    root = tmp_path / "booley"
    files = (
        "__init__.py",
        "flows/__init__.py",
        "flows/sim/__init__.py",
        "flows/sim/record/__init__.py",
        "flows/sim/record/store.py",
        "flows/sim/record/inspection.py",
        source,
    )
    for relative in files:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("")
    (root / source).write_text(statement)
    return root

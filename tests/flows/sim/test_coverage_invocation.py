from dataclasses import replace
from fractions import Fraction
from pathlib import Path

import pytest

from booley.flows.sim.coverage_campaign import DurableTargetIdentity
from booley.flows.sim.coverage_invocation import (
    CoverageInvocationRequest,
    CoverageProjectContext,
    prepare_coverage_invocation,
)
from booley.flows.sim.coverage_policy import CoverageCriterion, CoverageThreshold


def project(root: Path, tools: tuple[str, ...] = ("verilator",)) -> CoverageProjectContext:
    (root / "rtl").mkdir()
    (root / "rtl/counter.sv").write_text("module counter; endmodule\n")
    targets = "".join(
        f"  sim_{i}:\n    flow: sim\n    default_tool: {tool}\n"
        f"    flow_options: {{tool: {tool}}}\n"
        "    filesets: [rtl]\n    toplevel: counter\n"
        for i, tool in enumerate(tools)
    )
    (root / "counter.core").write_text(
        "CAPI=2:\nname: acme:demo:counter:1\nfilesets:\n  rtl:\n"
        "    files: [rtl/counter.sv]\n    file_type: systemVerilogSource\n"
        f"targets:\n{targets}"
    )
    return CoverageProjectContext(
        root, root / "project-data", {f"sim_{i}": ("reset", "wrap") for i in range(len(tools))}
    )


def test_coverage_context_prefers_checkout_local_project_data(tmp_path, monkeypatch):
    from booley.criteria.state import DevelopmentState
    from booley.flows.sim import coverage_flow_context
    from booley.runtime.project_dir import reset_cache, resolve_project_dir

    checkout = tmp_path / "checkout"
    local_project_data = checkout / ".booley_project"
    local_project_data.mkdir(parents=True)
    unrelated = tmp_path / "unrelated-project-data"
    unrelated.mkdir()
    monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(unrelated))
    reset_cache()
    assert resolve_project_dir() == unrelated.resolve()
    monkeypatch.setattr(coverage_flow_context, "_coverage_policies", lambda *_args: {})
    monkeypatch.setattr(
        coverage_flow_context,
        "load_test_configuration_field",
        lambda *_args: {},
    )

    context = coverage_flow_context.coverage_project_context(checkout, DevelopmentState())

    assert context.project_data_repository == local_project_data


def test_preflight_resolves_full_suite_without_creating_artifacts(tmp_path: Path) -> None:
    context = project(tmp_path)
    before = set(tmp_path.rglob("*"))
    result = prepare_coverage_invocation(CoverageInvocationRequest(("sim_0",)), context)
    assert result.findings == ()
    assert result.plan is not None
    assert result.plan.targets[0].selected_tests == ("reset", "wrap")
    assert result.plan.targets[0].handle.identity == "acme:demo:counter:1#sim_0"
    assert set(tmp_path.rglob("*")) == before


def test_preflight_aggregates_invalid_targets_and_rejects_mixed_tools_atomically(
    tmp_path: Path,
) -> None:
    context = project(tmp_path, ("verilator", "icarus"))
    before = set(tmp_path.rglob("*"))
    result = prepare_coverage_invocation(
        CoverageInvocationRequest(("sim_0", "sim_1", "missing")), context
    )
    assert result.plan is None
    assert {finding.code for finding in result.findings} == {
        "COV_TARGET_INVALID",
        "COV_TOOL_UNSUPPORTED",
    }
    assert set(tmp_path.rglob("*")) == before


def test_selection_precedence_is_explicit_then_criterion_then_registered_suite(
    tmp_path: Path,
) -> None:
    context = project(tmp_path)
    criterion = CoverageCriterion(
        DurableTargetIdentity("acme:demo:counter:1#sim_0"),
        (CoverageThreshold("line", Fraction(100)),),
        ("wrap",),
    )
    context = replace(context, criteria={"coverage_sim_0": criterion})
    prepared = prepare_coverage_invocation(CoverageInvocationRequest(("sim_0",)), context)
    assert prepared.plan.targets[0].selected_tests == ("wrap",)
    explicit = prepare_coverage_invocation(
        CoverageInvocationRequest(("sim_0",), ("reset",)), context
    )
    assert explicit.plan.targets[0].selected_tests == ("reset",)
    assert explicit.plan.targets[0].criterion == criterion


@pytest.mark.parametrize(
    "tests,declared",
    [((), ("reset",)), (("unknown",), ("reset",)), (("reset", "reset"), ("reset",)), (None, ())],
)
def test_preflight_rejects_empty_duplicate_or_unregistered_tests(tmp_path, tests, declared):
    context = replace(project(tmp_path), test_names={"sim_0": declared})
    result = prepare_coverage_invocation(CoverageInvocationRequest(("sim_0",), tests), context)
    assert result.plan is None
    assert any(item.code == "COV_SUITE_INVALID" for item in result.findings)


@pytest.mark.parametrize(
    "hooks,reset", [(None, True), ("[write_hook]", False), ("[start_hook, write_hook]", True)]
)
def test_custom_main_hook_errors_are_atomic_preflight_errors(tmp_path, hooks, reset):
    context = project(tmp_path)
    path = tmp_path / "counter.core"
    (tmp_path / "main.cpp").write_text("int main() { return 0; }\n")
    data = path.read_text().replace(
        "files: [rtl/counter.sv]", "files: [rtl/counter.sv, {main.cpp: {file_type: cppSource}}]"
    )
    coverage = f"reset_included: {str(reset).lower()}"
    if hooks is not None:
        coverage += f", custom_main_hooks: {hooks}"
    data = data.replace(
        "flow_options: {tool: verilator}",
        f"flow_options: {{tool: verilator, booley: {{coverage: {{{coverage}}}}}}}",
    )
    path.write_text(data)
    result = prepare_coverage_invocation(CoverageInvocationRequest(("sim_0",)), context)
    assert result.plan is None
    assert any("HOOK" in item.code for item in result.findings)


def test_unfiltered_suite_applies_configured_skips(tmp_path: Path) -> None:
    context = replace(project(tmp_path), skipped_tests={"sim_0": ("wrap",)})
    prepared = prepare_coverage_invocation(CoverageInvocationRequest(("sim_0",)), context)
    assert prepared.plan.targets[0].selected_tests == ("reset",)


@pytest.mark.parametrize(
    ("tests", "expected"),
    [(("wrap",), ("wrap",)), (("reset", "wrap"), ("reset", "wrap"))],
)
def test_explicit_suite_overrides_configured_skips(tmp_path: Path, tests, expected) -> None:
    context = replace(project(tmp_path), skipped_tests={"sim_0": ("wrap",)})
    prepared = prepare_coverage_invocation(
        CoverageInvocationRequest(("sim_0",), tests=tests), context
    )
    assert prepared.plan.targets[0].selected_tests == expected


def test_exact_criterion_suite_overrides_configured_skips(tmp_path: Path) -> None:
    criterion = CoverageCriterion(
        DurableTargetIdentity("acme:demo:counter:1#sim_0"),
        (CoverageThreshold("line", Fraction(100)),),
        ("wrap",),
    )
    context = replace(
        project(tmp_path),
        criteria={"coverage_sim_0": criterion},
        skipped_tests={"sim_0": ("wrap",)},
    )
    prepared = prepare_coverage_invocation(CoverageInvocationRequest(("sim_0",)), context)
    assert prepared.plan.targets[0].selected_tests == ("wrap",)


def test_all_tests_criterion_applies_configured_skips(tmp_path: Path) -> None:
    criterion = CoverageCriterion(
        DurableTargetIdentity("acme:demo:counter:1#sim_0"),
        (CoverageThreshold("line", Fraction(100)),),
        None,
    )
    context = replace(
        project(tmp_path),
        criteria={"coverage_sim_0": criterion},
        skipped_tests={"sim_0": ("wrap",)},
    )
    prepared = prepare_coverage_invocation(CoverageInvocationRequest(("sim_0",)), context)
    assert prepared.plan.targets[0].selected_tests == ("reset",)


def test_legacy_filter_applies_configured_skips(tmp_path: Path) -> None:
    context = replace(project(tmp_path), skipped_tests={"sim_0": ("wrap",)})
    filtered = prepare_coverage_invocation(
        CoverageInvocationRequest(("sim_0",), test_filter="r"), context
    )
    assert filtered.plan.targets[0].selected_tests == ("reset",)

from pathlib import Path
from unittest.mock import Mock

import pytest

from booley.flows.implementation_comparison import (
    ImplementationComparisonError,
    TargetExecutionRef,
    TargetPairPlan,
    baseline_execution_context,
    resolve_target_execution_ref,
    target_pair_for_candidate,
    target_pair_plans_for_handles,
    target_plan_for_handle,
)
from booley.fusesoc import fusesoc_registry
from booley.targets.catalog import TargetCatalog


def select_target(project_root, token, *, for_flow=None):
    return TargetCatalog.build(project_root).select(token, for_flow=for_flow)


def _project(tmp_path: Path) -> None:
    (tmp_path / "toy.core").write_text(
        """CAPI=2:
name: acme:lib:toy:1.0
targets:
  synth_before: {flow: generic, flow_options: {tool: yosys}}
  synth_after: {flow: generic, flow_options: {tool: yosys}}
  synth_other: {flow: generic, flow_options: {tool: yosys}}
  lint_only: {flow: lint, flow_options: {tool: verilator}}
""",
        encoding="utf-8",
    )


def test_each_candidate_is_paired_with_itself(tmp_path: Path) -> None:
    _project(tmp_path)
    after = select_target(tmp_path, "synth_after", for_flow="synth")
    other = select_target(tmp_path, "synth_other", for_flow="synth")

    plans = target_pair_plans_for_handles((after, other), flow="synth")

    assert [(plan.baseline, plan.candidate) for plan in plans] == [
        (plan.candidate, plan.candidate) for plan in plans
    ]
    assert [plan.candidate.selector for plan in plans] == [after.selector, other.selector]
    assert {plan.flow for plan in plans} == {"synth"}


def test_plan_lookup_defaults_to_the_target_paired_with_itself(tmp_path: Path) -> None:
    _project(tmp_path)
    handle = select_target(tmp_path, "synth_after", for_flow="synth")

    plan = target_plan_for_handle((), handle, flow="synth")

    assert plan.baseline == plan.candidate
    assert plan.candidate.identity == handle.identity
    (planned,) = target_pair_plans_for_handles((handle,), flow="synth")
    assert target_plan_for_handle((planned,), handle, flow="synth") is planned


def test_resolution_uses_the_planned_selector(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _project(tmp_path)
    candidate = select_target(tmp_path, "synth_after", for_flow="synth")
    (plan,) = target_pair_plans_for_handles((candidate,), flow="synth")
    resolve = Mock()
    resolve.return_value = object()
    monkeypatch.setattr(fusesoc_registry, "resolve_target_handle", resolve)

    resolve_target_execution_ref(candidate, plan.candidate, build_root=tmp_path / "build")

    assert resolve.call_args.args == (candidate,)
    assert resolve.call_args.kwargs["build_root"] == tmp_path / "build"


def test_plan_has_no_public_authority_constructor() -> None:
    ref = TargetExecutionRef("acme:lib:dut:1#synth", "synth", "acme:lib:dut:1")
    with pytest.raises(TypeError, match="created by the pair-plan factory"):
        TargetPairPlan("synth", ref, ref, object())


def test_batch_rejects_duplicate_canonical_candidates(tmp_path: Path) -> None:
    _project(tmp_path)
    first = select_target(tmp_path, "synth_after", for_flow="synth")
    second = select_target(tmp_path, "acme:lib:toy:1.0#synth_after", for_flow="synth")

    with pytest.raises(ImplementationComparisonError, match="duplicates canonical identity"):
        target_pair_plans_for_handles((first, second), flow="synth")


def test_pair_lookup_has_no_equal_target_fallback(tmp_path: Path) -> None:
    _project(tmp_path)
    handle = select_target(tmp_path, "synth_after", for_flow="synth")
    plans = target_pair_plans_for_handles((handle,), flow="synth")

    with pytest.raises(ImplementationComparisonError, match="no unique"):
        target_pair_for_candidate(plans, "acme:lib:toy:1.0#synth_other")


def test_candidate_flow_mismatch_is_rejected_by_pair_factory(tmp_path: Path) -> None:
    _project(tmp_path)
    candidate = select_target(tmp_path, "lint_only")

    with pytest.raises(ImplementationComparisonError, match="cannot be driven"):
        target_pair_plans_for_handles((candidate,), flow="synth")


def test_baseline_checkout_missing_target_is_rejected(tmp_path: Path) -> None:
    _project(tmp_path)
    handle = select_target(tmp_path, "synth_after", for_flow="synth")
    plans = target_pair_plans_for_handles((handle,), flow="synth")
    missing = tmp_path / "missing"
    missing.mkdir()

    with pytest.raises(ImplementationComparisonError, match="cannot be selected"):
        baseline_execution_context(plans, missing)


def test_baseline_checkout_identity_change_is_rejected(tmp_path: Path) -> None:
    _project(tmp_path)
    handle = select_target(tmp_path, "synth_before", for_flow="synth")
    plans = target_pair_plans_for_handles((handle,), flow="synth")
    changed = tmp_path / "changed"
    changed.mkdir()
    (changed / "changed.core").write_text(
        "CAPI=2:\n"
        "name: other:lib:toy:2.0\n"
        "targets:\n"
        "  synth_before: {flow: generic, flow_options: {tool: yosys}}\n",
        encoding="utf-8",
    )

    with pytest.raises(ImplementationComparisonError, match="expected"):
        baseline_execution_context(plans, changed)

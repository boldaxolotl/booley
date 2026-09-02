from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from booley.criteria.templates import BASELINE_TARGET_PARAM
from booley.flows.baseline_worktree import resolve_ticket_baseline
from booley.flows.implementation_comparison import (
    ImplementationComparisonError,
    TargetPairPlan,
    baseline_execution_context,
    resolve_target_execution_ref,
    target_pair_for_candidate,
    target_pair_plans_for_handles,
    target_pairs_for_candidates,
)
from booley.fusesoc import fusesoc_registry, selftest_overlay
from booley.targets.target import select_target
from booley.ticket_board.target_contract import (
    ContractParticipant,
    ContractTargetBinding,
    TargetContract,
)


def test_missing_metadata_preserves_equal_target_behavior() -> None:
    (plan,) = target_pairs_for_candidates({}, "synthesis_ok_", ["synth_default"])
    assert plan.baseline.selector == "synth_default"
    assert plan.candidate.selector == "synth_default"
    assert not plan.sealed


def test_reads_paired_baseline_from_candidate_criterion() -> None:
    criteria = {
        "synthesis_ok_synth_after": SimpleNamespace(params={BASELINE_TARGET_PARAM: "synth_before"})
    }

    (plan,) = target_pairs_for_candidates(criteria, "synthesis_ok_", ["synth_after"])
    assert plan.baseline.selector == "synth_before"
    assert plan.candidate.selector == "synth_after"


def test_invalid_persisted_baseline_fails_closed() -> None:
    criteria = {"synthesis_ok_synth_after": SimpleNamespace(params={BASELINE_TARGET_PARAM: ""})}

    with pytest.raises(ImplementationComparisonError, match="invalid baseline Target"):
        target_pairs_for_candidates(criteria, "synthesis_ok_", ["synth_after"])


def _sealed_project(tmp_path: Path, *, schema: int = 3) -> TargetContract:
    (tmp_path / "toy.core").write_text(
        """CAPI=2:
name: acme:lib:toy:1.0
targets:
  synth_before: {flow: generic, flow_options: {tool: yosys}}
  synth_after: {flow: generic, flow_options: {tool: yosys}}
  synth_other: {flow: generic, flow_options: {tool: yosys}}
  fpga_before: {flow: generic, flow_options: {tool: verilator}}
  fpga_after: {flow: generic, flow_options: {tool: verilator}}
  lint_only: {flow: lint, flow_options: {tool: verilator}}
  synth_selftest_bad:
    flow: generic
    flow_options: {tool: yosys, booley: {doctor_selftest: true}}
""",
        encoding="utf-8",
    )
    return TargetContract(
        outer_sha="a" * 40,
        project_sha="",
        surface_digest="b" * 64,
        targets=("synth_after", "synth_before"),
        bindings=(
            ContractTargetBinding(
                flow="synth",
                criterion="synthesis_ok",
                baseline="acme:lib:toy:1.0#synth_before",
                candidate="acme:lib:toy:1.0#synth_after",
                baseline_selector="synth_before" if schema >= 4 else "",
                candidate_selector="synth_after" if schema >= 4 else "",
            ),
        ),
        participants=(
            ContractParticipant(
                "outer",
                "a" * 40,
                "refs/heads/ticket",
                "refs/heads/main",
                "c" * 40,
            ),
        ),
        schema=schema,
    )


def test_schema_three_executes_selector_verified_against_sealed_pair(tmp_path: Path) -> None:
    contract = _sealed_project(tmp_path)
    criteria = {
        "synthesis_ok_synth_after": SimpleNamespace(params={BASELINE_TARGET_PARAM: "synth_before"})
    }

    pairs = target_pairs_for_candidates(
        criteria,
        "synthesis_ok_",
        ["synth_after"],
        contract=contract,
        project_root=tmp_path,
        flow="synth",
    )

    assert pairs[0].baseline.selector == "synth_before"
    assert pairs[0].candidate.selector == "synth_after"
    assert pairs[0].sealed


def test_current_schema_executes_exact_sealed_selectors(tmp_path: Path) -> None:
    contract = _sealed_project(tmp_path, schema=4)
    criteria = {
        "synthesis_ok_synth_after": SimpleNamespace(params={BASELINE_TARGET_PARAM: "synth_before"})
    }

    pairs = target_pairs_for_candidates(
        criteria,
        "synthesis_ok_",
        ["synth_after"],
        contract=contract,
        project_root=tmp_path,
        flow="synth",
    )

    assert pairs[0].baseline.selector == "synth_before"
    assert pairs[0].candidate.selector == "synth_after"
    assert pairs[0].sealed


@pytest.mark.parametrize(
    ("flow", "criterion", "baseline_name", "candidate_name"),
    [
        ("synth", "synthesis_ok", "synth_before", "synth_after"),
        ("fpga", "fpga_impl_ok", "fpga_before", "fpga_after"),
    ],
)
def test_synth_and_fpga_share_current_schema_pair_plan_contract(
    tmp_path: Path,
    flow: str,
    criterion: str,
    baseline_name: str,
    candidate_name: str,
) -> None:
    contract = _sealed_project(tmp_path, schema=4)
    vlnv = "acme:lib:toy:1.0"
    binding = ContractTargetBinding(
        flow=flow,
        criterion=criterion,
        baseline=f"{vlnv}#{baseline_name}",
        candidate=f"{vlnv}#{candidate_name}",
        baseline_selector=f"{vlnv}#{baseline_name}",
        candidate_selector=f"{vlnv}#{candidate_name}",
    )
    contract = _contract_with_binding(contract, binding)
    candidate = select_target(tmp_path, binding.candidate_selector, for_flow=flow)
    criteria = {
        f"{criterion}_{candidate_name}": SimpleNamespace(
            params={
                "target": binding.candidate,
                "_target_selector": binding.candidate_selector,
                BASELINE_TARGET_PARAM: binding.baseline_selector,
            }
        )
    }

    (plan,) = target_pair_plans_for_handles(
        criteria,
        f"{criterion}_",
        (candidate,),
        contract=contract,
        flow=flow,
    )

    assert plan.flow == flow
    assert plan.baseline.selector == binding.baseline_selector
    assert plan.candidate.selector == binding.candidate_selector


def test_current_schema_finds_authored_criterion_and_keeps_sealed_selector(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract = _sealed_project(tmp_path, schema=4)
    (tmp_path / "other.core").write_text(
        "CAPI=2:\n"
        "name: other:lib:other:1.0\n"
        "targets:\n"
        "  synth_after: {flow: generic, flow_options: {tool: yosys}}\n",
        encoding="utf-8",
    )
    binding = ContractTargetBinding(
        flow="synth",
        criterion="synthesis_ok",
        baseline="acme:lib:toy:1.0#synth_before",
        candidate="acme:lib:toy:1.0#synth_after",
        baseline_selector="acme:lib:toy:1.0#synth_before",
        candidate_selector="acme:lib:toy:1.0#synth_after",
    )
    contract = _contract_with_binding(contract, binding)
    candidate = select_target(tmp_path, binding.candidate_selector, for_flow="synth")
    assert candidate.selector == "toy#synth_after"
    criteria = {
        "synthesis_ok_authored-spelling": SimpleNamespace(
            params={
                "target": binding.candidate,
                "_target_selector": binding.candidate_selector,
                BASELINE_TARGET_PARAM: binding.baseline_selector,
            }
        )
    }

    (plan,) = target_pair_plans_for_handles(
        criteria,
        "synthesis_ok_",
        (candidate,),
        contract=contract,
        flow="synth",
    )

    assert plan.baseline.selector == binding.baseline_selector
    assert plan.candidate.selector == binding.candidate_selector
    resolve = Mock()
    resolve.return_value = object()
    monkeypatch.setattr(fusesoc_registry, "resolve_target", resolve)
    resolve_target_execution_ref(candidate, plan.candidate, build_root=tmp_path / "build")
    assert resolve.call_args.args[0] == binding.candidate_selector
    assert resolve.call_args.kwargs["vlnv"] == candidate.vlnv


def test_authored_criterion_metadata_supplies_ticket_baseline_ref(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _sealed_project(tmp_path, schema=4)
    candidate = select_target(tmp_path, "synth_after", for_flow="synth")
    criteria = {
        "synthesis_ok_authored-spelling": SimpleNamespace(
            params={
                "target": candidate.identity,
                "_target_selector": "acme:lib:toy:1.0#synth_after",
                "_baseline_ref": "sealed-base",
            }
        )
    }
    monkeypatch.setattr(
        "booley.flows.baseline_worktree.git_full_sha",
        lambda _ref, _root: "a" * 40,
    )

    baseline, full_sha, error = resolve_ticket_baseline(
        criteria,
        "synthesis_ok_",
        (candidate,),
        None,
        tmp_path,
        "synth",
    )

    assert (baseline, full_sha, error) == ("sealed-base", "a" * 40, None)


def _contract_with_binding(
    contract: TargetContract,
    binding: ContractTargetBinding,
) -> TargetContract:
    return TargetContract(
        outer_sha=contract.outer_sha,
        project_sha=contract.project_sha,
        surface_digest=contract.surface_digest,
        targets=contract.targets,
        bindings=(binding,),
        participants=contract.participants,
        schema=contract.schema,
    )


def test_plan_has_no_public_authority_constructor() -> None:
    with pytest.raises(TypeError):
        TargetPairPlan()


def test_current_schema_rejects_empty_callable_selector(tmp_path: Path) -> None:
    contract = _sealed_project(tmp_path, schema=4)
    empty_selector_binding = ContractTargetBinding(
        flow="synth",
        criterion="synthesis_ok",
        baseline="acme:lib:toy:1.0#synth_before",
        candidate="acme:lib:toy:1.0#synth_after",
        baseline_selector="",
        candidate_selector="synth_after",
    )
    contract = TargetContract(
        outer_sha=contract.outer_sha,
        project_sha=contract.project_sha,
        surface_digest=contract.surface_digest,
        targets=contract.targets,
        bindings=(empty_selector_binding,),
        participants=contract.participants,
        schema=contract.schema,
    )
    criteria = {
        "synthesis_ok_synth_after": SimpleNamespace(params={BASELINE_TARGET_PARAM: "synth_before"})
    }

    with pytest.raises(ImplementationComparisonError, match="empty callable selector"):
        target_pairs_for_candidates(
            criteria,
            "synthesis_ok_",
            ["synth_after"],
            contract=contract,
            project_root=tmp_path,
            flow="synth",
        )


def test_sealed_plan_requires_exactly_one_binding(tmp_path: Path) -> None:
    contract = _sealed_project(tmp_path, schema=4)
    contract = TargetContract(
        outer_sha=contract.outer_sha,
        project_sha=contract.project_sha,
        surface_digest=contract.surface_digest,
        targets=contract.targets,
        bindings=(*contract.bindings, *contract.bindings),
        participants=contract.participants,
        schema=contract.schema,
    )
    criteria = {
        "synthesis_ok_synth_after": SimpleNamespace(params={BASELINE_TARGET_PARAM: "synth_before"})
    }

    with pytest.raises(ImplementationComparisonError, match="no unique"):
        target_pairs_for_candidates(
            criteria,
            "synthesis_ok_",
            ["synth_after"],
            contract=contract,
            project_root=tmp_path,
            flow="synth",
        )


def test_sealed_plan_rejects_missing_binding(tmp_path: Path) -> None:
    contract = _sealed_project(tmp_path, schema=4)
    contract = TargetContract(
        outer_sha=contract.outer_sha,
        project_sha=contract.project_sha,
        surface_digest=contract.surface_digest,
        targets=contract.targets,
        bindings=(),
        participants=contract.participants,
        schema=contract.schema,
    )
    criteria = {
        "synthesis_ok_synth_after": SimpleNamespace(params={BASELINE_TARGET_PARAM: "synth_before"})
    }

    with pytest.raises(ImplementationComparisonError, match="no unique"):
        target_pairs_for_candidates(
            criteria,
            "synthesis_ok_",
            ["synth_after"],
            contract=contract,
            project_root=tmp_path,
            flow="synth",
        )


def test_batch_rejects_duplicate_canonical_candidates(tmp_path: Path) -> None:
    _sealed_project(tmp_path)
    first = select_target(tmp_path, "synth_after", for_flow="synth")
    second = select_target(tmp_path, "acme:lib:toy:1.0#synth_after", for_flow="synth")

    with pytest.raises(ImplementationComparisonError, match="duplicates canonical identity"):
        target_pair_plans_for_handles(
            {},
            "synthesis_ok_",
            (first, second),
            flow="synth",
        )


def test_pair_lookup_has_no_equal_target_fallback(tmp_path: Path) -> None:
    _sealed_project(tmp_path)
    handle = select_target(tmp_path, "synth_after", for_flow="synth")
    plans = target_pair_plans_for_handles(
        {},
        "synthesis_ok_",
        (handle,),
        flow="synth",
    )

    with pytest.raises(ImplementationComparisonError, match="no unique"):
        target_pair_for_candidate(plans, "acme:lib:toy:1.0#synth_other")


@pytest.mark.parametrize("baseline", [None, "synth_other"])
def test_schema_three_rejects_resumed_state_that_disagrees_with_contract(
    tmp_path: Path, baseline: str | None
) -> None:
    contract = _sealed_project(tmp_path)
    params = {} if baseline is None else {BASELINE_TARGET_PARAM: baseline}
    criteria = {"synthesis_ok_synth_after": SimpleNamespace(params=params)}

    with pytest.raises(ImplementationComparisonError, match="does not match the sealed"):
        target_pairs_for_candidates(
            criteria,
            "synthesis_ok_",
            ["synth_after"],
            contract=contract,
            project_root=tmp_path,
            flow="synth",
        )


def test_candidate_flow_mismatch_is_rejected_by_pair_factory(tmp_path: Path) -> None:
    _sealed_project(tmp_path)
    candidate = select_target(tmp_path, "lint_only")

    with pytest.raises(ImplementationComparisonError, match="cannot be driven"):
        target_pair_plans_for_handles({}, "synthesis_ok_", (candidate,), flow="synth")


def test_baseline_flow_mismatch_is_rejected_before_setup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _sealed_project(tmp_path)
    candidate = select_target(tmp_path, "synth_after", for_flow="synth")
    setup = Mock(side_effect=AssertionError("FuseSoC setup must not run"))
    monkeypatch.setattr(fusesoc_registry, "resolve_target", setup)
    criteria = {
        "synthesis_ok_synth_after": SimpleNamespace(params={BASELINE_TARGET_PARAM: "lint_only"})
    }

    with pytest.raises(ImplementationComparisonError, match="cannot be driven"):
        target_pair_plans_for_handles(criteria, "synthesis_ok_", (candidate,), flow="synth")
    setup.assert_not_called()


def test_baseline_checkout_missing_target_is_rejected(tmp_path: Path) -> None:
    contract = _sealed_project(tmp_path, schema=4)
    criteria = {
        "synthesis_ok_synth_after": SimpleNamespace(params={BASELINE_TARGET_PARAM: "synth_before"})
    }
    plans = target_pairs_for_candidates(
        criteria,
        "synthesis_ok_",
        ["synth_after"],
        contract=contract,
        project_root=tmp_path,
        flow="synth",
    )
    missing = tmp_path / "missing"
    missing.mkdir()

    with pytest.raises(ImplementationComparisonError, match="cannot be selected"):
        baseline_execution_context(plans, missing)


def test_baseline_checkout_identity_change_is_rejected(tmp_path: Path) -> None:
    contract = _sealed_project(tmp_path, schema=3)
    criteria = {
        "synthesis_ok_synth_after": SimpleNamespace(params={BASELINE_TARGET_PARAM: "synth_before"})
    }
    plans = target_pairs_for_candidates(
        criteria,
        "synthesis_ok_",
        ["synth_after"],
        contract=contract,
        project_root=tmp_path,
        flow="synth",
    )
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


def test_doctor_private_candidate_cannot_enter_sealed_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract = _sealed_project(tmp_path, schema=4)
    binding = ContractTargetBinding(
        flow="synth",
        criterion="synthesis_ok",
        baseline="acme:lib:toy:1.0#synth_before",
        candidate="acme:lib:toy:1.0#synth_selftest_bad",
        baseline_selector="synth_before",
        candidate_selector="synth_selftest_bad",
    )
    contract = _contract_with_binding(contract, binding)
    monkeypatch.setenv(selftest_overlay.INTERNAL_KIND_ENV, selftest_overlay.BAD_KIND)
    candidate = select_target(tmp_path, binding.candidate_selector, for_flow="synth")
    criteria = {
        "synthesis_ok_synth_selftest_bad": SimpleNamespace(
            params={BASELINE_TARGET_PARAM: "synth_before"}
        )
    }

    with pytest.raises(ImplementationComparisonError, match="Doctor-private"):
        target_pair_plans_for_handles(
            criteria,
            "synthesis_ok_",
            (candidate,),
            contract=contract,
            flow="synth",
        )

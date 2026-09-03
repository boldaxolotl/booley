from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from booley.criteria.state import DevelopmentState
from booley.criteria.templates import CriteriaTemplate
from booley.flows.sim.flow import (
    SimulateFlow,
    TargetResult,
    parse_cycle_observation,
    parse_cycles,
)
from booley.flows.sim.flow import TestResult as SimTestResult
from booley.fusesoc import fusesoc_registry
from booley.harness.models import TicketContext
from booley.harness.setup.intake import _apply_contract_selectors
from booley.mcp.base import EXIT_ERROR, McpToolResult
from booley.ticket_board.target_contract import (
    ContractParticipant,
    ContractTargetBinding,
    TargetContract,
)

_TARGET_IDENTITY = "vendor:library:core#sim_core"
_TARGET_SELECTOR = "sim_core"


@pytest.mark.parametrize(
    ("output", "status", "value"),
    [
        ("[SIM_CYCLES] smoke 42", "observed", 42),
        ("[SIM_CYCLES] other 42", "wrong_test", None),
        ("ordinary output", "missing", None),
        ("[SIM_CYCLES] smoke nope", "malformed", None),
        ("[SIM_CYCLES] 42", "legacy", 42),
    ],
)
def test_cycle_observation_preserves_evidence_status(
    output: str, status: str, value: int | None
) -> None:
    observation = parse_cycle_observation(output, "smoke")
    assert observation.status == status
    assert observation.value == value


def test_duplicate_named_cycle_records_are_ambiguous() -> None:
    observation = parse_cycle_observation("[SIM_CYCLES] smoke 41\n[SIM_CYCLES] smoke 42", "smoke")
    assert observation.status == "duplicate"
    assert observation.value is None


def test_configured_prefix_is_a_literal() -> None:
    observation = parse_cycle_observation("CYCLES[1] smoke 9", "smoke", ["CYCLES[1]"])
    assert observation.status == "observed"
    assert observation.value == 9


def test_legacy_wrapper_remains_observationally_compatible() -> None:
    assert parse_cycles("[SIM_CYCLES] 987", "smoke") == 987
    assert parse_cycles("[SIM_CYCLES] 987", "smoke", allow_legacy=False) is None


def _criterion_flow(*, relative: bool = False) -> tuple[SimulateFlow, str]:
    threshold = {"cycle_count_reduce_at_least": "5%"} if relative else {"cycle_count_max": 100}
    template = CriteriaTemplate.from_yaml(
        {"mandatory": {"cycle_count": [{"target": "sim_core", "test": "coremark", **threshold}]}}
    )
    state = DevelopmentState()
    state.init_criteria(template.expand([]), criterion_params=template.expand_params([]))
    flow = object.__new__(SimulateFlow)
    flow._state = state
    flow._args = MagicMock(state_file="state.json", test=None, work_dir=".")
    flow._target_handles = {
        "sim_core": MagicMock(
            identity="sim_core",
            selector="sim_core",
            project_root=Path().resolve(),
        )
    }
    flow.set_criterion = MagicMock()
    return flow, next(iter(state.criteria))


def _sealed_contract() -> TargetContract:
    return TargetContract(
        outer_sha="a" * 40,
        project_sha=None,
        surface_digest="b" * 64,
        targets=(_TARGET_IDENTITY,),
        bindings=(
            ContractTargetBinding(
                flow="sim",
                criterion="cycle_count",
                baseline=_TARGET_IDENTITY,
                candidate=_TARGET_IDENTITY,
                baseline_selector=_TARGET_SELECTOR,
                candidate_selector=_TARGET_SELECTOR,
            ),
        ),
        participants=(
            ContractParticipant(
                role="outer",
                sealed_sha="a" * 40,
                ticket_ref="refs/heads/ticket",
                destination_ref="refs/heads/main",
                destination_sha="c" * 40,
            ),
        ),
    )


def _sealed_criterion_flow(*, relative: bool = False) -> tuple[SimulateFlow, str]:
    threshold = {"cycle_count_reduce_at_least": "5%"} if relative else {"cycle_count_max": 100}
    template = CriteriaTemplate.from_yaml(
        {
            "mandatory": {
                "cycle_count": [{"target": _TARGET_SELECTOR, "test": "coremark", **threshold}]
            }
        }
    )
    expanded = template.expand([])
    params = template.expand_params([])
    context = TicketContext(
        slug="qualified-cycle-target",
        ticket_path=Path("ticket.md"),
        ticket_type="bugfix",
        branch="main",
        summary="Qualified Cycle Count Target",
        project_root=Path(),
        target_contract=_sealed_contract(),
    )
    _apply_contract_selectors(context, expanded, params)
    state = DevelopmentState()
    state.init_criteria(expanded, criterion_params=params)
    flow = object.__new__(SimulateFlow)
    flow._state = state
    flow._args = MagicMock(state_file="state.json", test=None, work_dir=".")
    flow._target_handles = {
        _TARGET_SELECTOR: MagicMock(
            identity=_TARGET_IDENTITY,
            selector=_TARGET_SELECTOR,
            project_root=Path().resolve(),
        )
    }
    flow.set_criterion = MagicMock()
    return flow, next(iter(state.criteria))


def _pin_baseline(flow: SimulateFlow, ref: str = "a" * 40) -> None:
    for entry in flow.state.criteria.values():
        entry.params["_baseline_ref"] = ref


def test_absolute_cycle_criterion_does_not_select_a_baseline(monkeypatch) -> None:
    flow, _key = _criterion_flow()
    resolve = MagicMock()
    monkeypatch.setattr("booley.flows.sim.flow.git_full_sha", resolve)

    assert flow._cycle_baseline_selection(["sim_core"]) == (None, [], None)
    resolve.assert_not_called()


def test_relative_cycle_criteria_share_one_resolved_baseline(monkeypatch) -> None:
    flow, _key = _criterion_flow(relative=True)
    _pin_baseline(flow)
    monkeypatch.setattr("booley.flows.sim.flow.git_full_sha", lambda *_args: "b" * 40)

    assert flow._cycle_baseline_selection(["sim_core"]) == (
        "b" * 40,
        ["sim_core"],
        None,
    )


def test_schema_four_relative_cycle_criterion_selects_callable_target(monkeypatch) -> None:
    flow, _key = _sealed_criterion_flow(relative=True)
    _pin_baseline(flow)
    monkeypatch.setattr("booley.flows.sim.flow.git_full_sha", lambda *_args: "b" * 40)

    assert flow._cycle_baseline_selection(["sim_core"]) == (
        "b" * 40,
        ["sim_core"],
        None,
    )


def test_invalid_cycle_baseline_ref_is_actionable(monkeypatch) -> None:
    flow, _key = _criterion_flow(relative=True)
    _pin_baseline(flow, "missing")
    monkeypatch.setattr("booley.flows.sim.flow.git_full_sha", lambda *_args: None)

    ref, targets, error = flow._cycle_baseline_selection(["sim_core"])

    assert ref == "missing"
    assert targets == ["sim_core"]
    assert "cannot be resolved" in error


def test_baseline_execution_uses_ephemeral_tree_and_restores_current_tree(monkeypatch) -> None:
    flow, _key = _criterion_flow(relative=True)
    _pin_baseline(flow)
    current = Path("/current")
    baseline = Path("/baseline")
    flow._args.work_dir = current
    flow._target_handles["sim_core"].project_root = current
    flow._tb_top_for_target = MagicMock(return_value="tb_top")
    flow._run_target = MagicMock(
        return_value=TargetResult(target="sim_core", passed=True, tests=[])
    )
    flow._attach_workload_snapshots = MagicMock()
    monkeypatch.setattr("booley.flows.sim.flow.git_full_sha", lambda *_args: "b" * 40)
    monkeypatch.setattr(
        "booley.flows.sim.flow.select_target",
        lambda root, *_args, **_kwargs: MagicMock(
            identity="sim_core",
            selector="sim_core",
            project_root=Path(root).resolve(),
        ),
    )

    @contextmanager
    def fake_worktree(_root, _ref):
        yield baseline

    monkeypatch.setattr("booley.flows.sim.flow.baseline_worktree", fake_worktree)

    result = flow._run_cycle_count_baselines(["sim_core"], {"sim_core": ["coremark"]})

    assert result["sim_core"].passed is True
    assert flow.args.work_dir == current
    flow._run_target.assert_called_once_with("sim_core", "tb_top", {"sim_core": ["coremark"]}, [])


def test_schema_four_baseline_results_are_keyed_by_identity(monkeypatch) -> None:
    identity = _TARGET_IDENTITY
    flow, _key = _sealed_criterion_flow(relative=True)
    _pin_baseline(flow)
    current = Path("/current")
    baseline = Path("/baseline")
    flow._args.work_dir = current
    flow._target_handles["sim_core"].project_root = current
    flow._tb_top_for_target = MagicMock(return_value="tb_top")
    flow._run_target = MagicMock(
        return_value=TargetResult(
            target="sim_core",
            passed=True,
            tests=[],
        )
    )
    flow._attach_workload_snapshots = MagicMock()
    monkeypatch.setattr("booley.flows.sim.flow.git_full_sha", lambda *_args: "b" * 40)
    monkeypatch.setattr(
        "booley.flows.sim.flow.select_target",
        lambda *_args, **_kwargs: MagicMock(
            identity=identity,
            selector="sim_core",
            project_root=baseline,
        ),
    )

    @contextmanager
    def fake_worktree(_root, _ref):
        yield baseline

    monkeypatch.setattr("booley.flows.sim.flow.baseline_worktree", fake_worktree)

    result = flow._run_cycle_count_baselines(["sim_core"], {"sim_core": ["coremark"]})

    assert set(result) == {identity}
    assert result[identity].target_identity == identity


def test_schema_four_baseline_rejects_selector_identity_drift(monkeypatch) -> None:
    flow, _key = _sealed_criterion_flow(relative=True)
    _pin_baseline(flow)
    current = Path("/current")
    baseline = Path("/baseline")
    flow._args.work_dir = current
    flow._target_handles["sim_core"].project_root = current
    flow._tb_top_for_target = MagicMock(return_value="tb_top")
    flow._run_target = MagicMock()
    monkeypatch.setattr("booley.flows.sim.flow.git_full_sha", lambda *_args: "b" * 40)
    monkeypatch.setattr(
        "booley.flows.sim.flow.select_target",
        lambda *_args, **_kwargs: MagicMock(
            identity="other:library:core#sim_core",
            selector="sim_core",
            project_root=baseline,
        ),
    )

    @contextmanager
    def fake_worktree(_root, _ref):
        yield baseline

    monkeypatch.setattr("booley.flows.sim.flow.baseline_worktree", fake_worktree)

    result = flow._run_cycle_count_baselines(["sim_core"], {"sim_core": ["coremark"]})

    assert isinstance(result, McpToolResult)
    assert result.exit_code == EXIT_ERROR
    assert "resolves to 'other:library:core#sim_core'" in result.report_text
    flow._run_target.assert_not_called()


def test_schema_four_baseline_reports_ambiguous_selector(monkeypatch) -> None:
    flow, _key = _sealed_criterion_flow(relative=True)
    _pin_baseline(flow)
    current = Path("/current")
    baseline = Path("/baseline")
    flow._args.work_dir = current
    flow._target_handles[_TARGET_SELECTOR].project_root = current
    flow._run_target = MagicMock()
    monkeypatch.setattr("booley.flows.sim.flow.git_full_sha", lambda *_args: "b" * 40)

    def ambiguous_target(*_args, **_kwargs):
        raise fusesoc_registry.AmbiguousTargetError("sim_core is ambiguous")

    monkeypatch.setattr("booley.flows.sim.flow.select_target", ambiguous_target)

    @contextmanager
    def fake_worktree(_root, _ref):
        yield baseline

    monkeypatch.setattr("booley.flows.sim.flow.baseline_worktree", fake_worktree)

    result = flow._run_cycle_count_baselines([_TARGET_SELECTOR], {_TARGET_SELECTOR: ["coremark"]})

    assert isinstance(result, McpToolResult)
    assert result.exit_code == EXIT_ERROR
    assert "sim_core is ambiguous" in result.report_text
    flow._run_target.assert_not_called()


def test_cycle_criterion_grades_named_test_independently_from_target() -> None:
    flow, key = _criterion_flow()
    result = TargetResult(
        target="sim_core",
        passed=False,
        tests=[
            SimTestResult(name="coremark", passed=True, cycles=95, cycle_status="observed"),
            SimTestResult(name="other", passed=False),
        ],
    )

    flow._record_cycle_count_criteria(result)

    flow.set_criterion.assert_called_once()
    assert flow.set_criterion.call_args.args == (key, True)
    assert flow.set_criterion.call_args.kwargs["detail"]["cycles"] == 95


def test_schema_four_cycle_criterion_grades_selector_evidence() -> None:
    flow, key = _sealed_criterion_flow()
    result = TargetResult(
        target=_TARGET_SELECTOR,
        target_identity=_TARGET_IDENTITY,
        passed=True,
        tests=[SimTestResult(name="coremark", passed=True, cycles=95, cycle_status="observed")],
    )

    flow._record_cycle_count_criteria(result)

    assert flow.set_criterion.call_args.args == (key, True)
    assert flow.set_criterion.call_args.kwargs["detail"]["target"] == _TARGET_SELECTOR
    assert flow.set_criterion.call_args.kwargs["detail"]["target_identity"] == _TARGET_IDENTITY


@pytest.mark.parametrize(
    ("identity", "selector"),
    [
        ("other:library:core#sim_core", "other#sim_core"),
        (_TARGET_IDENTITY, "other#sim_core"),
        ("other:library:core#sim_core", "sim_core"),
    ],
)
def test_schema_four_cycle_criterion_rejects_mismatched_target_binding(
    identity: str,
    selector: str,
) -> None:
    flow, _key = _sealed_criterion_flow()
    result = TargetResult(
        target=selector,
        target_identity=identity,
        passed=True,
        tests=[SimTestResult(name="coremark", passed=True, cycles=95, cycle_status="observed")],
    )

    flow._record_cycle_count_criteria(result)

    flow.set_criterion.assert_not_called()


@pytest.mark.parametrize("status", ["missing", "duplicate", "malformed", "legacy"])
def test_cycle_criterion_fails_closed_on_inadmissible_record(status: str) -> None:
    flow, key = _criterion_flow()
    result = TargetResult(
        target="sim_core",
        passed=True,
        tests=[SimTestResult(name="coremark", passed=True, cycle_status=status)],
    )

    flow._record_cycle_count_criteria(result)

    assert flow.set_criterion.call_args.args == (key, False)
    assert status in flow.set_criterion.call_args.kwargs["detail"]["reason"]


def test_relative_cycle_criterion_requires_passing_baseline_test() -> None:
    flow, key = _criterion_flow(relative=True)
    flow._baseline_results = {
        "sim_core": TargetResult(
            target="sim_core",
            passed=False,
            tests=[
                SimTestResult(name="coremark", passed=False, cycles=100, cycle_status="observed")
            ],
        )
    }
    result = TargetResult(
        target="sim_core",
        passed=True,
        tests=[SimTestResult(name="coremark", passed=True, cycles=90, cycle_status="observed")],
    )

    flow._record_cycle_count_criteria(result)

    assert flow.set_criterion.call_args.args == (key, False)
    assert "baseline test did not pass" in flow.set_criterion.call_args.kwargs["detail"]["reason"]


def test_schema_four_relative_cycle_criterion_joins_baseline_by_identity() -> None:
    identity = _TARGET_IDENTITY
    flow, key = _sealed_criterion_flow(relative=True)
    flow._baseline_results = {
        identity: TargetResult(
            target="sim_core",
            target_identity=identity,
            passed=True,
            tests=[
                SimTestResult(
                    name="coremark",
                    passed=True,
                    cycles=100,
                    cycle_status="observed",
                )
            ],
        )
    }
    result = TargetResult(
        target="sim_core",
        target_identity=identity,
        passed=True,
        tests=[SimTestResult(name="coremark", passed=True, cycles=90, cycle_status="observed")],
    )

    flow._record_cycle_count_criteria(result)

    assert flow.set_criterion.call_args.args == (key, True)

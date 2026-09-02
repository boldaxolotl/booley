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
    flow._tb_top_for_target = MagicMock(return_value="tb_top")
    flow._run_target = MagicMock(
        return_value=TargetResult(target="sim_core", passed=True, tests=[])
    )
    flow._attach_workload_snapshots = MagicMock()
    monkeypatch.setattr("booley.flows.sim.flow.git_full_sha", lambda *_args: "b" * 40)

    @contextmanager
    def fake_worktree(_root, _ref):
        yield baseline

    monkeypatch.setattr("booley.flows.sim.flow.baseline_worktree", fake_worktree)

    result = flow._run_cycle_count_baselines(["sim_core"], {"sim_core": ["coremark"]})

    assert result["sim_core"].passed is True
    assert flow.args.work_dir == current
    flow._run_target.assert_called_once_with("sim_core", "tb_top", {"sim_core": ["coremark"]}, [])


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

"""INTERIM (#1233 stage S2): Test Runs no Goal can name record no Goal evidence.

Delete this file with ``SimulateFlow._apply_interim_goal_guard`` in stage S3,
when Goals name Test Variants and seeds and evidence matches per Test Run.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from booley.flows import endpoint_acceptance
from booley.flows.endpoint_admission import authorize_simulation_targets
from booley.runtime.endpoint_execution import EndpointOutcome
from tests.flows.sim.test_flow_test_run_resolution import _MAP, _flow


@pytest.mark.parametrize(
    ("argv", "names"),
    [
        (("--test", "stress+soak"), ("sim: stress+soak@1",)),
        (("--test", "stress@7"), ("sim: stress+short@7",)),
        (("--seed", "7"), ("sim: stress+short@7",)),
        (("--test", "stress", "--test", "stress+soak@3"), ("sim: stress+soak@3",)),
    ],
)
def test_any_non_default_test_run_makes_the_invocation_diagnostic(
    tmp_path, monkeypatch, argv, names
):
    flow = _flow(tmp_path, monkeypatch, *argv)
    assert flow.args.diagnostic is False

    assert flow._validate_test_plans(["sim"], _MAP) is None

    assert flow.args.diagnostic is True
    result = flow._with_test_run_guard_note(EndpointOutcome(report_text="sim: 1/1 passed"))
    assert result.detail["test_run_goal_guard"] == {"diagnostic": True, "test_runs": list(names)}
    assert result.report_text.splitlines() == [
        "sim: 1/1 passed",
        "Diagnostic: this run includes Test Runs other than the default Test Variant at "
        f"the Default Seed ({', '.join(names)}); it records no Goal evidence.",
    ]


def test_count_selector_is_diagnostic_and_lists_a_bounded_preview(tmp_path, monkeypatch):
    flow = _flow(tmp_path, monkeypatch, "--test", "stress@x12")
    assert flow._validate_test_plans(["sim"], _MAP) is None
    assert flow.args.diagnostic is True
    note = flow._with_test_run_guard_note(EndpointOutcome()).report_text
    assert note.count("sim: stress+short@") == 8
    assert ", and 4 more); it records no Goal evidence." in note


@pytest.mark.parametrize(
    ("argv", "normalized"),
    [
        ((), None),
        (("--test", "stress"), ("stress",)),
        (("--test", "stress+short@1", "--test", "reset"), ("stress", "reset")),
        (("--test", "stress", "--seed", "1"), ("stress",)),
    ],
)
def test_default_test_runs_keep_recording_evidence_by_bare_test_name(
    tmp_path, monkeypatch, argv, normalized
):
    flow = _flow(tmp_path, monkeypatch, *argv)

    assert flow._validate_test_plans(["sim"], _MAP) is None

    assert flow.args.diagnostic is False
    assert flow.args.test == normalized
    result = EndpointOutcome(report_text="sim: 1/1 passed")
    assert flow._with_test_run_guard_note(result).report_text == "sim: 1/1 passed"
    assert "test_run_goal_guard" not in result.detail


def test_a_guarded_invocation_records_no_criterion_and_needs_no_bound_goal(tmp_path, monkeypatch):
    """The guard works through the one flag every evidence consumer honours."""
    flow = _flow(tmp_path, monkeypatch, "--test", "stress+soak")
    flow._validate_test_plans(["sim"], _MAP)
    recorded: list[str] = []
    state = SimpleNamespace(
        strict_criteria=True,
        _file_path=tmp_path / "state.json",
        set_criterion=lambda key, *_args, **_kwargs: recorded.append(key),
    )
    endpoint = SimpleNamespace(
        args=flow.args,
        state=state,
        _bound_criterion_keys_for_target=lambda _target: [],
        _criterion_key_for_source=lambda key, _source: key,
    )

    endpoint_acceptance.set_criterion(endpoint, "sim_pass_sim", True)
    refusal = authorize_simulation_targets(endpoint, (SimpleNamespace(selector="sim"),))

    assert recorded == []
    assert refusal is None

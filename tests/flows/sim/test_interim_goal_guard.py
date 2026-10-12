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


def test_failing_runs_of_a_guarded_invocation_are_listed_as_diagnostic_failures(
    tmp_path, monkeypatch
):
    flow = _flow(tmp_path, monkeypatch, "--test", "stress+soak@3", "--test", "stress")
    flow._validate_test_plans(["sim"], _MAP)
    rows = [
        {"name": "stress+soak@3", "grade": "fail"},
        {"name": "stress+short@1", "grade": "pass"},
        {"name": "reset@1", "grade": "fail"},
    ]
    result = EndpointOutcome(
        report_text="sim: FAIL", detail={"campaigns": {"sim": {"test_runs": rows}}}
    )

    lines = flow._with_test_run_guard_note(result).report_text.splitlines()

    assert lines[0] == "sim: FAIL"
    assert lines[1].startswith("Diagnostic: this run includes Test Runs other than")
    assert lines[2:] == [
        "Diagnostic failures (recorded as no Goal evidence):",
        "  sim: stress+soak@3",
        "  sim: reset@1",
    ]
    assert result.detail["test_run_goal_guard"]["diagnostic_failures"] == [
        "sim: stress+soak@3",
        "sim: reset@1",
    ]


def test_a_default_invocation_has_no_diagnostic_failures_heading(tmp_path, monkeypatch):
    flow = _flow(tmp_path, monkeypatch)
    flow._validate_test_plans(["sim"], _MAP)
    rows = [{"name": "stress+short@1", "grade": "fail"}]
    result = EndpointOutcome(
        report_text="sim: FAIL", detail={"campaigns": {"sim": {"test_runs": rows}}}
    )
    assert flow._with_test_run_guard_note(result).report_text == "sim: FAIL"


@pytest.mark.parametrize("mode", [(), ("--coverage",)])
@pytest.mark.parametrize(
    "tests",
    [("stress", "stress+soak"), ("stress@x2",), ("stress@1", "stress@2"), ("stress+soak@x3",)],
)
def test_several_test_runs_of_one_test_always_make_the_invocation_diagnostic(
    tmp_path, monkeypatch, mode, tests
):
    """At most one Test Run of a Test is its default, so the guard always fires.

    Criteria name bare Tests, and acceptance refuses to judge a Test from
    several of its Test Runs (``SeveralTestRunsOfOneTestError``). The guard is
    what keeps that refusal unreachable for simulation, Coverage and Cycle
    Count requests alike: Cycle Count Criteria are judged from the same
    invocation, so they have no request form of their own.
    """
    argv = [word for test in tests for word in ("--test", test)]
    flow = _flow(tmp_path, monkeypatch, *mode, *argv)

    assert flow._validate_test_plans(["sim"], _MAP) is None

    runs = flow._resolve_test_runs("sim", _MAP)
    assert len(runs) > len({run.test for run in runs})
    assert flow.args.diagnostic is True


def test_a_diagnostic_campaign_is_never_judged_per_test(tmp_path):
    """Simulation, Coverage and Cycle Count Criteria all sit behind one gate."""
    from booley.flows.sim.acceptance import (
        AcceptanceContext,
        SeveralTestRunsOfOneTestError,
        SimulationAcceptanceCoordinator,
    )
    from tests.flows.sim.test_campaign_test_runs import _FAULT, _SOAK
    from tests.flows.sim.test_test_run_evidence_rows import _outcome, _state

    outcome = _outcome(tmp_path, _SOAK, _FAULT)
    state = _state(tmp_path, outcome, cycle_test="count")
    state.save()

    def context(diagnostic: bool) -> AcceptanceContext:
        recorder = SimpleNamespace()
        return AcceptanceContext(
            "ticket",
            {},
            "0" * 32,
            state,
            recorder,
            "1",
            diagnostic=diagnostic,  # type: ignore[arg-type]
        )

    guarded = SimulationAcceptanceCoordinator().reconcile(outcome, context(True))
    assert (guarded.committed, guarded.changes, guarded.reason) == (False, (), "no_criteria")
    # Without the guard the same campaign is refused with the typed error.
    with pytest.raises(SeveralTestRunsOfOneTestError):
        SimulationAcceptanceCoordinator().reconcile(outcome, context(False))


def test_a_resumed_campaign_is_guarded_by_the_test_runs_its_manifest_froze(tmp_path, monkeypatch):
    """Resume runs no request, so the guard reads the manifest's Test Runs."""
    from booley.flows.test_runs import build_test_run
    from tests.flows.sim.test_campaign_test_runs import _hdl_plan

    default = build_test_run("stress", variant="short", seed=1)
    soak = build_test_run("stress", variant="soak", seed=1, args=("+iters=9",))
    required = ("reset", "stress")

    flow = _flow(tmp_path, monkeypatch)
    flow._apply_interim_goal_guard_to_resume(_hdl_plan(tmp_path, ((default,),), required).manifest)
    assert flow.args.diagnostic is False

    (tmp_path / "other").mkdir()
    manifest = _hdl_plan(tmp_path / "other", ((default,), (soak,)), required).manifest
    flow._apply_interim_goal_guard_to_resume(manifest)
    assert flow.args.diagnostic is True
    selector = manifest.document["target"]["selector"]
    assert flow._test_run_guard_names == (f"{selector}: stress+soak@1",)

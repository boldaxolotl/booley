"""INTERIM (#1233 stage S2): Test Runs no Goal can name record no Goal evidence.

Delete this file with ``EvidenceGuard`` and the ``SimulateFlow`` methods that
decide and enforce it in stage S3, when Goals name Test Variants and seeds and
evidence matches per Test Run.

A guarded invocation withholds its evidence. That is its own state, not the
user's ``--diagnostic`` flag: it grants none of that flag's privileges.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import pytest

from booley.flows import endpoint_acceptance
from booley.flows.endpoint_admission import authorize_simulation_targets
from booley.runtime.endpoint_execution import EndpointOutcome
from booley.targets.domain import TargetHandle
from tests.flows.sim.test_flow_test_run_resolution import _MAP, _flow


def _guard(flow, targets=("sim",)):
    """Decide and enforce the guard the way every entry point does."""
    guard = flow._request_evidence_guard(list(targets), _MAP)
    flow._enforce_evidence_guard(guard)
    return guard


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
    assert flow.context.records_no_evidence is False

    assert flow._validate_test_plans(["sim"], _MAP) is None
    # Validation decides nothing about evidence and changes nothing.
    assert flow.context.records_no_evidence is False
    assert _guard(flow).other_test_runs == names

    assert flow.context.records_no_evidence is True
    # Withholding evidence is not the user's flag.
    assert flow.args.diagnostic is False
    result = flow._with_test_run_guard_note(EndpointOutcome(report_text="sim: 1/1 passed"))
    assert result.detail["test_run_goal_guard"] == {"diagnostic": True, "test_runs": list(names)}
    assert result.report_text.splitlines() == [
        "sim: 1/1 passed",
        "Diagnostic: this run includes Test Runs other than the default Test Variant at "
        f"the Default Seed ({', '.join(names)}); it records no Goal evidence.",
    ]


def test_count_selector_is_diagnostic_and_lists_a_bounded_preview(tmp_path, monkeypatch):
    flow = _flow(tmp_path, monkeypatch, "--test", "stress@x12")
    _guard(flow)
    assert flow.context.records_no_evidence is True
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
    requested = flow.args.test

    guard = flow._request_evidence_guard(["sim"], _MAP)
    # Deciding changes nothing; enforcing reduces --test to bare Test names.
    assert flow.args.test == requested
    flow._enforce_evidence_guard(guard)

    assert flow.context.records_no_evidence is False
    assert flow.args.test == normalized
    result = EndpointOutcome(report_text="sim: 1/1 passed")
    assert flow._with_test_run_guard_note(result).report_text == "sim: 1/1 passed"
    assert "test_run_goal_guard" not in result.detail


def _strict_state(flow, tmp_path, recorded: list[str]) -> None:
    flow.context._state = SimpleNamespace(
        strict_criteria=True,
        criteria={},
        _file_path=tmp_path / "state.json",
        set_criterion=lambda key, *_args, **_kwargs: recorded.append(key),
    )


def test_a_guarded_invocation_records_no_criterion(tmp_path, monkeypatch):
    flow = _flow(tmp_path, monkeypatch, "--test", "stress+soak")
    _guard(flow)
    recorded: list[str] = []
    _strict_state(flow, tmp_path, recorded)

    endpoint_acceptance.set_criterion(flow.context, "sim_pass_sim", True)

    assert recorded == []


def test_a_guarded_invocation_gains_no_diagnostic_privilege(tmp_path, monkeypatch):
    """Strict Target authorization still applies: only --diagnostic lifts it."""
    handle = cast(TargetHandle, SimpleNamespace(selector="sim", identity="acme:lib:demo:1#sim"))
    guarded = _flow(tmp_path, monkeypatch, "--test", "stress+soak")
    _guard(guarded)
    _strict_state(guarded, tmp_path, [])
    monkeypatch.setattr(guarded.context, "_bound_criterion_keys_for_target", lambda _target: [])

    refusal = authorize_simulation_targets(guarded.context, (handle,))

    assert refusal is not None
    assert refusal.report_text == (
        "sim: strict Ticket has no authorized Criterion binding for Target(s): sim"
    )

    asked = _flow(tmp_path, monkeypatch, "--test", "stress+soak", "--diagnostic")
    _guard(asked)
    _strict_state(asked, tmp_path, [])
    monkeypatch.setattr(asked.context, "_bound_criterion_keys_for_target", lambda _target: [])
    assert authorize_simulation_targets(asked.context, (handle,)) is None


def test_a_guarded_coverage_run_still_needs_diagnostic_for_no_waivers(tmp_path, monkeypatch):
    """Coverage preflight sees the user's flag, so its --no-waivers rule still applies."""
    from booley.flows.sim import coverage_flow_context, coverage_invocation

    flow = _flow(tmp_path, monkeypatch, "--coverage", "--no-waivers", "--test", "stress+soak")
    _guard(flow)
    assert flow.context.records_no_evidence is True
    requests = []

    def prepare(request, _context, *, selection):
        del selection
        requests.append(request)
        return SimpleNamespace(plan=object(), findings=())

    monkeypatch.setattr(coverage_invocation, "prepare_coverage_invocation", prepare)
    monkeypatch.setattr(coverage_flow_context, "coverage_project_context", lambda *_args: None)
    monkeypatch.setattr(flow, "_selected_target_selection", lambda: SimpleNamespace(handles=()))
    monkeypatch.setattr(flow, "_flow_enabled", lambda: True)

    assert flow._prepare_coverage() is None

    (request,) = requests
    assert (request.no_waivers, request.diagnostic) == (True, False)


def test_an_unresolvable_target_fails_closed(tmp_path, monkeypatch):
    """Nothing shows the Target runs defaults, so the run records no evidence."""
    from booley.fusesoc import fusesoc_registry

    flow = _flow(tmp_path, monkeypatch, "--test", "stress")

    def unresolvable(_target):
        raise fusesoc_registry.FuseSocError("no core declares Target 'sim'")

    monkeypatch.setattr(flow, "_target_test_plan", unresolvable)
    # Validation leaves the Target's own error to the run path ...
    assert flow._validate_test_plans(["sim"], _MAP) is None
    # ... and the guard withholds the evidence meanwhile.
    guard = _guard(flow)

    assert guard.unresolved == ("sim: no core declares Target 'sim'",)
    assert flow.context.records_no_evidence is True
    assert flow.args.test == ("stress",)
    recorded: list[str] = []
    _strict_state(flow, tmp_path, recorded)
    endpoint_acceptance.set_criterion(flow.context, "sim_pass_sim", True)
    assert recorded == []
    result = flow._with_test_run_guard_note(EndpointOutcome(report_text="sim: 1/1 passed"))
    assert result.detail["test_run_goal_guard"] == {
        "diagnostic": True,
        "test_runs": [],
        "unresolved": ["sim: no core declares Target 'sim'"],
    }
    assert result.report_text.splitlines()[1] == (
        "Diagnostic: the Test Runs of this run could not be resolved "
        "(sim: no core declares Target 'sim'); it records no Goal evidence."
    )


def test_the_direct_coverage_path_applies_the_guard(tmp_path, monkeypatch):
    flow = _flow(tmp_path, monkeypatch, "--coverage", "--test", "stress+soak")
    monkeypatch.setattr("booley.flows.sim.flow._get_test_names", lambda _work_dir: dict(_MAP))
    seen: list[bool] = []

    def prepare():
        seen.append(flow.context.records_no_evidence)
        return EndpointOutcome(exit_code=2, report_text="stop after the guard")

    monkeypatch.setattr(flow, "_prepare_coverage", prepare)

    flow._run_coverage()

    assert seen == [True]
    assert flow._test_runs.guard.other_test_runs == ("sim: stress+soak@1",)


def test_failing_runs_of_a_guarded_invocation_are_listed_as_diagnostic_failures(
    tmp_path, monkeypatch
):
    flow = _flow(tmp_path, monkeypatch, "--test", "stress+soak@3", "--test", "stress")
    _guard(flow)
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
    _guard(flow)
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

    _guard(flow)

    runs = flow._resolve_test_runs("sim", _MAP)
    assert len(runs) > len({run.test for run in runs})
    assert flow.context.records_no_evidence is True


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
    guard = flow._resume_evidence_guard(_hdl_plan(tmp_path, ((default,),), required).manifest)
    assert guard.withholds_evidence is False

    (tmp_path / "other").mkdir()
    manifest = _hdl_plan(tmp_path / "other", ((default,), (soak,)), required).manifest
    guard = flow._resume_evidence_guard(manifest)
    flow._enforce_evidence_guard(guard)
    assert flow.context.records_no_evidence is True
    assert flow.args.diagnostic is False
    selector = manifest.document["target"]["selector"]
    assert guard.other_test_runs == (f"{selector}: stress+soak@1",)


def test_a_resumed_campaign_whose_target_plan_fails_records_no_evidence(tmp_path, monkeypatch):
    from booley.flows.test_runs import build_test_run
    from tests.flows.sim.test_campaign_test_runs import _hdl_plan

    default = build_test_run("stress", variant="short", seed=1)
    manifest = _hdl_plan(tmp_path, ((default,),), ("reset", "stress")).manifest
    flow = _flow(tmp_path, monkeypatch)

    def broken(_target):
        raise ValueError("tests.toml no longer loads")

    monkeypatch.setattr(flow, "_target_test_plan", broken)
    guard = flow._resume_evidence_guard(manifest)

    assert guard.withholds_evidence is True
    assert guard.unresolved[0].endswith(": tests.toml no longer loads")

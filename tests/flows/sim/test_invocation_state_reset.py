"""One Flow object, several invocations: Test Run decisions never carry over (ADR 0072).

Resolved Test Runs (drawn seeds included), the baseline runs and the evidence
guard belong to one request. They restart wherever an invocation starts: a
new session, a newly parsed request, and the preparation of an execution.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from booley.flows.execution_persistence import StandaloneFlowExecution
from booley.flows.flow_session import FlowSession
from booley.flows.sim.test_run_invocation import TestRunInvocation
from booley.runtime.endpoint_execution import EndpointOutcome
from tests.flows.sim.test_flow_test_run_resolution import _MAP, _flow


def _names(flow) -> list[str]:
    return [run.name for run in flow._resolve_test_runs("sim", _MAP)]


def _decide(flow) -> tuple[str, ...]:
    guard = flow._request_evidence_guard(["sim"], _MAP)
    flow._enforce_evidence_guard(guard)
    return guard.other_test_runs


def test_a_newly_parsed_request_resolves_its_own_test_runs(tmp_path, monkeypatch):
    flow = _flow(tmp_path, monkeypatch, "--test", "stress+soak@9")
    assert _names(flow) == ["stress+soak@9"]
    assert _decide(flow) == ("sim: stress+soak@9",)
    assert flow._test_runs.guard.withholds_evidence

    flow.parse_args(["--work-dir", str(tmp_path), "--target", "sim", "--test", "stress"])

    assert flow._test_runs == TestRunInvocation()
    assert _names(flow) == ["stress+short@1"]
    assert _decide(flow) == ()
    assert not flow._test_runs.guard.withholds_evidence


def test_random_seeds_are_drawn_again_for_the_next_invocation(tmp_path, monkeypatch):
    argv = ["--work-dir", str(tmp_path), "--target", "sim", "--seed", "random", "--test", "stress"]
    flow = _flow(tmp_path, monkeypatch, "--seed", "random", "--test", "stress")
    first = flow._resolve_test_runs("sim", _MAP)
    assert flow._resolve_test_runs("sim", _MAP) is first

    draws = iter([(4242,)])
    monkeypatch.setattr(
        "booley.flows.target_test_plan.draw_seeds", lambda *_args, **_kwargs: next(draws)
    )
    flow.parse_args(argv)
    plan = flow._target_test_plan("sim")
    monkeypatch.setattr(
        flow,
        "_registered_test_plan",
        lambda *_args: replace(plan, tests=("reset", "stress"), skipped=("reset",)),
    )

    second = flow._resolve_test_runs("sim", _MAP)
    assert second is not first
    assert flow._test_runs.resolved == {(str(tmp_path), "sim"): second}


@pytest.mark.parametrize("boundary", ["new_session", "prepare_execution"])
def test_every_invocation_boundary_starts_the_state_afresh(tmp_path, monkeypatch, boundary):
    flow = _flow(tmp_path, monkeypatch, "--test", "stress+soak@9")
    _names(flow)
    _decide(flow)
    stale = flow._test_runs
    request = replace(flow.args, test=("stress",))

    if boundary == "new_session":
        flow.context = FlowSession(flow, StandaloneFlowExecution())
        flow.context._args = request
    else:
        flow.context._args = request
        flow.context.reset_report_metadata()

    assert flow._test_runs is not stale
    assert flow._test_runs == TestRunInvocation()
    assert flow.context.records_no_evidence is False or boundary == "prepare_execution"
    assert _names(flow) == ["stress+short@1"]
    assert _decide(flow) == ()


def test_executing_one_flow_object_twice_uses_each_requests_own_test_runs(tmp_path, monkeypatch):
    """Through ``execute``: the second request is not answered from the first one's runs."""
    flow = _flow(tmp_path, monkeypatch, "--test", "stress+soak@9")
    seen: list[tuple[list[str], tuple[str, ...], bool]] = []

    def run() -> EndpointOutcome:
        names = _names(flow)
        other = _decide(flow)
        seen.append((names, other, flow.context.records_no_evidence))
        return EndpointOutcome(report_text="sim: ran")

    monkeypatch.setattr(flow, "_run", run)
    # The host/Sandbox location gate is not what this test is about.
    monkeypatch.setattr(flow, "_pre_state_gate", lambda: None)
    base = replace(flow.args, report_dir=tmp_path / "reports", diagnostic=True)

    flow.execute(replace(base, test=("stress+soak@9",)))
    flow.execute(replace(base, test=("stress@7", "reset")))
    flow.execute(replace(base, test=("stress",), diagnostic=False))

    assert [names for names, _other, _withheld in seen] == [
        ["stress+soak@9"],
        ["stress+short@7", "reset@1"],
        ["stress+short@1"],
    ]
    assert [other for _names_, other, _withheld in seen] == [
        ("sim: stress+soak@9",),
        ("sim: stress+short@7",),
        (),
    ]
    # The third request is all defaults and not --diagnostic: evidence is not withheld.
    assert seen[2][2] is False

"""One Flow object, several invocations: Test Run decisions never carry over (ADR 0072).

Resolved Test Runs (drawn seeds included), the baseline runs and the Goals'
frozen Test Runs belong to one request. They restart wherever an invocation starts: a
new invocation, a newly parsed request, and the preparation of an execution.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from booley.flows import endpoint_session
from booley.flows.execution_persistence import StandaloneFlowExecution
from booley.flows.sim.test_run_invocation import TestRunInvocation
from booley.runtime.endpoint_execution import EndpointOutcome
from tests.flows.sim.test_flow_test_run_resolution import _MAP, _flow


def _names(flow) -> list[str]:
    return [run.name for run in flow._resolve_test_runs("sim", _MAP)]


def _decide(flow) -> tuple[str, ...]:
    """Read the Goals' frozen Test Runs, as an invocation does once (none: no Goal Mode)."""
    assert flow._frozen_test_runs() is None
    return tuple(flow._test_runs.fresh_seed_selectors)


def test_a_newly_parsed_request_resolves_its_own_test_runs(tmp_path, monkeypatch):
    flow = _flow(tmp_path, monkeypatch, "--test", "stress+soak@9")
    assert _names(flow) == ["stress+soak@9"]
    assert _decide(flow) == ()
    assert flow._test_runs.frozen == (None,)  # read once: nothing is frozen outside Goal Mode

    flow.parse_args(["--work-dir", str(tmp_path), "--target", "sim", "--test", "stress"])

    assert flow._test_runs == TestRunInvocation()
    assert _names(flow) == ["stress+short@1"]
    assert _decide(flow) == ()


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


@pytest.mark.parametrize("boundary", ["invocation_start", "prepare_execution"])
def test_every_invocation_boundary_starts_the_state_afresh(tmp_path, monkeypatch, boundary):
    flow = _flow(tmp_path, monkeypatch, "--test", "stress+soak@9")
    _names(flow)
    _decide(flow)
    stale = flow._test_runs
    request = replace(flow.args, test=("stress",))

    if boundary == "invocation_start":
        flow._start_invocation(StandaloneFlowExecution(), request)
    else:
        flow._args = request
        endpoint_session.prepare_execution(flow)

    assert flow._test_runs is not stale
    assert flow._test_runs == TestRunInvocation()
    assert _names(flow) == ["stress+short@1"]
    assert _decide(flow) == ()


def test_executing_one_flow_object_twice_uses_each_requests_own_test_runs(tmp_path, monkeypatch):
    """Through ``execute``: the second request is not answered from the first one's runs."""
    flow = _flow(tmp_path, monkeypatch, "--test", "stress+soak@9")
    seen: list[list[str]] = []

    def run() -> EndpointOutcome:
        seen.append(_names(flow))
        return EndpointOutcome(report_text="sim: ran")

    monkeypatch.setattr(flow, "_run", run)
    # The host/Sandbox location gate is not what this test is about.
    monkeypatch.setattr(flow, "_pre_state_gate", lambda: None)
    base = replace(flow.args, report_dir=tmp_path / "reports", diagnostic=True)

    flow.execute(replace(base, test=("stress+soak@9",)))
    flow.execute(replace(base, test=("stress@7", "reset")))
    flow.execute(replace(base, test=("stress",), diagnostic=False))

    assert seen == [
        ["stress+soak@9"],
        ["stress+short@7", "reset@1"],
        ["stress+short@1"],
    ]


def test_tool_identity_does_not_leak_between_invocations(tmp_path, monkeypatch):
    flow = _flow(tmp_path, monkeypatch)
    flow._record_eda_tool("sim", "verilator")
    assert flow._eda_tool == "verilator"
    flow.parse_args(["--work-dir", str(tmp_path), "--target", "sim"])
    assert flow._eda_tool is None
    assert flow._eda_tools == {"sim": None}
    assert flow._target_eda_tools == {"sim": None}

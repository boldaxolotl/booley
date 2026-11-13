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
from tests.flows.invocation_doubles import replace_run
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

    replace_run(monkeypatch, flow, run)
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


def test_simulation_outcomes_belong_to_simulation_flow(tmp_path, monkeypatch):
    """Only the Simulation Flow keeps Campaign outcomes; other built-in Flows carry none."""
    from booley.flows.lint.flow import LintFlow

    lint = LintFlow()
    assert not hasattr(lint, "_simulation_campaign_outcomes")
    assert not hasattr(lint, "_simulation_acceptance_outcomes")

    flow = _flow(tmp_path, monkeypatch, "--test", "stress")
    flow._simulation_campaign_outcomes = ("stale",)
    flow._simulation_acceptance_outcomes = ("stale",)
    flow._start_invocation(StandaloneFlowExecution())
    assert flow._simulation_campaign_outcomes == ()
    assert flow._simulation_acceptance_outcomes == ()


def test_build_timeout_is_resolved_again_for_the_next_invocation(tmp_path, monkeypatch):
    """A reused Flow object keeps no build budget from the previous request."""
    config = tmp_path / ".booley" / "booley.toml"
    config.parent.mkdir()
    config.write_text("[flows.sim]\nbuild_timeout_ms = 111000\n", encoding="utf-8")
    flow = _flow(tmp_path, monkeypatch)
    assert flow._effective_build_timeout_ms() == 111_000

    config.write_text("[flows.sim]\nbuild_timeout_ms = 222000\n", encoding="utf-8")
    flow.parse_args(["--work-dir", str(tmp_path), "--target", "sim"])

    assert flow._effective_build_timeout_ms() == 222_000


# Flow state set while preparing a request: a stale value, and what the next invocation starts with.
_STALE_STATE = {
    "_resume_coverage_plan": (object(), None),
    "_coverage_prepared": (object(), None),
    "_resolved_build_timeout_ms": (5, None),
    "_target_handles": ({"sim": object()}, {}),
    "_current_published_pre_sim_keys": ({("a", "b", "c", 1)}, set()),
    "_standalone_plan_recipe": (object(), None),
}


@pytest.mark.parametrize("name", _STALE_STATE)
def test_flow_state_restarts_with_each_invocation(tmp_path, monkeypatch, name):
    stale, fresh = _STALE_STATE[name]
    flow = _flow(tmp_path, monkeypatch)
    setattr(flow, name, stale)

    flow._start_invocation(StandaloneFlowExecution())

    assert getattr(flow, name) == fresh


def test_requeued_attempt_keeps_drawn_seeds_and_drops_campaign_outcomes(
    tmp_path, monkeypatch, capsys
):
    """The OOM requeue reruns the same request: seeds stay, the killed attempt's results go."""
    flow = _flow(tmp_path, monkeypatch, "--seed", "random", "--test", "stress")
    drawn = flow._resolve_test_runs("sim", _MAP)
    # Stand-ins for what the killed attempt left behind.
    flow._simulation_campaign_outcomes = ("campaign-outcome",)
    flow._simulation_acceptance_outcomes = ("acceptance-outcome",)
    flow._current_published_pre_sim_keys = {"pre-sim-key"}
    flow.cycle_report_json = '{"cycle": 1}'
    flow._reserved_invocation_dir = tmp_path / "reports" / "sim" / "1"

    flow.begin_requeued_attempt()

    assert flow._resolve_test_runs("sim", _MAP) is drawn
    assert flow._simulation_campaign_outcomes == ()
    assert flow._simulation_acceptance_outcomes == ()
    assert flow._current_published_pre_sim_keys == set()
    assert flow.cycle_report_json is None
    assert flow._reserved_invocation_dir is None
    # The marker separates the killed attempt's stdout from the next attempt's.
    assert "requeued after out-of-memory" in capsys.readouterr().out

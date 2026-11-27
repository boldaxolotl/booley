"""One Flow object, several invocations: Test Run decisions never carry over (ADR 0072).

Resolved Test Runs (drawn seeds included) and the baseline runs belong to one request. They restart wherever an invocation starts: a
new invocation, a newly parsed request, and the preparation of an execution.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from booley.flows import test_runs
from booley.flows.sim.flow import SimulateFlow
from booley.flows.sim.request import SimRequest
from booley.runtime.endpoint_execution import EndpointOutcome
from tests.flows.invocation_doubles import replace_run
from tests.flows.sim.test_flow_test_run_resolution import _MAP, _flow, _launched


def test_one_flow_object_resolves_each_request_to_its_own_test_runs(tmp_path, monkeypatch):
    """Five requests through ``execute`` on one Flow: no Test Run, seed or draw carries over."""
    draws = [11, 22]
    source = SimpleNamespace(randint=lambda _low, _high: draws.pop(0))
    # The module-owned name ``draw_seeds`` reads its operating-system random source from.
    monkeypatch.setattr(test_runs, "secrets", SimpleNamespace(SystemRandom=lambda: source))
    flow = SimulateFlow()
    requests: list[dict] = [
        {"test": ("stress+soak@9",)},
        {"test": ("stress@7", "reset")},
        {"test": ("stress",)},
        {"seed": "random", "test": ("stress",)},
        {"seed": "random", "test": ("stress",)},
    ]

    launched = [
        [name for name, _args in _launched(tmp_path, monkeypatch, flow=flow, **request)[1]]
        for request in requests
    ]

    assert launched == [
        ["stress+soak@9"],
        ["stress+short@7", "reset@1"],
        ["stress+short@1"],
        ["stress+short@11"],
        ["stress+short@22"],
    ]
    assert draws == []


def test_a_replaced_run_executes_through_the_ordinary_invocation(tmp_path, monkeypatch):
    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    flow = SimulateFlow()
    replace_run(monkeypatch, flow, lambda: EndpointOutcome(report_text="sim: ran"))

    result = flow.execute(
        SimRequest(work_dir=tmp_path, report_dir=tmp_path / "reports", target="sim")
    )

    assert result.outcome.report_text == "sim: ran"


def test_tool_identity_does_not_leak_between_invocations(tmp_path, monkeypatch):
    # Private seam: the per-Target tool map is read only by the job record the run supervisor writes.
    flow = _flow(tmp_path, monkeypatch)
    flow._record_eda_tool("sim", "verilator")
    assert flow._eda_tool == "verilator"
    flow.parse_args(["--work-dir", str(tmp_path), "--target", "sim"])
    assert flow._eda_tool is None
    assert flow._eda_tools == {"sim": None}


def test_build_timeout_is_resolved_again_for_the_next_invocation(tmp_path, monkeypatch):
    """A reused Flow object keeps no build budget from the previous request."""
    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    config = tmp_path / ".booley" / "booley.toml"
    config.parent.mkdir()
    (tmp_path / "sim.core").write_text(
        "CAPI=2:\nname: ::sim:0\ntargets:\n  sim:\n    flow: sim\n"
        "    flow_options: {tool: verilator}\n    toplevel: tb\n",
        encoding="utf-8",
    )
    flow = SimulateFlow()
    seen: list[int] = []
    for budget in (111_000, 222_000):
        config.write_text(f"[flows.sim]\nbuild_timeout_ms = {budget}\n", encoding="utf-8")
        reports = tmp_path / f"reports-{budget}"
        request = SimRequest(
            mode="elab_only", dry_run=True, work_dir=tmp_path, report_dir=reports, target="sim"
        )
        assert flow.execute(request).exit_code == 0
        plan = json.loads(next(reports.rglob("flow_plan.json")).read_text(encoding="utf-8"))
        seen.append(plan["work_units"][0]["timeout_ms"])

    assert seen == [111_000, 222_000]


# Flow state set while preparing a request: a stale value, and what the next invocation starts with.
# Private seam: set only by coverage and standalone preparation; a second full run of each is the only route.
_STALE_STATE = {
    "_coverage_prepared": (object(), None),
    "_target_handles": ({"sim": object()}, {}),
    "_current_published_pre_sim_keys": ({("a", "b", "c", 1)}, set()),
    "_standalone_plan_recipe": (object(), None),
}


@pytest.mark.parametrize("name", _STALE_STATE)
def test_flow_state_restarts_with_each_invocation(tmp_path, monkeypatch, name):
    # Private seam: set only by coverage, resume and standalone preparation; a second full run of each is the only route.
    stale, fresh = _STALE_STATE[name]
    flow = _flow(tmp_path, monkeypatch)
    setattr(flow, name, stale)

    flow._start_invocation()

    assert getattr(flow, name) == fresh


def test_requeued_attempt_keeps_drawn_seeds_and_drops_simulation_record_outcomes(
    tmp_path, monkeypatch, capsys
):
    """The OOM requeue reruns the same request: seeds stay, the killed attempt's results go."""
    # Private seam: the requeue is read only by the job record the run supervisor writes.
    flow = _flow(tmp_path, monkeypatch, "--seed", "random", "--test", "stress")
    drawn = flow._resolve_test_runs("sim", _MAP)
    # Stand-ins for what the killed attempt left behind.
    flow._simulation_record_outcomes = ("record-outcome",)
    flow._current_published_pre_sim_keys = {"pre-sim-key"}
    flow.cycle_report_json = '{"cycle": 1}'
    flow._reserved_invocation_dir = tmp_path / "reports" / "sim" / "1"

    flow.begin_requeued_attempt()

    assert flow._resolve_test_runs("sim", _MAP) is drawn
    assert flow._simulation_record_outcomes == ()
    assert flow._current_published_pre_sim_keys == set()
    assert flow.cycle_report_json is None
    assert flow._reserved_invocation_dir is None
    # The marker separates the killed attempt's stdout from the next attempt's.
    assert "requeued after out-of-memory" in capsys.readouterr().out

"""The Simulation Flow resolves a request to concrete Test Runs once (ADR 0072)."""

from __future__ import annotations

from pathlib import Path

import pytest

from booley.config.project_config import normalize_tests_toml
from booley.flows.sim.execution import DefaultSelection
from booley.flows.sim.flow import SimulateFlow
from booley.flows.sim.request import SimRequest
from booley.flows.sim.watchdog_sizing import resolve_simulation_record_work_units
from booley.flows.target_test_plan import build_target_test_plan
from booley.runtime.endpoint_execution import EXIT_ERROR
from tests.flows.sim.leased_session import canned_build_session
from tests.flows.sim.simulation_record_boundary import PASS_PROCESS, run_simulation_record_sim
from tests.target_test_support import write_two_core_sim_project

_TESTS = {
    "sim": {
        "tests": ["reset", "stress"],
        "skip": ["reset"],
        "variants": {"stress": {"default": "short", "short": [], "soak": ["+iters=9"]}},
    }
}
_MAP = {"sim": ["reset", "stress"]}


def _flow(tmp_path: Path, monkeypatch, *argv: str, adapter: str = "verilator") -> SimulateFlow:
    # Private seam: importers resolve Test Runs without a project, so the plan and skips are stubbed.
    flow = SimulateFlow()
    flow.parse_args(["--work-dir", str(tmp_path), "--target", "sim", *argv])
    plan = build_target_test_plan("sim", normalize_tests_toml(_TESTS)["sim"], adapter=adapter)
    monkeypatch.setattr(flow, "_target_test_plan", lambda _target: plan)
    monkeypatch.setattr(flow, "_effective_skips", lambda _target: {"reset"})
    return flow


_TESTS_TOML = (
    '[sim]\ntests = ["reset", "stress"]\nskip = ["reset"]\n[sim.variants.stress]\n'
    'default = "short"\nshort = []\nsoak = ["+iters=9"]\n'
)


def _launched(tmp_path: Path, monkeypatch, *, files=None, **request):
    """Run the record path over ``sim``; return its result and the launched ``(name, args)``."""
    with canned_build_session():
        run = run_simulation_record_sim(
            tmp_path,
            monkeypatch,
            {"sim": ["reset", "stress"]},
            PASS_PROCESS,
            files={".booley/tests.toml": _TESTS_TOML, **(files or {})},
            target="sim",
            **request,
        )
    runs = [(r.name, r.args) for launch in run.launches for r in launch.work.test_runs]
    return run.result, runs


def test_default_request_runs_default_variants_at_the_default_seed(tmp_path, monkeypatch):
    _result, runs = _launched(tmp_path, monkeypatch)
    assert runs == [("stress+short@1", ())]


def test_selectors_keep_order_and_override_skip(tmp_path, monkeypatch):
    _result, runs = _launched(tmp_path, monkeypatch, test=("stress+soak@9", "reset"))
    assert runs == [("stress+soak@9", ("+iters=9",)), ("reset@1", ())]


def test_count_selector_resolves_to_distinct_seeds(tmp_path, monkeypatch):
    _result, runs = _launched(tmp_path, monkeypatch, test=("stress@x4",))
    assert len({name for name, _args in runs}) == 4
    assert {name.split("@")[0] for name, _args in runs} == {"stress+short"}


@pytest.mark.parametrize("tests_toml", [_TESTS_TOML, ""], ids=["registered-tests", "no-tests"])
def test_unseeded_target_refuses_a_seed_before_anything_runs(tmp_path, monkeypatch, tests_toml):
    core = (
        "CAPI=2:\nname: ::sim:0\ntargets:\n  sim:\n    flow: sim\n"
        "    flow_options: {tool: icarus}\n    toplevel: tb\n"
    )
    files = {"sim.core": core, ".booley/tests.toml": tests_toml}
    result, runs = _launched(tmp_path, monkeypatch, files=files, seed=3)
    assert result.exit_code == EXIT_ERROR
    assert "is unseeded" in result.outcome.report_text
    assert runs == []


def test_plan_validation_discovers_the_registered_tests_itself(tmp_path, monkeypatch):
    # Private seam: every production caller passes the registry map; only the default discovers it.
    flow = _flow(tmp_path, monkeypatch, "--seed", "3", adapter="icarus")
    outcome = flow._validate_test_plans(["sim"])
    assert outcome is not None and "is unseeded" in outcome.report_text


def test_all_skipped_suite_resolves_to_no_runs(tmp_path, monkeypatch):
    # Private seam: no public request reaches the empty-runs DefaultSelection branch; a Target without a catalog resolves to one unnamed run and the endpoint refuses an all-skipped Target first.
    flow = _flow(tmp_path, monkeypatch)
    monkeypatch.setattr(flow, "_effective_skips", lambda _target: {"reset", "stress"})
    assert flow._resolve_test_runs("sim", _MAP) == ()
    assert isinstance(flow._test_run_selection("sim", _MAP), DefaultSelection)


def test_two_keys_for_one_target_refuse_the_run_naming_both(tmp_path, monkeypatch):
    write_two_core_sim_project(
        tmp_path, '["y#sim"]\ntests = ["one"]\n["vendor:b:y#sim"]\ntests = ["two"]\n'
    )
    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    result = SimulateFlow().execute(
        SimRequest(work_dir=tmp_path, report_dir=tmp_path / "reports", target="y#sim")
    )

    assert result.exit_code == EXIT_ERROR
    assert "[vendor:b:y#sim] and [y#sim]" in result.outcome.report_text
    # The watchdog sizing preflight must not raise: the child Flow reports the refusal.
    assert resolve_simulation_record_work_units(tmp_path, "y#sim") == 1

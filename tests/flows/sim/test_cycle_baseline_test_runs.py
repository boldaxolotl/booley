"""A Cycle Count baseline runs the candidate's concrete Test Run (ADR 0072)."""

from __future__ import annotations

from pathlib import Path

import pytest

from booley.flows.sim.execution import NamedTests, SelectedTestRuns
from booley.flows.sim.execution.engine import target_test_plan
from booley.flows.test_runs import UnseededTargetError
from tests.flows.sim.test_flow_test_run_resolution import _MAP, _flow
from tests.flows.sim.test_test_run_launch import (
    _handle,
    _inspection,
    _plusargs,
    _preview,
    _project,
    _work,
)

_CANDIDATE = """
[sim]
tests = ["stress"]
select = "+test={name}"
default_seed = 7

[sim.variants.stress]
default = "soak"
short = ["+iters=10"]
soak = { args = ["+iters=900"], env = { MODE = "soak" } }
"""

# The baseline revision declares another default Test Variant and Default Seed.
_BASELINE = """
[sim]
tests = ["stress"]
select = "+test={name}"

[sim.variants.stress]
default = "short"
short = ["+iters=10"]
soak = ["+iters=5"]
"""


def test_baseline_checkout_launches_the_candidates_test_run(tmp_path: Path):
    candidate = _project(tmp_path / "candidate", _CANDIDATE)
    baseline = _project(tmp_path / "baseline", _BASELINE)
    inspection = _inspection("verilator")
    run = target_test_plan(_handle(candidate), "verilator", {}).run("stress")
    assert run.name == "stress+soak@7"

    on_candidate = _preview(candidate, inspection, SelectedTestRuns((run,)))
    on_baseline = _preview(baseline, inspection, SelectedTestRuns((run,)))
    own_default = _preview(baseline, inspection, NamedTests(("stress",)))

    expected = ["test=stress", "iters=900", "verilator+seed+7"]
    assert _plusargs(on_candidate.commands[0]) == expected
    assert _plusargs(on_baseline.commands[0]) == expected
    assert on_baseline.run_groups == ((run,),)
    launched = _work(baseline, inspection, ("stress",), (run,))
    assert launched.environment == (("MODE", "soak"),)
    # What the baseline would have run from its own tests.toml, and must not.
    assert _plusargs(own_default.commands[0]) == ["test=stress", "iters=10", "verilator+seed+1"]


def test_baseline_that_cannot_apply_the_seed_refuses_instead_of_dropping_it(tmp_path: Path):
    candidate = _project(tmp_path / "candidate", _CANDIDATE)
    baseline = _project(tmp_path / "baseline", _BASELINE)
    run = target_test_plan(_handle(candidate), "verilator", {}).run("stress")
    with pytest.raises(UnseededTargetError) as excinfo:
        _preview(baseline, _inspection("icarus"), SelectedTestRuns((run,)))
    assert str(excinfo.value).startswith(
        "Test Run 'stress+soak@7' has a seed, but Target 'sim' is unseeded"
    )


def test_flow_freezes_the_candidates_run_for_each_baseline_test(tmp_path, monkeypatch):
    flow = _flow(tmp_path, monkeypatch, "--test", "stress+soak@9")

    flow._freeze_baseline_test_runs((("sim", "sim_ref", "stress"), ("sim", "sim", "reset")), _MAP)

    selection = flow._baseline_selection("sim_ref", ("stress",))
    assert isinstance(selection, SelectedTestRuns)
    assert [run.name for run in selection.runs] == ["stress+soak@9"]
    # The candidate does not run "reset" in this invocation: the baseline
    # checkout resolves that Test itself.
    assert flow._baseline_selection("sim", ("reset",)) == NamedTests(("reset",))
    assert flow._baseline_selection("other", ("stress",)) == NamedTests(("stress",))


def test_default_request_freezes_the_default_test_run(tmp_path, monkeypatch):
    flow = _flow(tmp_path, monkeypatch)
    flow._freeze_baseline_test_runs((("sim", "sim", "stress"),), _MAP)
    selection = flow._baseline_selection("sim", ("stress",))
    assert isinstance(selection, SelectedTestRuns)
    assert [run.name for run in selection.runs] == ["stress+short@1"]

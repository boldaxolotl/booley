"""Simulation Record disclosure planning prepares the Test Runs that launch (ADR 0072).

A Cocotb batch may hold non-default Test Variants that share an environment
while their Tests' default Variants do not. Planning the disclosure from bare
Test names would prepare the defaults and refuse the batch ("cannot share one
simulator process"), so the concrete Test Runs are passed through.
"""

from __future__ import annotations

import pytest

from booley.config.project_config import normalize_tests_toml
from booley.flows.base import SubprocessResult
from booley.flows.target_test_plan import batch_test_runs, build_target_test_plan
from booley.mcp.base import EXIT_SUCCESS
from tests.flows.sim.simulation_record_boundary import run_simulation_record_sim
from tests.flows.sim.test_cocotb import _CORE_FILES, _cocotb_output

_TESTS = {
    "sim": {
        "tests": ["a", "b"],
        "variants": {
            "a": {
                "default": "one",
                "one": {"env": {"MODE": "A"}},
                "shared": {"env": {"MODE": "C"}},
            },
            "b": {
                "default": "two",
                "two": {"env": {"MODE": "B"}},
                "shared": {"env": {"MODE": "C"}},
            },
        },
    }
}
_TESTS_TOML = """\
[ccfg]
tests = ["a", "b"]
[ccfg.variants.a]
default = "one"
one = { env = { MODE = "A" } }
shared = { env = { MODE = "C" } }
[ccfg.variants.b]
default = "two"
two = { env = { MODE = "B" } }
shared = { env = { MODE = "C" } }
"""


def _plan():
    return build_target_test_plan("sim", normalize_tests_toml(_TESTS)["sim"], adapter="cocotb")


def test_selected_variants_share_a_batch_their_defaults_could_not():
    plan = _plan()
    runs = plan.resolve(["a+shared", "b+shared"])

    assert batch_test_runs(runs, one_process_per_run=False) == (runs,)
    assert plan.launch(runs).environment == (("MODE", "C"),)
    with pytest.raises(ValueError, match="cannot share one simulator process"):
        plan.launch(plan.default_runs_for(("a", "b")))


def test_simulation_record_inputs_disclose_the_concrete_test_runs(tmp_path, monkeypatch):
    """Planning prepares the selected Variants' runs; the defaults would refuse the batch."""
    run = run_simulation_record_sim(
        tmp_path,
        monkeypatch,
        {"ccfg": ["a", "b"]},
        SubprocessResult(
            returncode=0,
            stdout=_cocotb_output([("a", "pass", ""), ("b", "pass", "")]),
            duration_s=2.0,
        ),
        files={**_CORE_FILES, ".booley/tests.toml": _TESTS_TOML},
        target="ccfg",
        test=("a+shared", "b+shared"),
    )

    assert run.result.exit_code == EXIT_SUCCESS, run.result.outcome.report_text
    (launch,) = run.launches
    assert [work_run.name for work_run in launch.work.test_runs] == ["a+shared@1", "b+shared@1"]
    assert "    PASS         b+shared@1" in run.result.outcome.report_text.splitlines()

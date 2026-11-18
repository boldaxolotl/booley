"""Simulation Record disclosure planning prepares the Test Runs that launch (ADR 0072).

A Cocotb batch may hold non-default Test Variants that share an environment
while their Tests' default Variants do not. Planning the disclosure from bare
Test names would prepare the defaults and refuse the batch ("cannot share one
simulator process"), so the concrete Test Runs are passed through.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from booley.config.project_config import normalize_tests_toml
from booley.flows.base import SubprocessResult
from booley.flows.target_test_plan import batch_test_runs, build_target_test_plan
from booley.mcp.base import EXIT_SUCCESS
from tests.flows.sim.simulation_record_boundary import run_simulation_record_sim
from tests.flows.sim.test_cocotb import _CORE_FILES, _cocotb_output
from tests.flows.sim.test_flow_test_run_resolution import _flow

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


def test_resume_discloses_the_test_runs_the_manifest_froze(tmp_path, monkeypatch):
    """The resume call site: disclosure planning gets each batch's frozen Test Runs.

    The manifest froze two batches of non-default Test Variants. Re-planning
    for the resume comparison must prepare exactly those runs, not the
    default Variants of the same Tests (which could not even share a batch).
    """
    # Private seam: the public resume path (see test_simulation_record_test_runs.py) starts
    # from an ordinary-HDL interruption; a Cocotb record frozen with two non-default Variant
    # batches cannot be interrupted there, and the call to pin is the re-plan itself.
    from booley.flows.sim import flow_resume as resume_module
    from booley.flows.test_runs import build_test_run
    from tests.flows.sim.test_simulation_record_test_runs import _cocotb_plan

    groups = (
        (
            build_test_run("reset", variant="shared", seed=1, env={"MODE": "C"}),
            build_test_run("count", variant="shared", seed=1, env={"MODE": "C"}),
        ),
        (build_test_run("count", variant="other", seed=9, env={"MODE": "D"}),),
    )
    manifest = _cocotb_plan(tmp_path, groups).manifest
    handle = SimpleNamespace(project_root=tmp_path, selector="sim", identity="sim")
    validated = SimpleNamespace(
        manifest=manifest, binding_for=lambda _manifest: None, target_handles=(handle,)
    )
    disclosed: list[tuple[tuple[str, ...], object]] = []
    previewed: list[object] = []
    execution = SimpleNamespace(
        preview=lambda _handle, selection: (
            previewed.append(selection) or SimpleNamespace(groups=(), run_groups=())
        ),
        plan_ordinary_group=lambda _handle, group, test_runs=None: (
            disclosed.append((group, test_runs)) or {"group": group}
        ),
    )
    flow = _flow(tmp_path, monkeypatch)
    monkeypatch.setattr(flow, "_simulation_execution", lambda: execution)
    monkeypatch.setattr(
        resume_module.TargetCatalog,
        "build",
        lambda _root: SimpleNamespace(inspect=lambda _handle: object()),
    )
    monkeypatch.setattr(resume_module, "git_full_sha", lambda *_args: "c" * 40)
    planned: dict[str, object] = {}
    monkeypatch.setattr(
        resume_module,
        "plan_coarse_simulation_record",
        lambda **kwargs: planned.update(kwargs) or "plan",
    )

    assert flow._resume_simulation_record_plan(validated, 1) == "plan"

    assert disclosed == [(("reset", "count"), groups[0]), (("count",), groups[1])]
    assert planned["planning_disclosures"] == (
        {"group": ("reset", "count")},
        {"group": ("count",)},
    )
    # Resume previews the frozen runs too: nothing is resolved or drawn again.
    (selection,) = previewed
    assert selection.runs == (*groups[0], *groups[1])

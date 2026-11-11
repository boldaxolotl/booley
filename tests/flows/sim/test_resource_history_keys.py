"""Resource History keys name each Test Variant; records carry the source size (#3235).

Runs the real ``SimulateFlow`` campaign path against the fake-Verilator Project
of the build-reuse tests and reads the history file the Flow wrote.
"""

from __future__ import annotations

import os
from collections import Counter
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.flows import job_resources
from booley.flows.sim.flow import SimulateFlow
from booley.flows.sim.request import SimRequest
from booley.flows.sim.verilator_coverage import SelectedCoverageRun, SimulationCommandRequest
from booley.flows.target_test_plan_loading import load_target_test_plan
from booley.runtime import resource_history
from tests.flows.sim.test_verilator_build_reuse_campaign import (  # noqa: F401 -- fixture
    flow_project,
)
from tests.flows.sim.test_verilator_coverage_execution import (
    _build_coverage,
    _declare_variants,
    _execution_fixture,
    _run_request,
)

# The imported fake-Verilator Project is POSIX-only, like its home module.
pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="Verilator build reuse is proven in the POSIX Sandbox"
)

_TESTS_TOML = """\
[sim]
tests = ["first"]
select = "+test={name}"
seed = "+seed={seed}"

[sim.variants.first]
default = "fast"
fast = []
slow = ["+iters=9"]
"""


def _run(project: Path, reports: Path, selector: str) -> None:
    result = SimulateFlow().execute(
        SimRequest(
            target="sim",
            work_dir=project,
            report_dir=reports,
            test=(selector,),
            timeout_ms=30_000,
        )
    )
    assert result.exit_code == 0, result.outcome.report_text


def _source_bytes(project: Path) -> int:
    names = ("rtl/tb.sv", "inc/defs.svh", "dpi/helper.cpp")
    return sum((project / name).stat().st_size for name in names)


def test_sim_history_keys_name_variants_not_seeds(
    flow_project: Path,  # noqa: F811 -- shared fixture keeps its required name
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    booley_dir = flow_project / ".booley"
    monkeypatch.setenv("BOOLEY_DIR", str(booley_dir))
    (booley_dir / "tests.toml").write_text(_TESTS_TOML, encoding="utf-8")
    reports = tmp_path / "reports"
    initial_bytes = _source_bytes(flow_project)

    _run(flow_project, reports, "first+slow@1")
    _run(flow_project, reports, "first+slow@2")
    _run(flow_project, reports, "first+fast")
    appended = b"// grown\n"
    with (flow_project / "rtl" / "tb.sv").open("ab") as handle:
        handle.write(appended)
    _run(flow_project, reports, "first+fast")

    records = resource_history.load_records(booley_dir)
    runs = [r for r in records if r["key"]["phase"] == "sim_run"]
    # Seeds share a key; the default Variant keeps the bare Test name (the old key).
    assert Counter(r["key"]["test_run"] for r in runs) == {"first+slow": 2, "first": 2}
    builds = [r for r in records if r["key"]["phase"] == "sim_build"]
    assert builds, "the changed source forces at least one measured build"
    assert {r["source_bytes"] for r in runs[:3] + builds[:1]} == {initial_bytes}
    assert runs[-1]["source_bytes"] == initial_bytes + len(appended)


def test_sim_request_maps_to_recorded_keys(
    flow_project: Path,  # noqa: F811 -- shared fixture keeps its required name
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The estimator lists the keys a real ``SimulateFlow`` run writes, in run order."""
    from booley.flows.job_estimates import phase_keys, sim_job_request
    from booley.flows.target_test_plan_loading import load_checkout_target_test_plan

    booley_dir = flow_project / ".booley"
    monkeypatch.setenv("BOOLEY_DIR", str(booley_dir))
    (booley_dir / "tests.toml").write_text(_TESTS_TOML, encoding="utf-8")
    selectors = ("first+slow@1", "first+fast")

    result = SimulateFlow().execute(
        SimRequest(
            target="sim",
            work_dir=flow_project,
            report_dir=tmp_path / "reports",
            test=selectors,
            timeout_ms=30_000,
        )
    )
    assert result.exit_code == 0, result.outcome.report_text

    recorded: list[resource_history.HistoryKey] = []
    for record in resource_history.load_records(booley_dir):
        key = resource_history.history_key(record)
        if key not in recorded:
            recorded.append(key)
    # Nothing writes these, which is why the request never lists them.
    assert {key.phase for key in recorded} == {"sim_build", "sim_run"}
    plan = load_checkout_target_test_plan(flow_project, "sim")
    request = sim_job_request(plan, plan.resolve(selectors))
    assert list(phase_keys(request)) == recorded
    assert [key.test_run for key in recorded] == ["", "first+slow", "first"]


def test_coverage_request_maps_to_recorded_keys(tmp_path: Path, monkeypatch) -> None:
    """A coverage Job's request lists the keys its commands record, in run order."""
    from booley.flows.job_estimates import phase_keys, sim_job_request
    from booley.runtime.resource_history import history_key

    _declare_variants(tmp_path)
    execution, target, raw_path, _captured = _execution_fixture(tmp_path, monkeypatch)
    plan = load_target_test_plan(tmp_path, "sim", identity="sim", adapter="verilator")
    runs = plan.resolve(("wrap+soak@7",))
    labels: list[job_resources.ResourceLabel | None] = []
    invoke = execution._invoke

    def spy(command, *, timeout):
        labels.append(job_resources.current_label())
        return invoke(command, timeout=timeout)

    execution._invoke = spy
    job_resources.clear_label()
    _build_coverage(execution, target)
    execution.run(
        replace(_run_request(target, raw_path), test=SelectedCoverageRun("wrap", runs[0]))
    )
    execution.command(
        SimulationCommandRequest(("verilator_coverage", "--version"), tmp_path, raw_path)
    )
    job_resources.clear_label()

    result = SimpleNamespace(returncode=0, timed_out=False, duration_s=1.0)
    measurement = job_resources.JobMeasurement(0.25, job_resources.Usage(), {}, None, None, None)
    recorded: list = []
    for label in labels:
        assert label is not None
        (record,) = job_resources.history_records(
            "simulate", label, ("coverage",), measurement, result
        )
        if history_key(record) not in recorded:
            recorded.append(history_key(record))
    request = sim_job_request(plan, runs, flags=("coverage",))
    assert list(phase_keys(request)) == recorded
    assert [key.phase for key in recorded] == [
        "sim_prepare",
        "sim_build",
        "sim_run",
        "sim_postprocess",
    ]

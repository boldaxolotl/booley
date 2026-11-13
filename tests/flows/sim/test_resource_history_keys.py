"""Resource History keys name each Test Variant; records carry the source size (#3235).

Runs the real ``SimulateFlow`` campaign path against the fake-Verilator Project
of the build-reuse tests and reads the history file the Flow wrote.
"""

from __future__ import annotations

import itertools
import os
from collections import Counter
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.flows import job_resources
from booley.flows.sim.flow import SimulateFlow
from booley.flows.sim.mode import SimulationMode
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
    # FuseSoC setup is measured as ``sim_prepare`` too (#3787), keyed by no Test Run.
    assert [key.phase for key in recorded] == ["sim_prepare", "sim_build", "sim_run", "sim_run"]
    plan = load_checkout_target_test_plan(flow_project, "sim")
    request = sim_job_request(plan, plan.resolve(selectors))
    assert list(phase_keys(request)) == recorded
    assert [key.test_run for key in recorded] == ["", "", "first+slow", "first"]


def test_elab_only_request_maps_to_recorded_keys(
    flow_project: Path,  # noqa: F811 -- shared fixture keeps its required name
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An elaboration-only run records the FuseSoC setup and build its request lists (#3787)."""
    from booley.flows.job_estimates import JobRequest, phase_keys

    booley_dir = flow_project / ".booley"
    monkeypatch.setenv("BOOLEY_DIR", str(booley_dir))

    result = SimulateFlow().execute(
        SimRequest(
            target="sim",
            work_dir=flow_project,
            report_dir=tmp_path / "reports",
            mode=SimulationMode.ELAB_ONLY,
            timeout_ms=30_000,
        )
    )
    assert result.exit_code == 0, result.outcome.report_text

    recorded: list[resource_history.HistoryKey] = []
    for record in resource_history.load_records(booley_dir):
        key = resource_history.history_key(record)
        if key not in recorded:
            recorded.append(key)
    assert list(phase_keys(JobRequest("sim", "sim", test_runs=()))) == recorded
    assert [key.phase for key in recorded] == ["sim_prepare", "sim_build"]


def _fusesoc_killed_by_sigkill(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Put a ``fusesoc`` first on PATH that dies by SIGKILL, as the OOM killer would."""
    stub_dir = tmp_path / "oom-bin"
    stub_dir.mkdir()
    stub = stub_dir / "fusesoc"
    stub.write_bytes(b"#!/bin/sh\nkill -9 $$\n")
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", os.pathsep.join((str(stub_dir), os.environ["PATH"])))


def test_oom_killed_fusesoc_setup_requeues_the_job(
    flow_project: Path,  # noqa: F811 -- shared fixture keeps its required name
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An OOM kill of FuseSoC setup is blamed, recorded and ends the attempt for requeue (#3787).

    Drives the engine with the Flow's measured runner, as ``SimulateFlow`` does,
    inside the attempt scope that arms a requeue (``tests/runtime`` covers the
    requeue itself: an own OOM kill plus a failed result).
    """
    from booley.flows.sim.build import SimulationBuildPreparationError
    from booley.flows.sim.execution.contract import SimulationOptions
    from booley.flows.sim.execution.engine import SimulationExecution
    from booley.runtime.endpoint_execution import job_attempt
    from tests.flows.sim.prepared_group import run_prepared_group
    from tests.flows.sim.test_verilator_build_reuse_engine import _select

    booley_dir = flow_project / ".booley"
    monkeypatch.setenv("BOOLEY_DIR", str(booley_dir))
    _fusesoc_killed_by_sigkill(tmp_path, monkeypatch)
    # The container's OOM counter rises over every command, so each kill is blamed.
    counter = itertools.count()
    monkeypatch.setattr(job_resources, "cgroup_oom_kill_count", lambda: next(counter))
    flow = SimulateFlow()
    # What ``execute`` binds before preparing: the Flow reads its flags for the history key.
    flow._args = SimRequest(
        target="sim", work_dir=flow_project, report_dir=tmp_path / "reports", timeout_ms=30_000
    )
    execution = SimulationExecution(
        invoke=flow._execute_boundary, options=SimulationOptions(timeout_ms=30_000)
    )

    with (
        job_attempt(stop_on_oom=True) as attempt,
        pytest.raises(SimulationBuildPreparationError) as raised,
    ):
        run_prepared_group(execution, _select(flow_project), ("first",))

    assert "OOM kill" in str(raised.value)
    assert attempt.oom_killed.is_set()
    records = [r for r in resource_history.load_records(booley_dir) if r["key"]["phase"]]
    assert [(r["key"]["phase"], r["outcome"]) for r in records] == [("sim_prepare", "oom_kill")]


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

    result = SimpleNamespace(returncode=0, timed_out=False, duration_s=1.0, oom_killed=False)
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


def test_cocotb_requests_batch_runs_except_under_coverage(tmp_path: Path) -> None:
    """Cocotb batches Test Runs of one seed per process; coverage runs each alone."""
    from booley.flows.job_estimates import phase_keys, sim_job_request

    (tmp_path / ".booley").mkdir()
    (tmp_path / ".booley" / "tests.toml").write_text(
        '[sim]\ntests = ["alpha", "beta"]\n', encoding="utf-8", newline="\n"
    )
    plan = load_target_test_plan(tmp_path, "sim", identity="sim", adapter="cocotb")
    runs = plan.resolve(("alpha@7", "beta@7"))

    def sim_run_names(flags: tuple[str, ...]) -> list[str]:
        request = sim_job_request(plan, runs, flags=flags)
        return [key.test_run for key in phase_keys(request) if key.phase == "sim_run"]

    assert sim_run_names(()) == ["alpha,beta"]
    assert sim_run_names(("coverage",)) == ["alpha", "beta"]

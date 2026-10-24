"""The baseline store and Job runner serve any Goal family through its adapter.

The SIM adapter keeps today's on-disk layout; a test-only ``probe`` family
runs through request, Job and evidence without editing the generic code.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from booley.goals import sim_baseline_store as store
from booley.goals.freshness import DEFAULT_RESOLVERS
from booley.goals.paths import record_paths
from booley.goals.store import GoalStore
from booley.mcp import sim_baseline_execution as execution
from booley.mcp import sim_baseline_jobs as jobs
from booley.runtime import job_records
from tests.goals.conftest import SMOKE_RUN, enter_goals, run_row

pytestmark = pytest.mark.timeout(120)

PROBE_KEY = "lint_probe_baseline"
PROBE_UNIT = {"name": "probe_check", "fingerprint": "probe-fingerprint"}


@pytest.fixture
def baseline(layout):
    record = enter_goals(layout, [{"family": "SIM", "target": "top"}])
    return SimpleNamespace(**vars(layout), record=record)


def _probe_result_key(target: str, unit: Any, stamp: str, protected: str) -> dict[str, str]:
    return {
        "target": target,
        "name": unit["name"],
        "fingerprint": unit["fingerprint"],
        "contents": stamp,
        "protected_inputs": protected,
    }


def _probe_family() -> Any:
    """A second family: dict units and a freshness key with fewer categories than SIM."""
    return store.BaselineFamily(
        name="probe",
        child_stem="probe",
        freshness_key=PROBE_KEY,
        goals=store.sim_goals,
        runs=lambda _record, _goal: (PROBE_UNIT,),
        unit_document=dict,
        parse_unit=dict,
        result_key=_probe_result_key,
    )


def _probe_executor() -> Any:
    """Run every unit and stamp the contents with the probe's own freshness key."""

    def classify(checkout: Path, request: Any, units: tuple[Any, ...]) -> Any:
        return "::top:0#top", [], units

    def measure(request: Any) -> dict[str, Any]:
        fingerprint = DEFAULT_RESOLVERS.fingerprint(Path(request["checkout"]), target="top")
        rows = [{**unit, "grade": "pass"} for unit in request["identity"]["runs"]]
        return {
            "schema": request["schema"],
            "rows": rows,
            "stamps": [execution.contents_stamp(fingerprint, PROBE_KEY)],
            "reason": "",
        }

    return execution.BaselineExecutor(
        classify=classify, measure=measure, job_class=lambda: "heavy"
    )


def _register_probe(monkeypatch):
    """Register the probe family in the store and executor registries."""
    family = _probe_family()
    monkeypatch.setattr(store, "BASELINE_FAMILIES", (store.SIM_BASELINE, family))
    monkeypatch.setitem(execution.EXECUTORS, "probe", _probe_executor())
    return family


def _schedule(baseline, family, published):
    """Schedule one family's baseline; *published* collects each Job record before launch."""

    def launch(path, request):
        published.append(job_records.read_record(request["job_id"], Path(request["jobs_root"])))

    jobs.schedule_family_baselines(baseline.control, baseline.record, family, launch=launch)
    goal = family.goals(baseline.record)[0]
    inputs = store.entry_inputs(baseline.control, baseline.record)
    identity = store.request_identity(baseline.record, goal, inputs, family)
    path = store.request_path(baseline.control, identity, family)
    return path, store.read_document(path, family)


def test_sim_family_keeps_its_on_disk_layout(baseline, monkeypatch):
    family = store.SIM_BASELINE
    assert (family.schema, family.root_name, family.job_prefix, family.endpoint) == (
        "booley.sim-baseline/v1",
        "sim-baselines",
        "sim-baseline-",
        "sim_baseline",
    )
    published: list[Any] = []
    path, request = _schedule(baseline, family, published)
    root = baseline.control / "goals" / "sim-baselines"
    digest = path.parent.name
    assert path == root / "requests" / digest / "request.json"
    assert request["schema"] == "booley.sim-baseline/v1"
    assert request["job_id"] == f"sim-baseline-{digest}"
    assert request["jobs_root"] == str(root / ".runtime" / "jobs")
    assert [job.endpoint for job in published] == ["sim_baseline"]
    inputs = record_paths(baseline.control, baseline.record.id).root / "sim-baseline-inputs.json"
    assert inputs.is_file()

    def execute(checkout, target, runs, output):
        stamp = execution.contents_stamp(DEFAULT_RESOLVERS.fingerprint(checkout, target=target))
        return {"rows": [run_row(run) for run in runs], "stamps": [stamp]}

    rows = execution.run_baseline(path, request, execute=execute)
    (evidence,) = (root / "evidence").glob("*.json")
    document = store.read_document(evidence)
    assert document["schema"] == "booley.sim-baseline/v1"
    assert evidence.name == store.content_digest(document["key"]) + ".json"
    assert rows[0]["grade"] == "pass"

    child = SimpleNamespace(pid=os.getpid(), wait=lambda **_kwargs: 0)
    store.write_document(path.parent / "simulation-result.json", {"schema": request["schema"]})
    monkeypatch.setattr(execution.subprocess, "Popen", lambda *_args, **_kwargs: child)
    monkeypatch.setattr(execution, "terminate_process_group", lambda *_args: None)
    execution.execute_child(baseline.worktree, "top", (SMOKE_RUN,), path.parent)
    sent = store.read_document(path.parent / "simulation-request.json")
    assert sent["schema"] == "booley.sim-baseline/v1" and sent["identity"]["runs"]


def test_second_family_runs_through_request_job_and_evidence(baseline, monkeypatch):
    probe = _register_probe(monkeypatch)
    published: list[Any] = []
    path, request = _schedule(baseline, probe, published)
    root = baseline.control / "goals" / "probe-baselines"
    digest = path.parent.name
    assert path == root / "requests" / digest / "request.json"
    assert request["schema"] == "booley.probe-baseline/v1"
    assert request["job_id"] == f"probe-baseline-{digest}"
    assert request["jobs_root"] == str(root / ".runtime" / "jobs")
    assert [job.endpoint for job in published] == ["probe_baseline"]

    # The real child entry point runs in this process in place of a detached worker.
    def popen(command, **_kwargs):
        return SimpleNamespace(pid=os.getpid(), wait=lambda **_kw: execution.main([command[-1]]))

    # Replace the module reference, not ``subprocess.Popen``: Git runs through it too.
    monkeypatch.setattr(execution, "subprocess", SimpleNamespace(Popen=popen))
    monkeypatch.setattr(execution, "terminate_process_group", lambda *_args: None)
    monkeypatch.setattr(
        execution, "_child_watchdog", lambda _output: SimpleNamespace(cancel=lambda: None)
    )
    monkeypatch.setattr(jobs.signal, "signal", lambda *_args: None)
    assert jobs.main([str(path)]) == 0

    report = store.read_document(path, probe)
    assert report["status"] == "complete"
    assert [row["name"] for row in report["results"]] == [PROBE_UNIT["name"]]
    job = job_records.read_record(request["job_id"], Path(request["jobs_root"]))
    assert job is not None and job.status == job_records.STATUS_DONE
    assert (path.parent / "probe-request.json").is_file()
    assert (path.parent / "probe-result.json").is_file()

    (evidence,) = (root / "evidence").glob("*.json")
    proof = store.read_document(evidence, probe)["proof"]
    fingerprint = DEFAULT_RESOLVERS.fingerprint(baseline.worktree, target="top")
    assert proof["contents"] == execution.contents_stamp(fingerprint, PROBE_KEY)
    assert proof["contents"] != execution.contents_stamp(fingerprint)
    assert not (baseline.control / "goals" / "sim-baselines").exists()


def test_job_roots_cover_every_registered_family(baseline, monkeypatch):
    probe = _register_probe(monkeypatch)
    from booley.harness.dashboard.jobs import retained_job_roots
    from booley.mcp.job_results import job_roots

    expected = [
        store.baseline_jobs_dir(baseline.control, family) for family in (store.SIM_BASELINE, probe)
    ]
    for root in expected:
        root.mkdir(parents=True)
    assert set(expected) <= set(retained_job_roots(baseline.control, (baseline.record,)))
    monkeypatch.setattr(
        "booley.mcp.job_results.project_goal_store", lambda: GoalStore(baseline.control)
    )
    assert set(expected) <= set(job_roots(container_root=lambda: None))

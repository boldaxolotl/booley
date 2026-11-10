"""Scheduling order and failure handling of simulation baseline Jobs (#1931)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.goals.sim_baseline_store import (
    baseline_observations,
    entry_inputs,
    read_document,
    request_identity,
    request_path,
    sim_goals,
    write_document,
)
from booley.mcp import sim_baseline_jobs
from booley.mcp.sim_baseline_jobs import schedule_sim_baselines
from booley.runtime import job_records
from booley.runtime.timefmt import utc_now_rfc3339
from tests.goals.conftest import enter_goals

pytestmark = pytest.mark.timeout(120)


@pytest.fixture
def baseline(layout):
    record = enter_goals(layout, [{"family": "SIM", "target": "top"}])
    return SimpleNamespace(**vars(layout), record=record)


def _request_file(baseline) -> Path:
    goal = sim_goals(baseline.record)[0]
    inputs = entry_inputs(baseline.control, baseline.record)
    return request_path(
        baseline.control, request_identity(baseline.control, baseline.record, goal, inputs)
    )


INTERRUPTED = "baseline Job interrupted or unavailable"


@pytest.fixture
def future_baseline(layout):
    """A SIM Goal with an empty wanted Test Run set, so ``complete`` carries no results."""
    record = enter_goals(layout, [{"family": "SIM", "target": "future", "test": "new"}])
    return SimpleNamespace(**vars(layout), record=record)


def _finish_on_first_job_read(monkeypatch, path: Path) -> None:
    """Interleave the supervisor's final writes between a reader's two reads.

    The first ``read_record`` call (the reader's Job read, after its request
    read) first runs the supervisor's terminal sequence: the final request,
    then the terminal Job record.
    """
    real_read = job_records.read_record
    armed = [True]

    def read_after_supervisor_finished(run_id, root):
        if armed[0]:
            armed[0] = False
            request = read_document(path, None)
            write_document(path, request | {"status": "complete", "results": []})
            job = real_read(run_id, root)
            assert job is not None
            job.status, job.exit_code, job.ended_at = (
                job_records.STATUS_DONE,
                0,
                utc_now_rfc3339(),
            )
            job_records.write_record(job, root)
        return real_read(run_id, root)

    monkeypatch.setattr(job_records, "read_record", read_after_supervisor_finished)


def test_reader_returns_a_request_completed_between_its_reads(future_baseline, monkeypatch):
    scene = future_baseline
    schedule_sim_baselines(scene.control, scene.record, launch=lambda _p, _r: None)
    path = _request_file(scene)
    _finish_on_first_job_read(monkeypatch, path)

    report = baseline_observations(scene.control, scene.record, sim_goals(scene.record)[0])

    assert report["status"] == "complete"
    assert report["job_id"] == read_document(path, None)["job_id"]


def test_reconciliation_keeps_a_request_completed_between_its_reads(future_baseline, monkeypatch):
    scene = future_baseline
    schedule_sim_baselines(scene.control, scene.record, launch=lambda _p, _r: None)
    path = _request_file(scene)
    _finish_on_first_job_read(monkeypatch, path)

    schedule_sim_baselines(scene.control, scene.record, launch=lambda _p, _r: None)

    assert json.loads(path.read_bytes())["status"] == "complete"


@pytest.mark.parametrize("ending", ["missing", "dead-process"])
def test_pending_request_with_an_ended_job_is_interrupted(baseline, ending):
    schedule_sim_baselines(baseline.control, baseline.record, launch=lambda _p, _r: None)
    path = _request_file(baseline)
    request = read_document(path, None)
    jobs_root = Path(request["jobs_root"])
    if ending == "missing":
        (jobs_root / f"{request['job_id']}.json").unlink()
    else:
        process = subprocess.Popen([sys.executable, "-c", "pass"])
        process.wait(timeout=30)
        job = job_records.read_record(request["job_id"], jobs_root)
        assert job is not None
        job.pid = process.pid
        job_records.write_record(job, jobs_root)
    goal = sim_goals(baseline.record)[0]

    report = baseline_observations(baseline.control, baseline.record, goal)
    assert report == {"status": "unavailable", "reason": INTERRUPTED}

    schedule_sim_baselines(baseline.control, baseline.record, launch=lambda _p, _r: None)
    persisted = json.loads(path.read_bytes())
    assert (persisted["status"], persisted["reason"]) == ("unavailable", INTERRUPTED)


def test_startup_failure_publishes_the_request_before_the_job_record(baseline, monkeypatch):
    schedule_sim_baselines(baseline.control, baseline.record, launch=lambda _p, _r: None)
    path = _request_file(baseline)
    seen: list[tuple[str, str]] = []
    real_write = job_records.write_record

    def spy(rec, root):
        if rec.status == job_records.STATUS_FAILED:
            seen.append((rec.status, json.loads(path.read_bytes())["status"]))
        real_write(rec, root)

    monkeypatch.setattr(job_records, "write_record", spy)

    sim_baseline_jobs._reap_unstarted(SimpleNamespace(wait=lambda: 3), path)

    assert seen == [(job_records.STATUS_FAILED, "unavailable")]
    request = json.loads(path.read_bytes())
    assert "exited before initialization (exit 3)" in request["reason"]


def test_unreadable_job_record_never_persists_unavailable_for_a_live_job(baseline, monkeypatch):
    # The first call publishes the Job record and the pending request; the second
    # one reconciles an existing request while the record is held open.
    schedule_sim_baselines(baseline.control, baseline.record, launch=lambda _p, _r: None)
    path = _request_file(baseline)

    def held(_run_id: str, _root: Path | None) -> None:
        raise PermissionError(13, "record held open")

    monkeypatch.setattr(job_records, "read_record", held)
    schedule_sim_baselines(baseline.control, baseline.record, launch=lambda _p, _r: None)
    assert json.loads(path.read_bytes())["status"] == "pending"


def test_job_record_write_failure_publishes_unavailable_with_its_cause(baseline, monkeypatch):
    published: list[str] = []
    real_write = sim_baseline_jobs.write_document

    def spy(path, value):
        published.append(value["status"])
        real_write(path, value)

    def refuse(_rec, _root):
        raise PermissionError(13, "record held open")

    launched: list[Path] = []
    monkeypatch.setattr(sim_baseline_jobs, "write_document", spy)
    monkeypatch.setattr(job_records, "write_record_strict", refuse)
    schedule_sim_baselines(
        baseline.control, baseline.record, launch=lambda path, _r: launched.append(path)
    )

    report = baseline_observations(
        baseline.control, baseline.record, sim_goals(baseline.record)[0]
    )
    assert "pending" not in published
    assert launched == []
    assert report["status"] == "unavailable"
    assert "record held open" in report["reason"]

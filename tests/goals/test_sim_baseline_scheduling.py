"""Scheduling order and failure handling of simulation baseline Jobs (#1931)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.goals.sim_baseline_store import (
    entry_inputs,
    request_identity,
    request_path,
    sim_goals,
)
from booley.goals.sim_transitions import baseline_observations
from booley.mcp import sim_baseline_jobs
from booley.mcp.sim_baseline_jobs import schedule_sim_baselines
from booley.runtime import job_records
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

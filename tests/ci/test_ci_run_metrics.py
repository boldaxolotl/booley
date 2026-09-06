"""Contracts for GitHub Actions queue and runner-time telemetry."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(_ROOT / ".github/scripts"))

from ci_run_metrics import MetricsError, summarize


def _run() -> dict[str, object]:
    return {
        "id": 42,
        "run_attempt": 2,
        "created_at": "2026-09-06T10:00:00Z",
        "run_started_at": "2026-09-06T10:00:03Z",
    }


def _jobs() -> dict[str, object]:
    return {
        "jobs": [
            {
                "created_at": "2026-09-06T10:00:00Z",
                "started_at": "2026-09-06T10:00:02Z",
                "completed_at": "2026-09-06T10:01:32Z",
            },
            {
                "created_at": "2026-09-06T10:01:32Z",
                "started_at": "2026-09-06T10:01:42Z",
                "completed_at": "2026-09-06T10:04:42Z",
            },
            {
                "created_at": "2026-09-06T10:01:32Z",
                "started_at": "2026-09-06T10:01:34Z",
                "completed_at": None,
            },
        ]
    }


def test_summarizes_queue_and_runner_time() -> None:
    metrics = summarize(_run(), _jobs(), "2026-09-06T10:05:00Z")

    assert metrics["workflow_queue_seconds"] == 3
    assert metrics["workflow_elapsed_seconds"] == 300
    assert metrics["completed_jobs"] == 2
    assert metrics["runner_minutes"] == 4.5
    assert metrics["rounded_job_minutes_estimate"] == 5
    assert metrics["job_queue_seconds"] == {"median": 6, "p90": 10, "max": 10}


def test_rejects_negative_timing_evidence() -> None:
    jobs = _jobs()
    jobs["jobs"][0]["started_at"] = "2026-09-06T10:00:05Z"  # type: ignore[index]
    jobs["jobs"][0]["completed_at"] = "2026-09-06T10:00:04Z"  # type: ignore[index]

    with pytest.raises(MetricsError, match="cannot be negative"):
        summarize(_run(), jobs, "2026-09-06T10:05:00Z")


def test_rejects_missing_jobs_list() -> None:
    with pytest.raises(MetricsError, match="jobs list"):
        summarize(_run(), {}, "2026-09-06T10:05:00Z")

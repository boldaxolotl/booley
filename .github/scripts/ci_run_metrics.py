#!/usr/bin/env python3
"""Summarize GitHub Actions queue time and consumed runner time."""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime
from pathlib import Path
from statistics import median
from typing import Any


class MetricsError(ValueError):
    """Raised when GitHub timing evidence is incomplete or malformed."""


def _timestamp(value: Any, *, field: str) -> datetime:
    if not isinstance(value, str):
        raise MetricsError(f"{field} must be an RFC 3339 timestamp")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise MetricsError(f"{field} must be an RFC 3339 timestamp") from error


def _seconds(start: Any, end: Any, *, field: str) -> float:
    duration = (
        _timestamp(end, field=f"{field}.end") - _timestamp(start, field=f"{field}.start")
    ).total_seconds()
    if duration < 0:
        raise MetricsError(f"{field} duration cannot be negative")
    return duration


def _percentile(values: list[float], proportion: float) -> float:
    if not values:
        return 0.0
    return sorted(values)[math.ceil(len(values) * proportion) - 1]


def _completed_current_attempt_jobs(jobs: list[Any]) -> list[dict[str, Any]]:
    """Return completed jobs whose timestamps belong to the current attempt.

    GitHub's failed-job rerun endpoint includes carried-over jobs from the prior
    attempt. It rewrites their ``created_at`` to the rerun time while retaining
    the original start and completion timestamps. Skipped jobs may likewise
    retain an old completion time. Neither kind consumed runner time in the
    current attempt, so omit them from its telemetry.
    """
    completed: list[dict[str, Any]] = []
    for job in jobs:
        if (
            not isinstance(job, dict)
            or not job.get("completed_at")
            or job.get("conclusion") == "skipped"
        ):
            continue
        created = _timestamp(job.get("created_at"), field="job queue.start")
        started = _timestamp(job.get("started_at"), field="job queue.end")
        if started < created:
            continue
        completed.append(job)
    return completed


def summarize(run: Any, jobs_payload: Any, observed_at: str) -> dict[str, Any]:
    """Build stable run-level metrics from GitHub's run and jobs responses."""
    if not isinstance(run, dict) or not isinstance(jobs_payload, dict):
        raise MetricsError("run and jobs payloads must be objects")
    jobs = jobs_payload.get("jobs")
    if not isinstance(jobs, list):
        raise MetricsError("jobs payload must contain a jobs list")
    completed = _completed_current_attempt_jobs(jobs)
    runtime_seconds = [
        _seconds(job.get("started_at"), job.get("completed_at"), field="job runtime")
        for job in completed
    ]
    queue_seconds = [
        _seconds(job.get("created_at"), job.get("started_at"), field="job queue")
        for job in completed
    ]
    workflow_queue = _seconds(
        run.get("created_at"), run.get("run_started_at"), field="workflow queue"
    )
    elapsed = _seconds(run.get("created_at"), observed_at, field="workflow elapsed")
    return {
        "schema": 1,
        "run_id": run.get("id"),
        "run_attempt": run.get("run_attempt"),
        "observed_at": observed_at,
        "workflow_queue_seconds": workflow_queue,
        "workflow_elapsed_seconds": elapsed,
        "completed_jobs": len(completed),
        "runner_minutes": sum(runtime_seconds) / 60,
        "rounded_job_minutes_estimate": sum(math.ceil(value / 60) for value in runtime_seconds),
        "job_queue_seconds": {
            "median": median(queue_seconds) if queue_seconds else 0.0,
            "p90": _percentile(queue_seconds, 0.9),
            "max": max(queue_seconds, default=0.0),
        },
    }


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise MetricsError(f"cannot read {path}: {error}") from error


def _write_summary(path: Path, metrics: dict[str, Any]) -> None:
    queue = metrics["job_queue_seconds"]
    rows = (
        ("Workflow queue", f"{metrics['workflow_queue_seconds']:.1f} s"),
        (
            "Job queue p50 / p90 / max",
            f"{queue['median']:.1f} / {queue['p90']:.1f} / {queue['max']:.1f} s",
        ),
        ("Runner time", f"{metrics['runner_minutes']:.2f} min"),
        ("Rounded job-minute estimate", str(metrics["rounded_job_minutes_estimate"])),
        ("Workflow elapsed at observation", f"{metrics['workflow_elapsed_seconds']:.1f} s"),
    )
    with path.open("a", encoding="utf-8") as stream:
        print("## CI timing\n\n| Metric | Value |\n| --- | --- |", file=stream)
        for label, value in rows:
            print(f"| {label} | {value} |", file=stream)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-json", type=Path, required=True)
    parser.add_argument("--jobs-json", type=Path, required=True)
    parser.add_argument("--observed-at", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--summary", type=Path)
    args = parser.parse_args()
    try:
        metrics = summarize(
            _read_json(args.run_json), _read_json(args.jobs_json), args.observed_at
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(metrics, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        if args.summary is not None:
            _write_summary(args.summary, metrics)
    except MetricsError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(json.dumps(metrics, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

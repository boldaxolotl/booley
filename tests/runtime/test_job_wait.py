"""Tests for generic detached-job waiting."""

from __future__ import annotations

from pathlib import Path

import pytest

from booley.runtime import job_records as jobrec
from booley.runtime import job_wait


def _record() -> jobrec.JobRecord:
    return jobrec.JobRecord(
        run_id="mutation_tester-x-1",
        endpoint="mutation_tester",
        started_at="2026-08-10T08:00:00Z",
        timeout_s=60,
        pid=1234,
    )


@pytest.mark.asyncio
async def test_waits_until_job_is_terminal(tmp_path: Path, monkeypatch):
    states = iter([[_record()], []])
    monkeypatch.setattr(job_wait, "active_jobs", lambda _root: next(states))

    waited = await job_wait.wait_for_jobs(
        tmp_path,
        poll_interval=0,
        max_wait_seconds=1,
    )

    assert [rec.run_id for rec in waited] == ["mutation_tester-x-1"]


@pytest.mark.asyncio
async def test_timeout_names_jobs_that_are_still_active(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(job_wait, "active_jobs", lambda _root: [_record()])

    with pytest.raises(job_wait.JobWaitTimeoutError, match="mutation_tester-x-1"):
        await job_wait.wait_for_jobs(tmp_path, max_wait_seconds=0)


@pytest.mark.parametrize(
    "contents",
    [
        "{not json",
        '{"run_id":"job","endpoint":"sim","started_at":"bad","timeout_s":60}',
        '{"run_id":"job","endpoint":"sim","started_at":"2999-01-01T00:00:00Z","timeout_s":60}',
        '{"run_id":"job","endpoint":"sim","started_at":"2026-08-10T08:00:00Z","timeout_s":0}',
        '{"run_id":"job","endpoint":"sim","started_at":"2026-08-10T08:00:00Z",'
        '"timeout_s":60,"argv":[1]}',
        '{"run_id":"job","endpoint":"sim","started_at":"2026-08-10T08:00:00Z",'
        '"timeout_s":60,"status":"unknown"}',
        '{"run_id":"job","endpoint":"sim","started_at":"2026-08-10T08:00:00Z",'
        '"timeout_s":60,"pid":0}',
        '{"run_id":"job","endpoint":"sim","started_at":"2026-08-10T08:00:00Z",'
        '"timeout_s":60,"exit_code":"bad"}',
        '{"run_id":"job","endpoint":"sim","started_at":"2026-08-10T08:00:00Z",'
        '"timeout_s":60,"run_started_at":"bad"}',
        '{"run_id":"job","endpoint":"sim","started_at":"2026-08-10T08:00:00Z",'
        '"timeout_s":60,"run_started_at":"2999-01-01T00:00:00Z"}',
        '{"run_id":"job","endpoint":"sim","started_at":"2026-08-10T08:00:00Z",'
        '"timeout_s":60,"run_started_at":"2000-01-01T00:00:00Z"}',
        '{"run_id":"job","endpoint":"sim","started_at":"2026-08-10T08:00:00Z",'
        '"timeout_s":"unbounded"}',
    ],
)
def test_active_jobs_fails_closed_on_malformed_records(tmp_path: Path, contents: str) -> None:
    tmp_path.joinpath("job.json").write_text(contents, encoding="utf-8")

    with pytest.raises(jobrec.JobRecordError, match="repair or removal"):
        job_wait.active_jobs(tmp_path)


def test_strict_records_returns_valid_records(tmp_path: Path) -> None:
    record = _record()
    jobrec.write_record(record, tmp_path)

    assert jobrec.strict_records(tmp_path) == [record]

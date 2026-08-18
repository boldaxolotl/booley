"""Tests for the detached ticket-job finalization fence."""

from __future__ import annotations

from pathlib import Path

import pytest

from booley.harness import job_fence
from booley.mcp_tools import job_records as jobrec


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
    monkeypatch.setattr(job_fence, "active_ticket_jobs", lambda _log_dir: next(states))

    waited = await job_fence.wait_for_ticket_jobs(
        tmp_path,
        poll_interval=0,
        max_wait_seconds=1,
    )

    assert [rec.run_id for rec in waited] == ["mutation_tester-x-1"]


@pytest.mark.asyncio
async def test_timeout_names_jobs_that_are_still_active(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(job_fence, "active_ticket_jobs", lambda _log_dir: [_record()])

    with pytest.raises(job_fence.TicketJobFenceTimeoutError, match="mutation_tester-x-1"):
        await job_fence.wait_for_ticket_jobs(tmp_path, max_wait_seconds=0)

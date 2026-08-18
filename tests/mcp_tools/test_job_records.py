"""Tests for the durable job-record store (ADR 0027 async submit/poll)."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

import pytest

from booley.mcp_tools import job_records as jobrec


@pytest.fixture()
def _jobs_env(tmp_path: Path, monkeypatch):
    """Point the runtime at a tmp tree so job records land under tmp/jobs/."""
    monkeypatch.setenv("BOOLEY_LOGS_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("BOOLEY_RUNTIME_DIR", str(tmp_path))
    return tmp_path


class TestTerminalStatus:
    def test_clean_exit_is_done(self):
        assert jobrec.terminal_status(0, timed_out=False) == jobrec.STATUS_DONE

    def test_nonzero_exit_is_failed(self):
        assert jobrec.terminal_status(1, timed_out=False) == jobrec.STATUS_FAILED

    def test_timeout_is_failed_even_on_zero(self):
        # A timeout-killed run must never read as success.
        assert jobrec.terminal_status(0, timed_out=True) == jobrec.STATUS_FAILED


class TestRunId:
    def test_is_session_unique_by_counter(self):
        a = jobrec.make_run_id("sim", "20260704T131502", 1)
        b = jobrec.make_run_id("sim", "20260704T131502", 2)
        assert a != b
        assert a.startswith("sim-")


class TestRecordRoundTrip:
    def test_write_then_read(self, _jobs_env):
        rec = jobrec.JobRecord(
            run_id="simulate-x-1",
            endpoint="sim",
            started_at="2026-07-04T13:15:02Z",
            timeout_s=1290,
            argv=["python", "-m", "booley.flows.simulate"],
            pid=4242,
        )
        jobrec.write_record(rec)
        got = jobrec.read_record("simulate-x-1")
        assert got is not None
        assert got.endpoint == "sim"
        assert got.pid == 4242
        assert got.status == jobrec.STATUS_RUNNING  # default
        assert got.argv == ["python", "-m", "booley.flows.simulate"]

    def test_read_missing_returns_none(self, _jobs_env):
        assert jobrec.read_record("nope-1") is None

    def test_malformed_record_returns_none(self, _jobs_env):
        root = jobrec.jobs_dir()
        assert root is not None
        root.mkdir(parents=True, exist_ok=True)
        (root / "broken-1.json").write_text("{not json", encoding="utf-8")
        assert jobrec.read_record("broken-1") is None

    def test_list_records(self, _jobs_env):
        for i in (1, 2):
            jobrec.write_record(
                jobrec.JobRecord(
                    run_id=f"simulate-x-{i}",
                    endpoint="sim",
                    started_at="t",
                    timeout_s=60,
                )
            )
        run_ids = {r.run_id for r in jobrec.list_records()}
        assert run_ids == {"simulate-x-1", "simulate-x-2"}

    def test_list_records_from_explicit_root(self, tmp_path: Path):
        root = tmp_path / "other-jobs"
        root.mkdir()
        (root / "sim-x-1.json").write_text(
            '{"run_id":"sim-x-1","endpoint":"sim","started_at":"t","timeout_s":60}\n',
            encoding="utf-8",
        )
        assert [rec.run_id for rec in jobrec.list_records(root)] == ["sim-x-1"]

    def test_overwrite_is_atomic_and_leaves_no_tmp(self, _jobs_env):
        # write_record goes through tmp + os.replace so a cross-process reader
        # can never observe a half-written record; the tmp must not linger.
        rec = jobrec.JobRecord(
            run_id="simulate-x-1",
            endpoint="sim",
            started_at="t",
            timeout_s=60,
        )
        jobrec.write_record(rec)
        rec.status = jobrec.STATUS_DONE
        rec.exit_code = 0
        jobrec.write_record(rec)
        got = jobrec.read_record("simulate-x-1")
        assert got is not None and got.status == jobrec.STATUS_DONE
        root = jobrec.jobs_dir()
        assert root is not None
        assert not list(root.glob("*.tmp"))


class TestJobsDir:
    def test_none_without_logs_dir(self, monkeypatch):
        monkeypatch.delenv("BOOLEY_LOGS_DIR", raising=False)
        assert jobrec.jobs_dir() is None

    def test_write_is_noop_without_runtime(self, monkeypatch):
        monkeypatch.delenv("BOOLEY_LOGS_DIR", raising=False)
        # Best-effort: must not raise even with nowhere to write.
        jobrec.write_record(
            jobrec.JobRecord(
                run_id="x",
                endpoint="sim",
                started_at="t",
                timeout_s=1,
            )
        )
        assert jobrec.read_record("x") is None


class TestDeriveStatus:
    def _rec(self, **kw):
        base = {"run_id": "r", "endpoint": "sim", "started_at": "t", "timeout_s": 60}
        base.update(kw)
        return jobrec.JobRecord(**base)

    def test_cancelled_is_terminal(self):
        rec = self._rec(status=jobrec.STATUS_CANCELLED, exit_code=130)
        assert jobrec.derive_status(rec, lambda _pid: True) == jobrec.STATUS_CANCELLED

    def test_live_record_is_active(self):
        rec = self._rec(pid=1234)
        assert jobrec.is_active(rec, lambda _pid: True)

    def test_fresh_pidless_record_is_active(self):
        stamp = "2027-01-15T08:00:00Z"
        started = jobrec.parse_stamp(stamp)
        assert started is not None
        rec = self._rec(pid=None, started_at=stamp)
        assert jobrec.is_active(rec, lambda _pid: False, now=started + 1)

    def test_stale_pidless_record_is_not_active(self):
        stamp = "2027-01-15T08:00:00Z"
        started = jobrec.parse_stamp(stamp)
        assert started is not None
        rec = self._rec(pid=None, started_at=stamp)
        assert not jobrec.is_active(
            rec,
            lambda _pid: False,
            now=started + jobrec.SPAWN_GRACE_SECONDS + 1,
        )

    def test_terminal_record_is_authoritative(self):
        rec = self._rec(status=jobrec.STATUS_DONE, exit_code=0, pid=1)
        # Even a live PID cannot override a recorded terminal outcome.
        assert jobrec.derive_status(rec, lambda _pid: True) == jobrec.STATUS_DONE

    def test_running_with_live_pid_stays_running(self):
        rec = self._rec(status=jobrec.STATUS_RUNNING, pid=1234)
        assert jobrec.derive_status(rec, lambda _pid: True) == jobrec.STATUS_RUNNING

    def test_running_with_dead_pid_is_failed(self):
        rec = self._rec(status=jobrec.STATUS_RUNNING, pid=1234)
        assert jobrec.derive_status(rec, lambda _pid: False) == jobrec.STATUS_FAILED

    def test_running_without_pid_is_failed(self):
        # No PID yet means the spawn never happened → nothing to wait on.
        rec = self._rec(status=jobrec.STATUS_RUNNING, pid=None)
        assert jobrec.derive_status(rec, lambda _pid: True) == jobrec.STATUS_FAILED

    # -- deadline guard: a live PID cannot keep a job "running" forever --------

    _STARTED = "2026-07-04T13:15:02Z"

    def _epoch(self) -> float:
        started = jobrec.parse_stamp(self._STARTED)
        assert started is not None
        return started

    def test_running_past_deadline_is_failed_despite_live_pid(self):
        # Past started_at + timeout_s + slack nothing can ever finish this job
        # (the watchdog died with the server) — a live PID is reuse or a hung
        # unsupervised orphan, and either way the caller must not poll forever.
        rec = self._rec(
            status=jobrec.STATUS_RUNNING, pid=1234, started_at=self._STARTED, timeout_s=60
        )
        late = self._epoch() + 60 + jobrec.DEADLINE_SLACK_SECONDS + 1
        assert (
            jobrec.derive_status(
                rec,
                lambda _pid: True,
                now=late,
            )
            == jobrec.STATUS_FAILED
        )

    def test_running_within_deadline_stays_running(self):
        rec = self._rec(
            status=jobrec.STATUS_RUNNING, pid=1234, started_at=self._STARTED, timeout_s=60
        )
        early = self._epoch() + 60 + jobrec.DEADLINE_SLACK_SECONDS - 1
        assert (
            jobrec.derive_status(
                rec,
                lambda _pid: True,
                now=early,
            )
            == jobrec.STATUS_RUNNING
        )

    def test_unparseable_started_at_skips_deadline(self):
        # Best-effort: a garbage stamp must not crash or fail a live job.
        rec = self._rec(status=jobrec.STATUS_RUNNING, pid=1234, started_at="t")
        assert (
            jobrec.derive_status(
                rec,
                lambda _pid: True,
                now=9e18,
            )
            == jobrec.STATUS_RUNNING
        )

    # -- identity guard: the PID must still be OUR process ---------------------

    _ARGV: ClassVar[list[str]] = ["python", "-m", "booley.flows.simulate", "--cfg", "top"]

    def test_recycled_pid_is_failed(self):
        # Container PID namespaces are dense: after a restart the recorded PID
        # can belong to an unrelated process. argv mismatch → not our job.
        rec = self._rec(status=jobrec.STATUS_RUNNING, pid=1234, argv=self._ARGV)
        assert (
            jobrec.derive_status(
                rec,
                lambda _pid: True,
                read_cmdline=lambda _pid: ["sleep", "3600"],
            )
            == jobrec.STATUS_FAILED
        )

    def test_matching_cmdline_stays_running(self):
        rec = self._rec(status=jobrec.STATUS_RUNNING, pid=1234, argv=self._ARGV)
        assert (
            jobrec.derive_status(
                rec,
                lambda _pid: True,
                read_cmdline=lambda _pid: list(self._ARGV),
            )
            == jobrec.STATUS_RUNNING
        )

    def test_unreadable_cmdline_is_trusted_alive(self):
        # None means "cannot judge" (no /proc, permission, raced exit) — must
        # not be treated as a mismatch or live jobs die on non-Linux hosts.
        rec = self._rec(status=jobrec.STATUS_RUNNING, pid=1234, argv=self._ARGV)
        assert (
            jobrec.derive_status(
                rec,
                lambda _pid: True,
                read_cmdline=lambda _pid: None,
            )
            == jobrec.STATUS_RUNNING
        )


class TestParseStamp:
    def test_roundtrips_record_format(self):
        assert jobrec.parse_stamp("2026-07-04T13:15:02Z") is not None

    def test_garbage_and_non_string_are_none(self):
        assert jobrec.parse_stamp("t") is None
        assert jobrec.parse_stamp(None) is None
        assert jobrec.parse_stamp(1234) is None


class TestDeadlineAnchorsAtRunStart:
    """Queue wait (ADR 0028) must not count against the derive_status deadline."""

    _SUBMITTED = "2026-07-04T13:15:02Z"
    _RUN_STARTED = "2026-07-04T13:45:02Z"  # promoted 30 min after submit

    def _rec(self):
        return jobrec.JobRecord(
            run_id="r",
            endpoint="sim",
            started_at=self._SUBMITTED,
            timeout_s=60,
            pid=1234,
            status=jobrec.STATUS_RUNNING,
            run_started_at=self._RUN_STARTED,
        )

    def test_within_run_anchored_deadline_stays_running(self):
        # Well past submit + budget + slack, but inside run_start + budget:
        # the job queued for its slot, and that wait is not run time.
        run_started = jobrec.parse_stamp(self._RUN_STARTED)
        assert run_started is not None
        now = run_started + 30
        assert (
            jobrec.derive_status(self._rec(), lambda _pid: True, now=now) == jobrec.STATUS_RUNNING
        )

    def test_past_run_anchored_deadline_is_failed(self):
        run_started = jobrec.parse_stamp(self._RUN_STARTED)
        assert run_started is not None
        late = run_started + 60 + jobrec.DEADLINE_SLACK_SECONDS + 1
        assert (
            jobrec.derive_status(self._rec(), lambda _pid: True, now=late) == jobrec.STATUS_FAILED
        )

    def test_run_started_at_round_trips_through_disk(self, _jobs_env):
        rec = self._rec()
        jobrec.write_record(rec)
        loaded = jobrec.read_record("r")
        assert loaded is not None
        assert loaded.run_started_at == self._RUN_STARTED

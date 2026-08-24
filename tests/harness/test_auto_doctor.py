"""Tests for stale-triggered automatic Doctor execution and reporting."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from booley.harness import auto_doctor, doctor, doctor_stamp
from booley.runtime.project_dir import reset_cache


@pytest.fixture(autouse=True)
def _isolated_project_resolution(monkeypatch):
    monkeypatch.delenv("BOOLEY_PROJECT_DIR", raising=False)
    reset_cache()
    yield
    reset_cache()


def _project(root: Path) -> Path:
    project_dir = root / ".booley_project"
    project_dir.mkdir()
    (project_dir / "booley.toml").write_text("[project]\nname = 'unit'\n", encoding="utf-8")
    return project_dir


def _report(root: Path, *, clean: bool, checked_at: datetime) -> dict:
    project_dir = root / ".booley_project"
    findings = []
    counts = {"pass": 1, "fail": 0, "warn": 0, "waived": 0, "note": 0, "skip": 0}
    if not clean:
        counts.update({"pass": 0, "fail": 1})
        findings = [
            {
                "severity": "fail",
                "message": "sandbox image missing",
                "fix": "run booley init",
                "check_id": None,
                "subject": None,
            }
        ]
    payload = {
        "checked_at": checked_at.strftime(auto_doctor._TIME_FORMAT),
        "clean": clean,
        "counts": counts,
        "findings": findings,
        "fingerprint": auto_doctor.compute_fingerprint(project_dir, root),
    }
    payload["finding_hash"] = auto_doctor._finding_hash(payload)
    path = auto_doctor.report_path(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return payload


class TestDueReason:
    def test_missing_result_is_due_without_clean_manual_stamp(self, tmp_path: Path):
        _project(tmp_path)

        assert auto_doctor.due_reason(tmp_path) == "no automatic Doctor result"

    def test_manual_stamp_does_not_replace_runtime_scoped_result(self, tmp_path: Path):
        project_dir = _project(tmp_path)
        doctor_stamp.record_clean_run(project_dir, tmp_path, deep=False)

        assert auto_doctor.due_reason(tmp_path) == "no automatic Doctor result"

    def test_unhealthy_result_retries_daily(self, tmp_path: Path):
        _project(tmp_path)
        now = datetime.now(tz=UTC)
        _report(tmp_path, clean=False, checked_at=now - timedelta(hours=23))

        assert auto_doctor.due_reason(tmp_path, now=now) is None

        _report(tmp_path, clean=False, checked_at=now - timedelta(hours=25))
        assert auto_doctor.due_reason(tmp_path, now=now) == "automatic Doctor result expired"

    def test_config_change_invalidates_result(self, tmp_path: Path):
        project_dir = _project(tmp_path)
        _report(tmp_path, clean=True, checked_at=datetime.now(tz=UTC))
        (project_dir / "booley.toml").write_text("[project]\nname = 'edited'\n", encoding="utf-8")

        assert auto_doctor.due_reason(tmp_path) == "Doctor inputs changed"


def test_execute_persists_structured_report_and_transcript(tmp_path: Path, monkeypatch):
    project_dir = _project(tmp_path)
    result = doctor.DoctorRunResult(
        counts={"pass": 2, "fail": 0, "warn": 1, "waived": 0, "note": 0, "skip": 0},
        findings=(
            doctor.DoctorFinding(
                "warn", "license may expire", "renew it", "agent.credential", "claude"
            ),
        ),
        exit_code=0,
    )

    def run_doctor(*_args, progress=None, **_kwargs):
        print("captured Doctor finding")
        progress("host/project checks")
        return result

    monkeypatch.setattr(doctor, "run_doctor_result", run_doctor)
    progress = []

    payload = auto_doctor._execute(
        tmp_path,
        project_dir,
        "test",
        progress=progress.append,
    )

    assert payload["clean"] is False
    assert payload["trigger"] == "test"
    assert payload["findings"][0]["check_id"] == "agent.credential"
    assert auto_doctor.load_report(tmp_path) == payload
    assert auto_doctor.transcript_path(project_dir).is_file()
    assert "captured Doctor finding" in auto_doctor.transcript_path(project_dir).read_text()
    assert progress[0] == "host/project checks"
    assert progress[-1].startswith("completed in ")
    assert "transcript:" in progress[-1]


def test_manual_runtime_result_replaces_stale_automatic_status(tmp_path: Path):
    _project(tmp_path)
    _report(tmp_path, clean=False, checked_at=datetime.now(tz=UTC))
    result = doctor.DoctorRunResult(
        counts={"pass": 3, "fail": 0, "warn": 0, "waived": 0, "note": 0, "skip": 0},
        findings=(doctor.DoctorFinding("pass", "environment healthy"),),
        exit_code=0,
    )

    payload = auto_doctor.record_manual_result(tmp_path, result)

    assert payload is not None and payload["clean"] is True
    assert payload["trigger"] == "manual-doctor"
    assert "clean" in auto_doctor.current_summary(tmp_path)


def test_manual_smoke_result_does_not_replace_health_status(tmp_path: Path):
    _project(tmp_path)
    previous = _report(tmp_path, clean=False, checked_at=datetime.now(tz=UTC))
    result = doctor.DoctorRunResult(
        counts={"pass": 3, "fail": 0, "warn": 0, "waived": 0, "note": 0, "skip": 3},
        findings=(doctor.DoctorFinding("skip", "agent credential checks skipped"),),
        exit_code=0,
        health_evidence=False,
    )

    assert auto_doctor.record_manual_result(tmp_path, result) is None
    assert auto_doctor.load_report(tmp_path) == previous


def test_changed_summary_is_consumed_once_per_channel(tmp_path: Path):
    _project(tmp_path)
    _report(tmp_path, clean=False, checked_at=datetime.now(tz=UTC))

    first = auto_doctor.consume_changed_summary(tmp_path, channel="mcp", issues_only=True)
    second = auto_doctor.consume_changed_summary(tmp_path, channel="mcp", issues_only=True)
    other = auto_doctor.consume_changed_summary(tmp_path, channel="runner", issues_only=True)

    assert first is not None and "1 FAIL" in first
    assert second is None
    assert other == first


def test_launch_skips_when_current(tmp_path: Path, monkeypatch):
    _project(tmp_path)
    _report(tmp_path, clean=True, checked_at=datetime.now(tz=UTC))
    spawned = []
    monkeypatch.setattr(auto_doctor.subprocess, "Popen", lambda *a, **k: spawned.append((a, k)))

    assert auto_doctor.launch(tmp_path) == "current"
    assert spawned == []


def test_launch_starts_detached_worker_when_due(tmp_path: Path, monkeypatch):
    _project(tmp_path)
    spawned = []
    monkeypatch.setattr(auto_doctor.subprocess, "Popen", lambda *a, **k: spawned.append((a, k)))

    assert auto_doctor.launch(tmp_path) == "started"
    assert spawned[0][1]["start_new_session"] is True
    assert "booley.harness.auto_doctor" in spawned[0][0][0]


def test_run_if_due_records_once_then_stays_current(tmp_path: Path, monkeypatch):
    _project(tmp_path)
    calls = []

    def execute(root, _project_dir, trigger, **_kwargs):
        calls.append(trigger)
        return _report(root, clean=True, checked_at=datetime.now(tz=UTC))

    monkeypatch.setattr(auto_doctor, "_execute", execute)
    monkeypatch.setattr(auto_doctor, "_notify_if_changed", lambda *_a: None)

    progress = []
    assert (
        auto_doctor.run_if_due(
            tmp_path,
            trigger="test",
            progress=progress.append,
        )
        is not None
    )
    assert auto_doctor.run_if_due(tmp_path, trigger="test") is None
    assert calls == ["test"]
    assert progress == ["starting (no automatic Doctor result)"]

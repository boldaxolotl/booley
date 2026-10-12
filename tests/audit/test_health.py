"""Shared interpretation and real consumers agree without executing Doctor probes."""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from booley.audit.diagnostic_results import DiagnosticFinding, DiagnosticReport, Severity
from booley.harness import auto_doctor, doctor, upgrade_review
from booley.harness.dashboard import model as dashboard
from booley.mcp import server
from booley.runtime import health, health_presentation
from booley.runtime.project_dir import reset_cache
from booley.runtime.timefmt import rfc3339_from_datetime

NOW = datetime(2026, 10, 12, 12, tzinfo=UTC)


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.delenv("BOOLEY_PROJECT_DIR", raising=False)
    reset_cache()
    directory = tmp_path / ".booley_project"
    directory.mkdir()
    (directory / "booley.toml").write_text('[project]\nname="health-test"\n')
    yield tmp_path, directory
    reset_cache()


def _result(severity: Severity) -> doctor.DoctorRunResult:
    reporter = doctor._Reporter.create()
    observation = DiagnosticFinding(
        severity,
        "probe unavailable" if severity is Severity.FAIL else "observed",
        "reviewed reason",
        "test.health",
        "fixture",
    )
    reporter.diagnostics(DiagnosticReport((observation,)))
    return reporter.result(reporter.finish())


def _write(root, directory, result, *, age=timedelta()):
    document = health.publish_result(
        root,
        directory,
        trigger="test",
        duration_s=0,
        counts=result.counts,
        diagnostics=result.diagnostics,
        exit_code=result.exit_code,
        transcript="probe text\n",
    )
    document["checked_at"] = rfc3339_from_datetime(NOW - age)
    health.report_path(directory).write_text(json.dumps(document))
    return document


@pytest.mark.parametrize(
    "severity,clean,exit_code",
    [
        (Severity.FAIL, False, 1),
        (Severity.WARN, False, 0),
        (Severity.WAIVED, True, 0),
        (Severity.PASS, True, 0),
    ],
)
def test_same_observations_reach_doctor_mcp_and_dashboard(
    project, monkeypatch, severity, clean, exit_code
):
    root, directory = project
    result = _result(severity)
    assert (result.clean, result.exit_code) == (clean, exit_code)
    assert doctor.record_manual_result(root, result) is not None
    snapshot = health.read_health(root)
    assert snapshot.diagnostics == result.diagnostics
    assert snapshot.assessment == result.diagnostics.health
    assert snapshot.due is None
    message = health_presentation.brief_message(snapshot)
    monkeypatch.chdir(root)
    monkeypatch.setattr(server, "_interactive_session_id", lambda: "fixture")
    monkeypatch.setattr(server, "format_status_line", lambda: "status")
    monkeypatch.setattr(server, "_status_mcp_tool_visible", lambda: True)
    monkeypatch.setattr(
        upgrade_review,
        "read_status",
        lambda _: upgrade_review.ReviewStatus(upgrade_review.ReviewCondition.CURRENT, "1", "path"),
    )
    assert "Health: " + message in server._format_status_card([])
    lines = dashboard.health_snapshot(root, directory)
    assert lines == (() if clean else (message + " · current",))
    content = [server.TextContent(type="text", text="Flow result")]
    announced = server._prepend_changed_health_alert(content)
    assert len(announced) == (1 if clean else 2)
    if not clean:
        assert announced[0].text == "HEALTH WARNING: " + message
    assert server._prepend_changed_health_alert(content) == content


@pytest.mark.parametrize(
    "severity,days", [(Severity.PASS, 7), (Severity.FAIL, 1), (Severity.WARN, 1)]
)
def test_expiry_exact_boundary(project, severity, days):
    root, directory = project
    _write(root, directory, _result(severity), age=timedelta(days=days))
    assert health.read_health(root, now=NOW - timedelta(seconds=1)).due is None
    assert health.read_health(root, now=NOW).due == "automatic Doctor result expired"


def test_upgrade_only_uses_weekly_cadence_and_drift_wins(project):
    root, directory = project
    result = _result(Severity.WARN)
    result = replace(
        result, findings=(replace(result.findings[0], check_id="upgrade.review-pending"),)
    )
    _write(root, directory, result, age=timedelta(days=7))
    assert health.read_health(root, now=NOW - timedelta(seconds=1)).due is None
    assert health.read_health(root, now=NOW).due == "automatic Doctor result expired"
    (directory / "booley.toml").write_text('[project]\nname="changed"\n')
    assert health.read_health(root, now=NOW).due == "Doctor inputs changed"


@pytest.mark.parametrize("value", [None, "invalid", 42])
def test_unreadable_time_keeps_findings_but_requires_recheck(project, value):
    root, directory = project
    document = _write(root, directory, _result(Severity.FAIL))
    document["checked_at"] = value
    health.report_path(directory).write_text(json.dumps(document))
    snapshot = health.read_health(root, now=NOW)
    assert snapshot.assessment.fail_count == 1
    assert snapshot.due == "automatic Doctor timestamp unreadable"


@pytest.mark.parametrize(
    "mutation",
    [
        {"schema": True},
        {"schema": 2},
        {"counts": {"fail": True, "warn": 0}},
        {"counts": {"fail": -1, "warn": 0}},
        {"counts": {"fail": 0, "warn": 0}},
        {"counts": {"fail": 1, "warn": "0"}},
        {"counts": {"warn": 0}},
        {"clean": True},
        {"clean": "false"},
        {"exit_code": 0},
        {"exit_code": True},
        {
            "counts": {"fail": 0, "warn": 0},
            "clean": True,
            "exit_code": 0,
            "findings": [{"severity": "waived", "message": "invalid waiver identity"}],
        },
        {"findings": None},
        {"findings": {}},
        {"findings": [1]},
        {"findings": [{"severity": "invalid", "message": "bad"}]},
        {"findings": [{"severity": "fail", "message": 1}]},
        {"findings": [{"severity": "fail", "message": "bad", "subject": []}]},
    ],
)
def test_malformed_or_contradictory_reports_are_unavailable(project, mutation):
    root, directory = project
    document = _write(root, directory, _result(Severity.FAIL))
    document.update(mutation)
    health.report_path(directory).write_text(json.dumps(document))
    assert health.read_health(root, now=NOW) is None
    assert health.due_reason(root, now=NOW) == "no automatic Doctor result"
    assert "not completed" in health_presentation.current_summary(root)
    assert health.consume_changed_snapshot(root, channel="test") is None


def test_legacy_optional_metadata_and_hash_shape_are_preserved(project):
    root, directory = project
    document = _write(root, directory, _result(Severity.WARN))
    active = document["findings"]
    expected = hashlib.sha256(
        json.dumps(active, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert document["finding_hash"] == expected
    assert set(active[0]) == {"severity", "message", "fix", "check_id", "subject"}
    for field in ("schema", "clean", "exit_code", "deep_status", "finding_hash"):
        document.pop(field)
    health.report_path(directory).write_text(json.dumps(document))
    assert health.read_health(root, now=NOW).assessment.warn_count == 1
    assert health.consume_changed_snapshot(root, channel="legacy") is not None


def test_snapshot_is_not_reloaded_during_render_or_changed_publication(project, monkeypatch):
    root, directory = project
    _write(root, directory, _result(Severity.FAIL))
    load = health.load_report
    reads = []

    def once(path):
        reads.append(path)
        return load(path)

    monkeypatch.setattr(health, "load_report", once)
    snapshot = health.read_health(root, now=NOW)
    health.report_path(directory).write_text("broken")
    assert "1 FAIL" in health_presentation.brief_message(snapshot)
    assert reads == [root]


@pytest.mark.parametrize("issues_only", [False, True])
def test_changed_health_channels_preserve_clean_recovery_semantics(project, issues_only):
    root, _directory = project
    issue = _result(Severity.FAIL)
    # Use actual present time so freshness is not involved in this notification test.
    doctor.record_manual_result(root, issue)
    assert health.consume_changed_snapshot(root, channel="one", issues_only=issues_only)
    assert health.consume_changed_snapshot(root, channel="one", issues_only=issues_only) is None
    assert health.consume_changed_snapshot(root, channel="other", issues_only=issues_only)
    doctor.record_manual_result(root, _result(Severity.PASS))
    recovered = health.consume_changed_snapshot(root, channel="one", issues_only=issues_only)
    assert (recovered is None) is issues_only
    doctor.record_manual_result(root, issue)
    recurrence = health.consume_changed_snapshot(root, channel="one", issues_only=issues_only)
    assert (recurrence is None) is issues_only


def test_missing_and_corrupt_reports_do_not_create_state(project):
    root, directory = project
    assert health.read_health(root) is None
    assert health.due_reason(root) == "no automatic Doctor result"
    assert health.consume_changed_snapshot(root, channel="one") is None
    assert not health.state_dir(directory).exists()
    path = health.report_path(directory)
    path.parent.mkdir(parents=True)
    for content in ("invalid", "[]", "null"):
        path.write_text(content)
        assert health.read_health(root) is None
    assert set(path.parent.iterdir()) == {path}


def test_automatic_crash_becomes_shared_machinery_failure(project, monkeypatch):
    root, directory = project

    def crash(*_a, **_kw):
        raise RuntimeError("probe crashed")

    monkeypatch.setattr(doctor, "run_doctor_result", crash)
    auto_doctor._execute(root, directory, "test")
    snapshot = health.read_health(root)
    assert snapshot.assessment.fail_count == 1
    assert snapshot.diagnostics.findings[0].check_id == "automatic.crash"
    assert "probe crashed" in health_presentation.brief_message(snapshot)


def test_interpreted_lint_design_findings_remain_healthy(project):
    root, directory = project
    reporter = doctor._Reporter.create()
    result = subprocess.CompletedProcess(["lint"], 1, "RESULT: WARN (58 warnings)", "")
    doctor._report_flow_check_result(
        None,
        "lint",
        result,
        target="lint_test",
        dry_run=False,
        label="lint smoke",
        report_dir=directory / "unused",
        verbose=False,
        _pass=reporter.pass_,
        _warn=reporter.warn_,
        _skip=reporter.skip_,
        _fail=reporter.fail_,
    )
    outcome = reporter.result(reporter.finish())
    doctor.record_manual_result(root, outcome)
    assert health.read_health(root).assessment.clean
    assert "design has lint findings" in outcome.findings[0].message


def test_dashboard_displays_stale_findings_without_consuming_alert(project, monkeypatch):
    root, directory = project
    _write(root, directory, _result(Severity.FAIL), age=timedelta(days=2))
    monkeypatch.setattr(
        upgrade_review,
        "read_status",
        lambda _: upgrade_review.ReviewStatus(upgrade_review.ReviewCondition.CURRENT, "1", "path"),
    )
    read = health.read_health
    monkeypatch.setattr(health, "read_health", lambda root: read(root, now=NOW))
    lines = dashboard.health_snapshot(root, directory)
    assert len(lines) == 1 and "1 FAIL" in lines[0]
    assert " · stale: automatic Doctor result expired" in lines[0]
    assert not (health.state_dir(directory) / "announcements.json").exists()

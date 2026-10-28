"""Doctor, MCP and the Dashboard agree on one stored observation, without running probes."""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from booley.audit.diagnostic_results import DiagnosticFinding, DiagnosticReport, Severity
from booley.harness import auto_doctor, doctor, upgrade_review
from booley.harness import booley as cli
from booley.harness.dashboard import model as dashboard
from booley.mcp import (
    server,
)
from booley.runtime import health, health_presentation
from booley.runtime.booley_dir import reset_cache
from booley.runtime.timefmt import rfc3339_from_datetime

NOW = datetime(2026, 10, 12, 12, tzinfo=UTC)


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.delenv("BOOLEY_DIR", raising=False)
    reset_cache()
    directory = tmp_path / ".booley"
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
        trigger="test",
        duration_s=0,
        diagnostics=result.diagnostics,
        transcript="probe text\n",
    )
    document["checked_at"] = rfc3339_from_datetime(NOW - age)
    health.report_path(directory).write_text(json.dumps(document))
    return document


def _isolate_mcp_and_dashboard(monkeypatch, root):
    """Leave only the health line of the MCP status card and Dashboard to this test."""
    monkeypatch.chdir(root)
    monkeypatch.setattr(server, "_interactive_session_id", lambda: "fixture")
    monkeypatch.setattr(server, "format_status_line", lambda: "status")
    monkeypatch.setenv("BOOLEY_MCP_MODE", "interactive")
    monkeypatch.setattr(
        upgrade_review,
        "read_status",
        lambda _: upgrade_review.ReviewStatus(upgrade_review.ReviewCondition.CURRENT, "1", "path"),
    )


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
    _isolate_mcp_and_dashboard(monkeypatch, root)
    assert "Health: " + message in server._format_status_card([])
    lines = dashboard.health_lines(root, directory)
    expected = "Doctor: OK" if clean else f"Doctor: 1 {severity.name}"
    assert tuple(line.text for line in lines) == (expected,)
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
    assert health.read_health(root, now=NOW).due == "Doctor inputs changed: booley.toml"


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


def test_snapshot_renders_the_observation_it_read_after_the_report_changes(project):
    root, directory = project
    _write(root, directory, _result(Severity.FAIL))
    snapshot = health.read_health(root, now=NOW)
    health.report_path(directory).write_text("broken")
    assert "1 FAIL" in health_presentation.brief_message(snapshot)


@pytest.mark.parametrize("issues_only", [False, True])
def test_changed_health_channels_preserve_clean_recovery_semantics(project, issues_only):
    root, directory = project
    issue = _result(Severity.FAIL)
    _write(root, directory, issue)
    assert health.consume_changed_snapshot(root, channel="one", issues_only=issues_only)
    assert health.consume_changed_snapshot(root, channel="one", issues_only=issues_only) is None
    assert health.consume_changed_snapshot(root, channel="other", issues_only=issues_only)
    _write(root, directory, _result(Severity.PASS))
    recovered = health.consume_changed_snapshot(root, channel="one", issues_only=issues_only)
    assert (recovered is None) is issues_only
    _write(root, directory, issue)
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
    assert auto_doctor.run_if_due(root, trigger="test") is not None
    snapshot = health.read_health(root)
    assert snapshot.assessment.fail_count == 1
    crash_finding = snapshot.diagnostics.findings[0]
    assert crash_finding.check_id == "automatic.crash"
    assert health.transcript_path(directory).name in crash_finding.fix
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


def test_stale_observation_reaches_doctor_mcp_and_dashboard(project, monkeypatch, capsys):
    root, directory = project
    result = _result(Severity.FAIL)
    _write(root, directory, result, age=timedelta(days=2))
    _isolate_mcp_and_dashboard(monkeypatch, root)
    read = health.read_health
    monkeypatch.setattr(health, "read_health", lambda root, **_: read(root, now=NOW))
    expired = "automatic Doctor result expired"

    snapshot = health.read_health(root)
    assert snapshot.diagnostics == result.diagnostics
    assert snapshot.due == expired
    message = health_presentation.brief_message(snapshot)
    assert "1 FAIL" in message

    # Doctor's session start schedules a recheck rather than announcing stale findings.
    cli._report_session_health(root)
    assert f"Automatic Doctor is running in the Sandbox ({expired})" in capsys.readouterr().err
    # MCP retains full findings; the Dashboard shows compact counts and freshness.
    assert "Health: " + message in server._format_status_card([])
    assert tuple(line.text for line in dashboard.health_lines(root, directory)) == (
        "Doctor: 1 FAIL · recheck due",
    )
    assert not (health.state_dir(directory) / "announcements.json").exists()


def test_dashboard_compact_line_preserves_verbose_brief(tmp_path, monkeypatch):
    transcript = tmp_path / "sentinel[transcript].log"
    mixed = health.HealthSnapshot(
        DiagnosticReport(
            (
                DiagnosticFinding(Severity.FAIL, "broken machinery"),
                DiagnosticFinding(Severity.WARN, "review needed", check_id="test.warning"),
            )
        ),
        "hash",
        transcript,
        None,
    )
    clean = replace(mixed, diagnostics=DiagnosticReport())
    monkeypatch.setattr(
        upgrade_review,
        "read_status",
        lambda _: upgrade_review.ReviewStatus(upgrade_review.ReviewCondition.CURRENT, "1", "path"),
    )
    for snapshot, brief, compact in (
        (
            mixed,
            f"Automatic Doctor found 1 FAIL, 1 WARN: broken machinery; review needed. Full report: {transcript}",
            "Doctor: 1 FAIL · 1 WARN",
        ),
        (clean, f"Automatic Doctor is clean. Full report: {transcript}", "Doctor: OK"),
        # A due result keeps the verbose brief unchanged; only the compact line gains the marker.
        (
            replace(mixed, due="expired"),
            f"Automatic Doctor found 1 FAIL, 1 WARN: broken machinery; review needed. Full report: {transcript}",
            "Doctor: 1 FAIL · 1 WARN · recheck due",
        ),
    ):
        monkeypatch.setattr(health, "read_health", lambda _, observed=snapshot: observed)
        assert health_presentation.brief_message(snapshot) == brief
        lines = dashboard.health_lines(tmp_path, tmp_path)
        texts = tuple(getattr(line, "text", line) for line in lines)
        assert texts == (compact,), (
            "Dashboard compact format must preserve the separate verbose brief"
        )

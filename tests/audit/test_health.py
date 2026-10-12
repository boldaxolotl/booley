"""Health interpretation and freshness policy over explicit inputs, without storage."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from booley.audit.diagnostic_results import DiagnosticFinding, DiagnosticReport, Severity
from booley.audit.health import finding_documents, freshness_reason, parse_diagnostics

NOW = datetime(2026, 10, 12, 12, tzinfo=UTC)
EXPIRED = "automatic Doctor result expired"


def _report(*severities: Severity, check_id: str = "test.health") -> DiagnosticReport:
    return DiagnosticReport(
        tuple(
            DiagnosticFinding(severity, "observed", "repair", check_id) for severity in severities
        )
    )


def _due(diagnostics: DiagnosticReport, *, age: timedelta, inputs_changed: bool = False):
    return freshness_reason(
        diagnostics, inputs_changed=inputs_changed, checked_at=NOW - age, now=NOW
    )


@pytest.mark.parametrize(
    "severities,days",
    [
        ((), 7),
        ((Severity.PASS,), 7),
        ((Severity.WAIVED,), 7),
        ((Severity.WARN,), 1),
        ((Severity.FAIL,), 1),
        ((Severity.PASS, Severity.FAIL), 1),
    ],
)
def test_observation_expires_exactly_at_its_health_dependent_age(severities, days):
    diagnostics = _report(*severities)
    assert _due(diagnostics, age=timedelta(days=days, seconds=-1)) is None
    assert _due(diagnostics, age=timedelta(days=days)) == EXPIRED


def test_upgrade_only_findings_keep_the_healthy_age():
    upgrade_only = _report(Severity.WARN, check_id="upgrade.review-pending")
    assert _due(upgrade_only, age=timedelta(days=7, seconds=-1)) is None
    assert _due(upgrade_only, age=timedelta(days=7)) == EXPIRED
    mixed = DiagnosticReport(upgrade_only.findings + _report(Severity.WARN).findings)
    assert _due(mixed, age=timedelta(days=1)) == EXPIRED


def test_input_drift_precedes_age_and_an_unreadable_timestamp():
    diagnostics = _report(Severity.PASS)
    assert (
        _due(diagnostics, age=timedelta(days=30), inputs_changed=True) == "Doctor inputs changed"
    )
    assert (
        freshness_reason(diagnostics, inputs_changed=True, checked_at=None, now=NOW)
        == "Doctor inputs changed"
    )
    assert (
        freshness_reason(diagnostics, inputs_changed=False, checked_at=None, now=NOW)
        == "automatic Doctor timestamp unreadable"
    )


def test_persisted_findings_parse_back_to_the_same_interpretation():
    diagnostics = _report(Severity.FAIL, Severity.WARN, Severity.WAIVED, Severity.PASS)
    document = {
        "counts": {"fail": 1, "warn": 1},
        "findings": finding_documents(diagnostics.findings),
    }
    parsed = parse_diagnostics(document)
    assert parsed == diagnostics
    assert (parsed.health.fail_count, parsed.health.warn_count) == (1, 1)
    assert [f.severity for f in parsed.active_findings] == [Severity.FAIL, Severity.WARN]


@pytest.mark.parametrize(
    "contradiction",
    [{"counts": {"fail": 0, "warn": 1}}, {"clean": True}, {"exit_code": 0}, {"schema": 2}],
)
def test_a_document_contradicting_its_findings_is_rejected(contradiction):
    document = {
        "counts": {"fail": 1, "warn": 1},
        "findings": finding_documents(_report(Severity.FAIL, Severity.WARN).findings),
        **contradiction,
    }
    with pytest.raises(ValueError):
        parse_diagnostics(document)

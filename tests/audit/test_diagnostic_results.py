"""Warning identities are required before findings reach a rendering caller."""

import pytest

from booley.audit.diagnostic_results import DiagnosticFinding, DiagnosticReport, Severity


@pytest.mark.parametrize("check_id", [None, "", "Text with spaces", "host."])
def test_warning_cannot_be_constructed_without_stable_identity(check_id):
    with pytest.raises(ValueError, match="stable check ID"):
        DiagnosticFinding(Severity.WARN, "Actionable", check_id=check_id)


def test_warning_preserves_subject_deduplication_and_remediation():
    finding = DiagnosticFinding(
        Severity.WARN, "Stale", "repair", "host.bootstrap-pending", "docker", "once"
    )
    assert (finding.check_id, finding.subject, finding.dedupe, finding.fix) == (
        "host.bootstrap-pending",
        "docker",
        "once",
        "repair",
    )


@pytest.mark.parametrize("severity", [Severity.WARN, Severity.WAIVED])
def test_active_and_waived_warnings_require_a_stable_identity(severity):
    with pytest.raises(ValueError, match="stable check ID"):
        DiagnosticFinding(severity, "Missing identity")


def test_only_fail_and_warn_findings_are_active():
    report = DiagnosticReport(
        (
            DiagnosticFinding(Severity.PASS, "fine"),
            DiagnosticFinding(Severity.FAIL, "broken"),
            DiagnosticFinding(Severity.WAIVED, "accepted", "reason", "test.waived"),
            DiagnosticFinding(Severity.WARN, "risky", "repair", "test.warn"),
            DiagnosticFinding(Severity.NOTE, "aside"),
            DiagnosticFinding(Severity.SKIP, "not run"),
        )
    )
    assert [f.message for f in report.active_findings] == ["broken", "risky"]
    assert (report.health.fail_count, report.health.warn_count) == (1, 1)
    assert not report.health.clean
    assert report.health.exit_code == 1

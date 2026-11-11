"""Warning identities are required before findings reach a rendering caller."""

import pytest

from booley.audit.diagnostic_results import (
    DiagnosticDetail,
    DiagnosticFinding,
    DiagnosticReport,
    Findings,
    Severity,
)


@pytest.mark.parametrize("check_id", [None, "", "Text with spaces", "host."])
def test_warning_cannot_be_constructed_without_stable_identity(check_id):
    with pytest.raises(ValueError, match="stable check ID"):
        DiagnosticFinding(Severity.WARN, "Actionable", check_id=check_id)


def test_waived_warning_requires_a_stable_identity():
    with pytest.raises(ValueError, match="stable check ID"):
        DiagnosticFinding(Severity.WAIVED, "Missing identity")


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


def test_detail_is_verbose_only_unless_marked() -> None:
    findings = Findings()
    findings.pass_("first")
    findings.detail("hidden unless verbose")
    findings.fail("second", "fix it")
    findings.detail("always shown", verbose_only=False)

    assert findings.report().details == (
        DiagnosticDetail(1, "hidden unless verbose", True),
        DiagnosticDetail(2, "always shown", False),
    )

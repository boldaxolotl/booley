"""Warning identities are required before findings reach a rendering caller."""

import pytest

from booley.audit.diagnostic_results import DiagnosticFinding, Severity


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

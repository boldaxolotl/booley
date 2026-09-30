from booley.evidence.review_dispositions import (
    collect_review_dispositions,
    review_report_required,
)


def test_collects_done_findings_and_clean_dispositions() -> None:
    finding = {
        "severity": "MINOR",
        "file": "rtl/dut.sv",
        "line": 4,
        "summary": "intentional behavior",
    }
    criteria = {
        "review_rtl_bugs_done": {"detail": {"issue_list": [finding]}},
        "review_rtl_security_clean": {
            "detail": {
                "pending": [],
                "resolved": [
                    {
                        **finding,
                        "status": "waived",
                        "justification": "required by the interface",
                    }
                ],
            }
        },
    }

    rows = collect_review_dispositions(criteria)

    assert [row["disposition"] for row in rows] == ["reported", "waived"]
    assert rows[1]["justification"] == "required by the interface"
    assert review_report_required(criteria) is True


def test_clean_completion_does_not_inherit_done_report_policy() -> None:
    clean = {"review_rtl_bugs_clean": {"detail": {"pending": [], "resolved": []}}}
    done = {"review_rtl_bugs_done": {"detail": {"issue_list": []}}}
    waived = {
        "review_rtl_bugs_clean": {
            "detail": {
                "resolved": [
                    {
                        "severity": "MINOR",
                        "summary": "accepted finding",
                        "status": "waived",
                        "justification": "accepted by the user",
                    }
                ]
            }
        }
    }

    assert review_report_required(clean) is False
    assert review_report_required(done) is True
    assert review_report_required(waived) is True


def test_legacy_impasse_is_visible_as_waiver() -> None:
    criteria = {
        "review_rtl_bugs_clean": {
            "detail": {
                "resolved": [
                    {
                        "severity": "MINOR",
                        "file": "rtl/dut.sv",
                        "line": 1,
                        "summary": "legacy finding",
                        "status": "impasse_deferred",
                    }
                ]
            }
        }
    }

    row = collect_review_dispositions(criteria)[0]

    assert row["disposition"] == "waived"
    assert "Legacy automatic impasse" in row["justification"]


def test_audit_is_not_a_disposition_or_report_requirement() -> None:
    from booley.evidence.review_dispositions import collect_review_audit

    row = {"reason": "source_scope", "ordinal": 1, "attempt_id": "one", "phase": "discovery"}
    criteria = {
        "review_rtl_bugs_clean": {
            "detail": {"filtered": [row, row], "audit_evidence": "/tmp/immutable.json"}
        }
    }
    assert collect_review_dispositions(criteria) == []
    assert review_report_required(criteria) is False
    audit = collect_review_audit(criteria)
    assert len(audit) == 2
    assert audit[0]["evidence"] == "/tmp/immutable.json"

from booley.dev_support.review_dispositions import (
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

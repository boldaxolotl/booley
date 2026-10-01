import pytest

from booley.evidence.review_dispositions import (
    collect_review_dispositions,
    review_report_required,
)
from booley.review.artifact import ReviewArtifactError, ReviewDispositionRow


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


@pytest.mark.parametrize(
    "finding, expected",
    [
        ({"disposition": "current", "status": "current"}, ("open", "current", "current")),
        ({"disposition": "advisory", "status": "advisory"}, ("reported", "advisory", "advisory")),
        ({"disposition": "deferred"}, ("reported", "deferred", "")),
        ({"disposition": "out_of_scope"}, ("reported", "out_of_scope", "")),
        (
            {"disposition": "advisory", "status": "superseded"},
            ("reported", "advisory", "superseded"),
        ),
        ({"status": "superseded"}, ("reported", "superseded", "superseded")),
        ({"disposition": "current", "status": "fixed"}, ("fixed", "current", "fixed")),
        (
            {"disposition": "current", "status": "waived", "justification": "accepted"},
            ("waived", "current", "waived"),
        ),
        ({"disposition": "current", "status": "excluded"}, ("excluded", "current", "excluded")),
        ({}, ("reported", "", "")),
    ],
)
def test_normalized_row_preserves_original_and_lifecycle(finding, expected):
    row = collect_review_dispositions(
        {
            "review_rtl_bugs_done": {
                "detail": {"issue_list": [{"severity": "MAJOR", "summary": "finding", **finding}]}
            }
        }
    )[0]
    assert tuple(row[key] for key in ("disposition", "reviewer_disposition", "status")) == expected
    parsed = ReviewDispositionRow.parse(row)
    assert parsed.reviewer_disposition == expected[1]
    assert ReviewDispositionRow.parse(parsed.to_dict()) == parsed


@pytest.mark.parametrize("original", ["bogus", "open", None, 42, [], {}])
def test_corrupt_original_is_not_hidden_by_valid_status(original):
    row = collect_review_dispositions(
        {
            "review_rtl_bugs_done": {
                "detail": {
                    "issue_list": [
                        {
                            "severity": "MAJOR",
                            "summary": "finding",
                            "status": "advisory",
                            "disposition": original,
                        }
                    ]
                }
            }
        }
    )[0]
    with pytest.raises(ReviewArtifactError, match="reviewer_disposition"):
        ReviewDispositionRow.parse(row)


def test_normalization_precedes_open_duplicate_precedence():
    finding = {"finding_id": "same", "severity": "MAJOR", "summary": "finding"}
    rows = collect_review_dispositions(
        {
            "review_rtl_bugs_done": {
                "detail": {
                    "issue_list": [
                        {**finding, "disposition": "current"},
                        {**finding, "disposition": "advisory"},
                        {**finding, "status": "fixed"},
                    ]
                }
            }
        }
    )
    assert len(rows) == 1
    assert rows[0]["disposition"] == "open"


def test_unknown_effective_disposition_fails_package_boundary():
    row = collect_review_dispositions(
        {
            "review_rtl_bugs_done": {
                "detail": {
                    "issue_list": [
                        {"severity": "MAJOR", "summary": "finding", "status": "corrupt"}
                    ]
                }
            }
        }
    )[0]
    with pytest.raises(ReviewArtifactError, match="disposition"):
        ReviewDispositionRow.parse(row)


@pytest.mark.parametrize(
    "status, mapped",
    [
        ("project_policy", "excluded"),
        ("out_of_diff_scope", "excluded"),
        ("impasse_deferred", "waived"),
        ("fixed", "fixed"),
        ("waived", "waived"),
        ("excluded", "excluded"),
    ],
)
def test_clean_resolution_preserves_original_status(status, mapped):
    row = collect_review_dispositions(
        {
            "review_rtl_bugs_clean": {
                "detail": {
                    "resolved": [
                        {
                            "severity": "MINOR",
                            "summary": "finding",
                            "status": status,
                            "disposition": "current",
                            "justification": "accepted",
                        }
                    ]
                }
            }
        }
    )[0]
    assert (row["disposition"], row["reviewer_disposition"], row["status"]) == (
        mapped,
        "current",
        status,
    )
    assert ReviewDispositionRow.parse(row).disposition == mapped


def test_clean_pending_preserves_original_current_and_still_present():
    row = collect_review_dispositions(
        {
            "review_rtl_bugs_clean": {
                "detail": {
                    "pending": [
                        {
                            "severity": "MINOR",
                            "summary": "finding",
                            "status": "still_present",
                            "disposition": "current",
                        }
                    ]
                }
            }
        }
    )[0]
    assert (row["disposition"], row["reviewer_disposition"], row["status"]) == (
        "open",
        "current",
        "still_present",
    )


def test_mapping_covers_persisted_vocabulary_and_only_package_values():
    from booley.evidence.review_vocabulary import (
        ALL_DISPOSITIONS,
        PERSISTED_REVIEWER_DISPOSITIONS,
        REVIEW_DISPOSITIONS,
        REVIEWER_TO_PACKAGE,
    )

    assert {"current", "advisory", "deferred", "out_of_scope"} == ALL_DISPOSITIONS
    assert ALL_DISPOSITIONS | {"superseded"} == PERSISTED_REVIEWER_DISPOSITIONS
    assert set(REVIEWER_TO_PACKAGE) == PERSISTED_REVIEWER_DISPOSITIONS
    assert set(REVIEWER_TO_PACKAGE.values()) <= REVIEW_DISPOSITIONS


@pytest.mark.parametrize(
    "mode, disposition, expected",
    [
        ("done", "current", True),
        ("done", "advisory", True),
        ("clean", "advisory", False),
        ("clean", "deferred", False),
        ("clean", "out_of_scope", False),
    ],
)
def test_mapping_does_not_expand_report_gate(mode, disposition, expected):
    collection = "issue_list" if mode == "done" else "observations"
    assert (
        review_report_required(
            {f"review_rtl_bugs_{mode}": {"detail": {collection: [{"disposition": disposition}]}}}
        )
        is expected
    )


@pytest.mark.parametrize("disposition", ["reported", "open", "fixed", "waived", "excluded"])
def test_legacy_package_only_rows_remain_valid(disposition):
    row = collect_review_dispositions(
        {
            "review_rtl_bugs_done": {
                "detail": {
                    "issue_list": [
                        {
                            "severity": "MINOR",
                            "summary": "legacy finding",
                            "disposition": disposition,
                            "justification": "accepted",
                        }
                    ]
                }
            }
        }
    )[0]
    assert row["reviewer_disposition"] == ""
    assert ReviewDispositionRow.parse(row).disposition == disposition

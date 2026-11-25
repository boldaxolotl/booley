import pytest

from booley.evidence.review_dispositions import (
    collect_review_dispositions,
    corrective_done_findings,
)
from booley.evidence.review_vocabulary import (
    PERSISTED_REVIEWER_DISPOSITIONS,
    REVIEW_DISPOSITIONS,
    REVIEWER_TO_PACKAGE,
)


def _lifecycle(criterion: str, collection: str, **finding: object) -> tuple[object, ...]:
    """Collect one finding and return its (disposition, reviewer_disposition, status)."""
    detail = {collection: [{"severity": "MAJOR", "summary": "finding", **finding}]}
    row = collect_review_dispositions({criterion: {"detail": detail}})[0]
    return row["disposition"], row["reviewer_disposition"], row["status"]


def test_collects_every_review_criterion_and_justifies_waivers() -> None:
    finding = {"severity": "MINOR", "file": "rtl/dut.sv", "line": 4, "summary": "intentional"}
    criteria = {
        "review_rtl_bugs_done": {"detail": {"issue_list": [finding]}},
        "review_rtl_security_clean": {
            "detail": {
                "resolved": [
                    {**finding, "status": "waived", "justification": "required by the interface"},
                    # A legacy automatic impasse persisted no justification of its own.
                    {**finding, "status": "impasse_deferred"},
                ]
            }
        },
    }

    rows = collect_review_dispositions(criteria)

    assert [row["disposition"] for row in rows] == ["reported", "waived", "waived"]
    assert rows[1]["justification"] == "required by the interface"
    assert "Legacy automatic impasse" in rows[2]["justification"]


def test_audit_is_not_a_disposition_or_report_requirement() -> None:
    row = {"reason": "source_scope", "ordinal": 1, "attempt_id": "one", "phase": "discovery"}
    detail = {"filtered": [row, row], "audit_evidence": "/tmp/immutable.json"}
    assert collect_review_dispositions({"review_rtl_bugs_clean": {"detail": detail}}) == []


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
        ({"disposition": "current", "status": "waived"}, ("waived", "current", "waived")),
        ({"disposition": "current", "status": "excluded"}, ("excluded", "current", "excluded")),
        ({}, ("reported", "", "")),
        # A corrupt reviewer disposition stays visible beside a valid status.
        *[
            ({"disposition": original, "status": "advisory"}, ("reported", original, "advisory"))
            for original in ("bogus", "open", None, 42, [], {})
        ],
        # An unknown status reaches the package boundary instead of being renamed.
        ({"status": "corrupt"}, ("corrupt", "", "corrupt")),
        # Legacy rows persisted the package disposition itself, not a reviewer disposition.
        *[
            ({"disposition": legacy}, (legacy, "", ""))
            for legacy in ("reported", "open", "fixed", "waived", "excluded")
        ],
    ],
)
def test_done_row_keeps_reviewer_disposition_and_status(finding, expected):
    assert _lifecycle("review_rtl_bugs_done", "issue_list", **finding) == expected


@pytest.mark.parametrize(
    "collection, status, original, expected",
    [
        ("resolved", "project_policy", "current", ("excluded", "current")),
        ("resolved", "out_of_diff_scope", "current", ("excluded", "current")),
        ("resolved", "impasse_deferred", "current", ("waived", "current")),
        ("resolved", "fixed", "current", ("fixed", "current")),
        ("resolved", "waived", "current", ("waived", "current")),
        ("resolved", "excluded", "current", ("excluded", "current")),
        ("pending", "still_present", "current", ("open", "current")),
        # Legacy rows persisted the package disposition beside the clean lifecycle status.
        ("pending", "still_present", "open", ("open", "")),
        ("resolved", "project_policy", "excluded", ("excluded", "")),
        ("resolved", "out_of_diff_scope", "excluded", ("excluded", "")),
        ("resolved", "impasse_deferred", "waived", ("waived", "")),
    ],
)
def test_clean_row_keeps_reviewer_disposition_and_status(collection, status, original, expected):
    assert _lifecycle(
        "review_rtl_bugs_clean",
        collection,
        status=status,
        disposition=original,
        justification="accepted",
    ) == (*expected, status)


def test_normalization_precedes_open_duplicate_precedence() -> None:
    finding = {"finding_id": "same", "severity": "MAJOR", "summary": "finding"}
    issues = [
        {**finding, "disposition": "current"},
        {**finding, "disposition": "advisory"},
        {**finding, "status": "fixed"},
    ]
    rows = collect_review_dispositions(
        {"review_rtl_bugs_done": {"detail": {"issue_list": issues}}}
    )
    assert [row["disposition"] for row in rows] == ["open"]


def test_every_persisted_reviewer_disposition_maps_to_a_package_disposition() -> None:
    assert set(REVIEWER_TO_PACKAGE) == PERSISTED_REVIEWER_DISPOSITIONS
    assert set(REVIEWER_TO_PACKAGE.values()) <= REVIEW_DISPOSITIONS


@pytest.mark.parametrize("raw_status", [None, 1, {}, []])
def test_corrective_done_rows_include_optional_legacy_and_all_severities(raw_status) -> None:
    rows = [
        {"finding_id": "current", "severity": "MINOR", "disposition": "current"},
        {"finding_id": "legacy", "severity": "UNKNOWN"},
        # A raw legacy status that is not text stays corrective instead of crashing.
        {"finding_id": "raw", "status": raw_status},
        {"finding_id": "history", "disposition": "current", "status": "superseded"},
        {"finding_id": "advice", "disposition": "advisory"},
        {"finding_id": "fixed", "status": "fixed"},
    ]
    criteria = {"review_rtl_bugs_done": {"required": "optional", "detail": {"issue_list": rows}}}

    corrective = corrective_done_findings(criteria)

    assert [row["finding_id"] for row in corrective] == ["current", "legacy", "raw"]
    assert corrective[2]["status"] == raw_status

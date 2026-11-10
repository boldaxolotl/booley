"""The review verdict model: key splitting, ordering, and the severity gate.

The model is imported inside each test so a missing or broken model fails the test
itself rather than collection.
"""

from __future__ import annotations

from typing import Any, get_args

import pytest

from tests.review_verdict_support import register_no_major


def test_split_review_key() -> None:
    import booley.evidence.review_verdicts as rv

    cases: dict[str, tuple[str, str] | None] = {
        "review_rtl_bugs": None,
        "review_rtl_bugs_clean": ("review_rtl_bugs", "clean"),
        "review_rtl_bugs_done": ("review_rtl_bugs", "done"),
        "lint_clean": None,
        "sim_pass_done": None,
        "review_clean": None,
        "review__clean": None,
    }
    assert {key: rv.split_review_key(key) for key in cases} == cases


def test_split_review_key_multi_word_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    import booley.evidence.review_verdicts as rv

    assert rv.split_review_key("review_rtl_bugs_no_major") is None  # not registered yet
    register_no_major(monkeypatch)
    assert rv.split_review_key("review_rtl_bugs_no_major") == ("review_rtl_bugs", "no_major")
    assert rv.split_review_key("review_rtl_bugs_clean") == ("review_rtl_bugs", "clean")
    assert rv.review_family("review_rtl_bugs_no_major") == "review_rtl_bugs"
    assert rv.review_keys("review_rtl_bugs") == (
        "review_rtl_bugs_clean",
        "review_rtl_bugs_no_major",
        "review_rtl_bugs_done",
    )


def test_review_verdicts_match_the_literal() -> None:
    import booley.evidence.review_verdicts as rv

    assert get_args(rv.ReviewVerdict) == tuple(rv.VERDICT_RULES)
    assert rv.REVIEW_VERDICTS == ("clean", "done")


def test_verdict_order_and_key_helpers() -> None:
    import booley.evidence.review_verdicts as rv

    assert rv.verdict_rank("clean") < rv.verdict_rank("done")
    with pytest.raises(ValueError, match="unknown review verdict 'bogus'"):
        rv.verdict_rank("bogus")
    with pytest.raises(ValueError, match="unknown review verdict"):
        rv.review_key("review_rtl_bugs", "bogus")
    assert rv.review_key("review_rtl_bugs", "clean") == "review_rtl_bugs_clean"
    assert rv.review_family("review_rtl_bugs") == "review_rtl_bugs"
    assert rv.is_corrective_review_key("review_rtl_bugs_clean")
    assert not rv.is_corrective_review_key("review_rtl_bugs_done")
    assert not rv.is_corrective_review_key("review_rtl_bugs")
    assert rv.is_advisory_review_key("review_rtl_bugs_done")
    assert not rv.is_advisory_review_key("review_rtl_bugs_clean")
    assert not rv.is_advisory_review_key("lint_clean")


@pytest.mark.parametrize(
    ("severity", "blocks"),
    [("MINOR", True), ("MAJOR", True), ("CRITICAL", True), ("major", True), (None, True)],
)
def test_clean_blocks_every_severity(severity: str | None, blocks: bool) -> None:
    import booley.evidence.review_verdicts as rv

    assert rv.finding_blocks(severity, "clean") is blocks


@pytest.mark.parametrize("severity", ["MINOR", "MAJOR", "CRITICAL", None, "weird"])
def test_done_never_blocks(severity: str | None) -> None:
    import booley.evidence.review_verdicts as rv

    assert rv.finding_blocks(severity, "done") is False


def test_finding_blocks_at_or_above_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    import booley.evidence.review_verdicts as rv

    register_no_major(monkeypatch)
    expected: dict[str | None, bool] = {
        "MINOR": False,
        "MAJOR": True,
        "CRITICAL": True,
        "major": True,
        None: True,
        "weird": True,
    }
    assert {severity: rv.finding_blocks(severity, "no_major") for severity in expected} == expected


def test_blocking_findings_view(monkeypatch: pytest.MonkeyPatch) -> None:
    import booley.evidence.review_verdicts as rv

    rows: list[dict[str, Any]] = [
        {"finding_id": "a", "severity": "MAJOR"},
        {"finding_id": "b", "severity": "MINOR"},
    ]
    register_no_major(monkeypatch)
    assert [row["finding_id"] for row in rv.blocking_findings(rows, "no_major")] == ["a"]
    assert rv.blocking_findings(rows, "clean") == rows
    assert rv.blocking_findings(rows, "done") == rows

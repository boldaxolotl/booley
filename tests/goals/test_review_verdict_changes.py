"""Goal-change slot matching and relaxation follow the review verdict model."""

from __future__ import annotations

import pytest

from booley.goals.change_policy import same_slot, validate_kind
from booley.goals.changes import ChangeKind
from booley.goals.model import GoalFamily, GoalSpec
from booley.goals.proposals import ProposalError
from tests.review_verdict_support import register_no_major


def _review(key: str) -> GoalSpec:
    return GoalSpec(key, GoalFamily.REVIEW, None, {}, ("ad-hoc",))


def test_same_slot_spans_review_verdicts(monkeypatch: pytest.MonkeyPatch) -> None:
    register_no_major(monkeypatch)
    clean = _review("review_rtl_bugs_clean")
    done = _review("review_rtl_bugs_done")
    no_major = _review("review_rtl_bugs_no_major")
    assert same_slot(clean, done)
    assert same_slot(clean, no_major)
    assert same_slot(no_major, done)
    assert not same_slot(no_major, _review("review_tb_quality_no_major"))
    assert not same_slot(clean, _review("review_rtl_bugs_extra_done"))


def test_review_relaxation_follows_verdict_order(monkeypatch: pytest.MonkeyPatch) -> None:
    register_no_major(monkeypatch)
    accepted = [
        ("review_rtl_bugs_clean", "review_rtl_bugs_done"),
        ("review_rtl_bugs_no_major", "review_rtl_bugs_done"),
    ]
    refused = [
        ("review_rtl_bugs_done", "review_rtl_bugs_clean"),
        ("review_rtl_bugs_no_major", "review_rtl_bugs_clean"),
        # A corrective target is refused: derivation judges relaxed reviews on completion only.
        ("review_rtl_bugs_clean", "review_rtl_bugs_no_major"),
        ("review_rtl_bugs_clean", "review_tb_quality_done"),
        ("review_rtl_bugs_clean", "review_rtl_bugs_clean"),
    ]
    for before, after in accepted:
        validate_kind(ChangeKind.RELAX, _review(before), _review(after))
    for before, after in refused:
        with pytest.raises(ProposalError, match="review relaxation is only clean -> done"):
            validate_kind(ChangeKind.RELAX, _review(before), _review(after))

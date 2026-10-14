"""Unbound freshness persists stale checks and fences submitted reports."""

from pathlib import Path

import pytest

from booley.criteria.state import CriterionEntry, DevelopmentState
from booley.evidence.review_receipt import (
    REVIEW_DETAIL_VERSION,
    ReviewInvocation,
    build_review_contract_detail,
)
from booley.specialists.verification_freshness import refresh_verification_freshness


@pytest.mark.parametrize("persist", [False, True])
@pytest.mark.parametrize("report", ["absent", "submitted", "locked", "unsubmitted"])
def test_source_drift_invalidates_review_and_unlocked_submitted_report(
    tmp_path: Path, persist: bool, report: str
) -> None:
    (tmp_path / ".booley").mkdir()
    source = tmp_path / "rtl/dut.sv"
    source.parent.mkdir()
    source.write_text("module dut; endmodule\n")
    state = DevelopmentState.load(tmp_path / "state.json")
    state.work_dir = str(tmp_path)
    state.criteria["review_rtl_bugs_done"] = CriterionEntry(
        met=True,
        mandatory=True,
        detail={
            "review_detail_version": REVIEW_DETAIL_VERSION,
            "contract": build_review_contract_detail(
                ReviewInvocation(
                    work_dir=tmp_path,
                    category="rtl",
                    focus="bugs",
                    scope=("rtl/dut.sv",),
                    mode="done",
                )
            ),
        },
    )
    if report != "absent":
        state.criteria["_report_submitted"] = CriterionEntry(
            met=report != "unsubmitted", locked=report == "locked"
        )
    state.save()
    state = DevelopmentState.load(tmp_path / "state.json")
    before = (tmp_path / "state.json").read_bytes()
    assert refresh_verification_freshness(state, work_dir=None) == []
    source.write_text("module dut; wire changed; endmodule\n")
    assert refresh_verification_freshness(state, work_dir=None, persist=persist) == [
        "review_rtl_bugs_done"
    ]
    entry = state.criteria["review_rtl_bugs_done"]
    assert entry.stale and not entry.met
    assert entry.detail["stale_review_dimensions"] == ["scope"]
    if report == "submitted":
        gate = state.criteria["_report_submitted"]
        assert gate.stale and not gate.met
        assert "stale_reason" in gate.detail
    elif report == "locked":
        assert state.criteria["_report_submitted"].met
    assert ((tmp_path / "state.json").read_bytes() != before) == persist


def test_freshness_without_work_directory_leaves_state_unchanged() -> None:
    state = DevelopmentState()
    assert refresh_verification_freshness(state, work_dir=None) == []

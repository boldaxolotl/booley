"""Reviewer receipt contract tests."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from booley.evidence.review_receipt import ReviewInvocation, build_review_contract_detail


def test_contract_tracks_scoped_file_content_and_inline_spec(tmp_path: Path) -> None:
    scoped = tmp_path / "rtl" / "top.sv"
    scoped.parent.mkdir()
    scoped.write_text("module top; endmodule\n", encoding="utf-8")
    invocation = ReviewInvocation(
        work_dir=tmp_path,
        category="tb",
        focus="quality",
        scope=("./rtl\\top.sv",),
        mode="done",
    )

    before = build_review_contract_detail(invocation)
    scoped.write_text("module top(input a); endmodule\n", encoding="utf-8")
    after = build_review_contract_detail(replace(invocation, spec_text="spec"))

    assert before["scope"] == ["rtl/top.sv"]
    assert before["scope_hashes"] != after["scope_hashes"]
    assert (after["spec_text"], after["spec_source"]) == ("spec", "")

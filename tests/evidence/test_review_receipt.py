"""Reviewer receipt contract tests."""

from __future__ import annotations

from pathlib import Path

from booley.evidence.review_receipt import ReviewInvocation, build_review_contract_detail


def test_review_contract_has_no_policy_digest(tmp_path: Path) -> None:
    contract = build_review_contract_detail(
        ReviewInvocation(work_dir=tmp_path, category="tb", focus="quality", scope=(), mode="done")
    )
    assert "tb_policy_digest" not in contract

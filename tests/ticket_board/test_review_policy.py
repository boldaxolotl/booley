"""Ticket Board review-policy identity tests."""

from __future__ import annotations

from pathlib import Path

from booley.ticket_board.review_policy import review_policy_digest


def _write_sim_policy(project_root: Path, pass_sentinel: str) -> None:
    config_dir = project_root / ".booley_project"
    config_dir.mkdir(exist_ok=True)
    (config_dir / "booley.toml").write_text(
        f'[flows.sim]\npass_sentinels = ["{pass_sentinel}"]\n',
        encoding="utf-8",
    )


def test_tb_review_policy_digest_is_stable_until_relevant_policy_changes(
    tmp_path: Path,
) -> None:
    _write_sim_policy(tmp_path, "PASS")
    original = review_policy_digest(tmp_path, "tb")
    rtl_digest = review_policy_digest(tmp_path, "rtl")

    assert review_policy_digest(tmp_path, "tb") == original

    _write_sim_policy(tmp_path, "SUCCESS")

    assert review_policy_digest(tmp_path, "tb") != original
    assert review_policy_digest(tmp_path, "rtl") == rtl_digest

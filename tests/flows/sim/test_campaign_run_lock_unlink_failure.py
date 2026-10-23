"""A failed run-lock unlink must not mask the run's error or keep the lock held (#1971)."""

from __future__ import annotations

from pathlib import Path

import pytest

from booley.flows.sim.campaign.run_directory import claimed_run_directory, expand_run_directory


def test_failed_lock_unlink_keeps_body_error_and_releases_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = expand_run_directory(
        "work/{test}",
        project_root=tmp_path,
        campaign_id="c",
        target_key="sim",
        work_item_key="t",
        attempt_key="a1",
    )
    identity = {"campaign_id": "c", "work_item_id": "t", "attempt_id": "a1"}
    real_unlink = Path.unlink

    def deny_lock_unlink(self: Path, missing_ok: bool = False) -> None:
        if self == run.lock_path:
            raise PermissionError("denied")
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", deny_lock_unlink)
    with (
        pytest.raises(RuntimeError, match="body failed"),
        claimed_run_directory(run, identity=identity),
    ):
        raise RuntimeError("body failed")
    # The lock was released: a second claim acquires it at once.
    with claimed_run_directory(run, identity=identity) as directory:
        assert directory.is_dir()

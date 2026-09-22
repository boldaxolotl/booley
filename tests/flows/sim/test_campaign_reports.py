"""Crash-safe Simulation Campaign compatibility projections."""

import json
from pathlib import Path

import pytest

from booley.flows.sim import campaign_reports


def test_compatibility_projection_stays_incomplete_until_acceptance_commits(
    tmp_path: Path,
) -> None:
    path = tmp_path / "simulation.json"
    report = {"target": "sim_uart", "complete": True, "tests_passed": 1}

    campaign_reports.write_compatibility_projection(path, report, acceptance_committed=False)
    assert json.loads(path.read_text())["complete"] is False

    campaign_reports.write_compatibility_projection(path, report, acceptance_committed=True)
    assert json.loads(path.read_text()) == report


def test_failed_projection_replacement_preserves_previous_complete_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "simulation.json"
    campaign_reports.write_compatibility_projection(
        path, {"target": "sim_uart"}, acceptance_committed=False
    )
    before = path.read_bytes()

    def fail_replace(self: Path, target: Path) -> Path:
        raise OSError(f"injected replacement failure for {target}")

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(OSError, match="injected replacement failure"):
        campaign_reports.write_compatibility_projection(
            path, {"target": "sim_uart"}, acceptance_committed=True
        )

    assert path.read_bytes() == before
    assert list(tmp_path.iterdir()) == [path]

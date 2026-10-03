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


def test_infrastructure_termination_is_reported_beyond_observation_preview() -> None:
    from booley.flows.sim.flow import _campaign_observation_preview

    observation = {
        "test": "first",
        "execution": "completed",
        "failure_class": None,
        "functional": "pass",
        "assertions": "clean",
        "assertion_count": 0,
        "detail": {},
    }
    aborted = dict(
        observation,
        test="last",
        execution="aborted",
        failure_class="infrastructure",
        functional="not_observed",
        assertions="not_observed",
        detail={"termination": "disk_budget", "reason": "disk guard"},
    )
    preview = _campaign_observation_preview([observation] * 32 + [aborted])
    assert preview["observations_truncated"] is True
    assert preview["termination_counts"] == {"disk_budget": 1}
    assert preview["observations"][0]["failure_class"] is None


def test_campaign_build_diagnostics_survive_preview_limit() -> None:
    from booley.flows.sim.flow import _campaign_observation_preview

    observation = {
        "test": "ok",
        "execution": "completed",
        "failure_class": None,
        "functional": "pass",
        "assertions": "clean",
        "assertion_count": 0,
        "detail": {},
    }
    stage = {
        "failure_kind": "infrastructure",
        "timed_out": True,
        "returncode": -9,
        "reason": "build exceeded timeout",
    }
    failed = dict(
        observation,
        failure_class="infrastructure",
        execution="aborted",
        detail={"termination": "build_infrastructure", "build_stage": stage},
    )
    preview = _campaign_observation_preview([observation] * 32 + [failed])
    assert preview["build_stage"] == [stage]

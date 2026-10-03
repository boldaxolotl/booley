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


@pytest.mark.parametrize("budget", [80, 300, 4000, 12000])
def test_cycle_card_global_limits_and_utf8_names(monkeypatch, budget):
    from types import SimpleNamespace

    from booley.flows.sim.flow import _campaign_report_lines

    monkeypatch.setenv("BOOLEY_MCP_MAX_STDOUT_BYTES", str(budget * 2))
    monkeypatch.setenv("BOOLEY_MCP_MAX_STDERR_BYTES", str(budget))
    outcomes = [
        SimpleNamespace(
            target={"selector": str(index)},
            aggregate_grade="fail",
            observations=[
                {"test": "é" * 256, "cycle_count": value, "detail": {}} for value in range(100)
            ],
        )
        for index in range(2)
    ]
    base = [
        SimpleNamespace(target=item.target, aggregate_grade=item.aggregate_grade, observations=[])
        for item in outcomes
    ]
    plain = "\n".join(_campaign_report_lines(base))
    rendered = "\n".join(_campaign_report_lines(outcomes))
    added = len(rendered.encode()) - len(plain.encode())
    assert added <= min(budget // 4, max(0, budget - len(plain.encode()) - 2))
    assert rendered.count("cycles=") <= 32
    assert rendered.splitlines()[0] == plain.splitlines()[0]
    if "cycles=" in rendered:
        assert "…: cycles=" in rendered
    if added:
        assert "cycle counts omitted" in rendered


def test_count_metadata_keeps_full_rows_outside_preview_and_recomputes():
    from types import SimpleNamespace

    from booley.flows.sim.flow import SimulateFlow, _campaign_observation_preview

    observations = [
        {
            "test": None if index == 0 else str(index),
            "cycle_count": index,
            "execution": "completed",
            "failure_class": None,
            "functional": "pass",
            "assertions": "clean",
            "assertion_count": 0,
            "detail": {},
        }
        for index in range(40)
    ]
    flow = SimulateFlow()
    flow.context._simulation_report_outcomes = (
        SimpleNamespace(target={"selector": "sim"}, observations=observations),
    )
    assert len(flow.persisted_cycle_counts()["sim"]) == 40
    assert flow.persisted_cycle_counts()["sim"][0]["test"] is None
    preview = _campaign_observation_preview(observations)
    assert len(preview["observations"]) == 32
    assert preview["observations"][31]["cycle_count"] == 31
    assert "cycle_counts" not in preview
    flow.context._simulation_report_outcomes = ()
    assert flow.persisted_cycle_counts() == {}


def test_large_persisted_count_mapping_survives_actual_readers(tmp_path, capsys):
    import time
    from types import SimpleNamespace

    from booley.flows.endpoint_reporting import write_report
    from booley.flows.sim.campaign_retention import _read_object
    from booley.flows.sim.flow import SimulateFlow
    from booley.mcp.server import _read_report_json, _structured_from_report
    from booley.runtime.endpoint_execution import EndpointOutcome

    flow = SimulateFlow()
    name = '\\"é' * 100
    observations = [{"test": name + str(index), "cycle_count": index} for index in range(2000)]
    flow.context._simulation_report_outcomes = tuple(
        SimpleNamespace(target={"selector": selector}, observations=observations)
        for selector in ("one", "two")
    )
    context = flow.context
    flow._args = SimpleNamespace(report_dir=tmp_path, slug="")
    context._start_time = time.monotonic()
    start = time.monotonic()
    with context.publication_resources:
        path = write_report(context, EndpointOutcome())
    raw = path.read_bytes()
    report = _read_report_json(path)
    assert _read_object(path) == report
    assert report == json.loads((tmp_path / "sim.json").read_bytes())
    assert sum(len(rows) for rows in report["cycle_counts"].values()) == 4000
    assert len(json.dumps(_structured_from_report(report)).encode()) < 65536
    assert "cycle_counts" not in report["detail"]
    print(
        f"4000 count rows: {len(raw)} persisted bytes; writer/readers {time.monotonic() - start:.4f}s"
    )
    observation = capsys.readouterr().out
    with capsys.disabled():
        print(observation.strip())

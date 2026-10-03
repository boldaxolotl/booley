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


@pytest.mark.parametrize("interrupted", [False, True])
def test_resume_separates_historical_hooks_from_real_new_firings(
    tmp_path, monkeypatch, interrupted
):
    from types import SimpleNamespace

    from booley.flows.sim.campaign.coordinator import (
        CampaignPolicy,
        ResumeCampaignRunRequest,
        SimulationCampaign,
    )
    from booley.flows.sim.campaign.model import create_simulation_campaign_plan
    from booley.flows.sim.campaign.planning import manifest_digest
    from booley.flows.sim.campaign.resume import ValidatedManifestNode, ValidatedResumeManifest
    from booley.flows.sim.campaign.store import CampaignStore
    from booley.flows.sim.flow import SimulateFlow
    from tests.flows.sim.test_campaign_phase3_integrity import _admission, _executor, _handle
    from tests.flows.sim.test_endpoint_campaign_lifecycle import _successful_pre_sim_campaign

    origin, original = _successful_pre_sim_campaign(tmp_path, monkeypatch, "immutable")
    store = CampaignStore(original.manifest_path.parent)
    if interrupted:
        (original.pre_sim_firings[0].path.parents[3] / "result.json").unlink()
    manifest = store.load_manifest()
    node = ValidatedManifestNode(original.manifest_path, manifest, manifest_digest(manifest))
    project = tmp_path / "project"
    validated = ValidatedResumeManifest(node, (), (_handle(project),))
    invocation = origin.parent / "2"
    invocation.mkdir()
    counters = {"compile": 0, "durable_reuse": 0, "launch": 0}
    resumed = SimulationCampaign(_executor(tmp_path / "build", counters)).run(
        ResumeCampaignRunRequest(
            validated,
            create_simulation_campaign_plan(manifest),
            project,
            origin.parent,
            CampaignPolicy(),
            invocation,
            _admission(),
        )
    )
    expected_runs = int(interrupted)
    assert counters == {"compile": expected_runs, "durable_reuse": 0, "launch": expected_runs}
    assert len(resumed.pre_sim_firings) == 1 + expected_runs
    flow = SimulateFlow()
    flow.context._reserved_invocation_dir = invocation
    flow._args = SimpleNamespace(target=["sim"], result_verbosity="brief")
    result = flow._campaign_endpoint_outcome([resumed])
    assert len(result.detail["pre_sim_lines"]) == expected_runs
    assert result.report_text.count("pre_run_commands") == expected_runs
    assert result.detail["pre_sim_current"] == expected_runs
    assert result.detail["pre_sim_total"] == 1 + expected_runs
    assert result.detail["pre_sim_historical"] == 1
    from booley.flows.sim.campaign import inspect_retained_campaign

    retained = inspect_retained_campaign(original.manifest_path)
    assert all(firing.path in retained.retention_files for firing in resumed.pre_sim_firings)


def test_hook_reference_resolves_from_numbered_report_and_requires_external_context(
    tmp_path, monkeypatch
):
    from booley.flows.sim.campaign import ArtifactReferenceError, resolve_report_artifact_reference
    from booley.flows.sim.flow import _campaign_pre_sim_details
    from tests.flows.sim.test_endpoint_campaign_lifecycle import _successful_pre_sim_campaign

    invocation, outcome = _successful_pre_sim_campaign(tmp_path, monkeypatch, "immutable")
    detail = _campaign_pre_sim_details([outcome], invocation)
    reference = detail["pre_sim_runs"][0]["reference"]
    owner = outcome.pre_sim_firings[0].document["attempt_id"]
    options = {
        "expected_kind": "pre_sim_commands",
        "expected_owner": owner,
        "maximum": 16 * 1024 * 1024,
    }
    resolved = resolve_report_artifact_reference(invocation / "report.json", reference, **options)
    assert resolved.path == outcome.pre_sim_firings[0].path
    destination = tmp_path / "external/reports/2"
    external = _campaign_pre_sim_details([outcome], destination)["pre_sim_runs"][0]["reference"]
    assert external["path_base"] == "external_origin_target"
    with pytest.raises(ArtifactReferenceError):
        resolve_report_artifact_reference(destination / "report.json", external, **options)
    resolved = resolve_report_artifact_reference(
        destination / "report.json",
        external,
        external_origin_target=outcome.manifest_path.parent.parent,
        **options,
    )
    assert resolved.path == outcome.pre_sim_firings[0].path

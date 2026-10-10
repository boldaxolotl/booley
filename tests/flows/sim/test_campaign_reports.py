"""Crash-safe Simulation Campaign compatibility projections."""

import json
from pathlib import Path

import pytest

from booley.flows.sim import campaign_reports
from tests.flows.sim.test_verilator_build_reuse_campaign import flow_project
from tests.runtime.test_sandbox_layout import project_alias

__all__ = ["flow_project", "project_alias"]


def test_project_alias_simulation_executes_campaign(flow_project, tmp_path, monkeypatch):
    from booley.flows.sim.flow import SimulateFlow
    from booley.flows.sim.request import SimRequest
    from booley.harness import doctor
    from tests.conftest import require_symlinks, symlink_or_skip

    require_symlinks(tmp_path)
    data = tmp_path / "external-project-data"
    data.mkdir()
    alias = tmp_path / "booley-project"
    symlink_or_skip(alias, data, target_is_directory=True)
    monkeypatch.setattr(doctor.dc, "PROJECT_DIR_TARGET", str(alias))
    from tests.runtime.test_sandbox_layout import configure_project_alias

    configure_project_alias(monkeypatch, alias, data)
    result = SimulateFlow().execute(
        SimRequest(
            target="sim",
            work_dir=flow_project,
            report_dir=alias / "reports",
            test=("first",),
            timeout_ms=30_000,
        )
    )
    if result.exit_code:
        assert "Invocation lock paths must not contain symlinks" in result.outcome.report_text
    assert result.exit_code == 0, result.outcome.report_text
    assert list((data / "reports").glob("sim/*/targets/sim/campaign/manifest.json"))


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
            manifest_path=Path("/manifest.json"),
            aggregate_grade="fail",
            observations=[
                {"test": "é" * 256, "cycle_count": value, "detail": {}} for value in range(100)
            ],
        )
        for index in range(2)
    ]
    base = [
        SimpleNamespace(
            target=item.target,
            manifest_path=item.manifest_path,
            aggregate_grade=item.aggregate_grade,
            observations=[],
        )
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


@pytest.mark.parametrize("reason", ["x\n", "x\r\nz", "x\u2028z"])
def test_cycle_card_preserves_complete_reason_strings(reason, monkeypatch):
    from types import SimpleNamespace

    from booley.flows.sim import flow

    resolved = []
    monkeypatch.setattr(
        flow,
        "_resolved_coverage_campaigns",
        lambda _outcomes: resolved.append(True) or {"sim": object()},
    )
    monkeypatch.setattr(flow, "_coverage_report_suffix", lambda _campaign: "coverage")
    monkeypatch.setattr(flow, "_waiver_block_hint_lines", lambda _campaign: ["coverage hint"])
    monkeypatch.setattr(flow, "source_gap_report_lines", lambda _campaign: [])
    monkeypatch.setattr(flow, "_coverage_pre_sim_lines", lambda *_args: [])

    outcome = SimpleNamespace(
        target={"selector": "sim"},
        manifest_path=Path("/manifest.json"),
        aggregate_grade="fail",
        coverage_reference=object(),
        observations=[
            {"test": "one", "cycle_count": 7, "detail": {"reason": reason}},
            {"test": "two", "cycle_count": None, "detail": {"reason": "y"}},
        ],
    )
    rendered = flow._campaign_report_lines([outcome])[0]
    assert f"\n  {reason}\n  y\n  one: cycles=7\ncoverage hint" in rendered
    assert resolved == [True]


def test_endpoint_cycle_budget_includes_pre_sim_hook_lines(monkeypatch):
    from types import SimpleNamespace

    from booley.flows.sim import flow

    manifest = Path("/manifest.json")
    pointer_bytes = len(f"  manifest: {manifest.resolve()}\n".encode())
    budget = 200 + pointer_bytes
    monkeypatch.setenv("BOOLEY_MCP_MAX_STDOUT_BYTES", str(budget))
    monkeypatch.setenv("BOOLEY_MCP_MAX_STDERR_BYTES", str(budget))
    hooks = ["pre-sim " + "h" * 130]
    calls = []
    monkeypatch.setattr(
        flow, "_campaign_pre_sim_report_lines", lambda *_args: calls.append(True) or hooks
    )
    for symbol in (
        "_resolved_coverage_campaigns",
        "_campaign_structured_details",
        "_campaign_pre_sim_details",
        "_campaign_build_infrastructure_detail",
    ):
        monkeypatch.setattr(flow, symbol, lambda *_args: {})
    monkeypatch.setattr(flow, "_coverage_compatibility_targets", lambda *_args: [])
    owner = SimpleNamespace(
        context=SimpleNamespace(_reserved_invocation_dir=None),
        _campaign_completed_display_label=lambda _outcomes: None,
        _report_destination=lambda: None,
    )
    outcome = SimpleNamespace(
        target={"selector": "sim"},
        aggregate_grade="pass",
        coverage_reference=None,
        manifest_path=manifest,
        observations=[{"test": "test" * 30, "cycle_count": None, "detail": {}}],
    )
    baseline = flow.SimulateFlow._campaign_endpoint_outcome(owner, [outcome]).report_text
    assert baseline.endswith(hooks[0]) and len(baseline.encode()) < budget
    outcome.observations = [{"test": "test" * 30, "cycle_count": 7, "detail": {}}]
    rendered = flow.SimulateFlow._campaign_endpoint_outcome(owner, [outcome]).report_text
    assert rendered.endswith(hooks[0]) and len(rendered.encode()) <= budget
    assert rendered == baseline
    assert calls == [True, True]


@pytest.mark.parametrize("failure", [None, "retention", "supersede"])
def test_authenticated_coverage_resume_keeps_report_counts(tmp_path, monkeypatch, failure):
    from booley.flows.sim.flow import SimulateFlow
    from booley.flows.sim.request import SimRequest
    from tests.flows.sim.test_coverage_flow import NativeExecution, _interrupt_coverage_publication

    reports, _request, _runs = _interrupt_coverage_publication(tmp_path, monkeypatch)
    manifest = reports / "sim/1/targets/sim_0/campaign/manifest.json"
    execution = NativeExecution()
    if failure:
        symbol = (
            "_checkpoint_coverage_campaign" if failure == "retention" else "supersede_progress"
        )
        monkeypatch.setattr(
            "booley.flows.sim.flow." + symbol,
            lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("after completed resume")),
        )
    resumed = tmp_path / "resumed"
    flow = SimulateFlow(coverage_execution=lambda *_args: execution)
    result = flow.execute(SimRequest(resume_from=manifest, work_dir=tmp_path, report_dir=resumed))
    report = json.loads((resumed / "sim/1/report.json").read_text())
    assert result.exit_code == (2 if failure else 0)
    assert execution.runs == []
    assert report["cycle_counts"]["sim_0"] == [
        {"test": "reset", "cycle_count": None},
        {"test": "wrap", "cycle_count": None},
    ]
    assert report == json.loads((resumed / "sim.json").read_text())
    progress = json.loads((resumed / "sim/1/progress.json").read_text())
    assert "cycle_counts" not in json.dumps(progress)
    if failure:
        assert flow.context._simulation_campaign_outcomes == ()
    else:
        campaign = report["detail"]["campaigns"]["sim_0"]
        assert all(item["cycle_count"] is None for item in campaign["observations"])
        assert campaign["artifacts"]["simulation"]["path_base"] == "external_origin_target"


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


def _resume_hook_campaign(tmp_path, monkeypatch, interrupted, *, external=False):
    from booley.flows.sim.campaign.coordinator import (
        CampaignPolicy,
        ResumeCampaignRunRequest,
        SimulationCampaign,
    )
    from booley.flows.sim.campaign.model import create_simulation_campaign_plan
    from booley.flows.sim.campaign.planning import manifest_digest
    from booley.flows.sim.campaign.resume import ValidatedManifestNode, ValidatedResumeManifest
    from booley.flows.sim.campaign.store import CampaignStore
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
    invocation = tmp_path / "external/reports/1" if external else origin.parent / "2"
    invocation.mkdir(parents=True)
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
    return original, resumed, counters, invocation


@pytest.mark.parametrize("interrupted", [False, True])
def test_resume_separates_historical_hooks_from_real_new_firings(
    tmp_path, monkeypatch, interrupted
):
    from types import SimpleNamespace

    from booley.flows.sim.flow import SimulateFlow

    original, resumed, counters, invocation = _resume_hook_campaign(
        tmp_path, monkeypatch, interrupted
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


# Windows CI observed 34 s; budget = 3x the slowest run, rounded up to 30 s.
@pytest.mark.timeout(120)
@pytest.mark.parametrize("interrupted", [False, True])
def test_external_resume_number_collision_reports_only_actual_new_firings(
    tmp_path, monkeypatch, interrupted
):
    from types import SimpleNamespace

    from booley.flows.sim.flow import SimulateFlow

    original, resumed, counters, invocation = _resume_hook_campaign(
        tmp_path, monkeypatch, interrupted, external=True
    )
    assert original.producer_invocation_directory.name == invocation.name == "1"
    assert original.producer_invocation_directory != invocation
    flow = SimulateFlow()
    flow.context._reserved_invocation_dir = invocation
    flow._args = SimpleNamespace(target=["sim"], result_verbosity="brief")
    result = flow._campaign_endpoint_outcome([resumed])
    assert counters["launch"] == int(interrupted)
    assert len(result.detail["pre_sim_lines"]) == int(interrupted)
    assert result.detail["pre_sim_current"] == int(interrupted)
    assert result.detail["pre_sim_historical"] == 1
    assert result.report_text.count("pre_run_commands") == int(interrupted)


def _production_resume_flow(tmp_path, monkeypatch, original, invocation, counters):
    from types import SimpleNamespace

    from booley.flows.sim.campaign.coordinator import CampaignPolicy
    from booley.flows.sim.campaign.model import create_simulation_campaign_plan
    from booley.flows.sim.campaign.planning import manifest_digest
    from booley.flows.sim.campaign.resume import ValidatedManifestNode, ValidatedResumeManifest
    from booley.flows.sim.campaign.store import CampaignStore
    from booley.flows.sim.flow import PreparedSimulationEndpoint, SimulateFlow
    from tests.flows.sim.test_campaign_phase3_integrity import _executor, _handle

    manifest = CampaignStore(original.manifest_path.parent).load_manifest()
    node = ValidatedManifestNode(original.manifest_path, manifest, manifest_digest(manifest))
    validated = ValidatedResumeManifest(node, (), (_handle(tmp_path / "project"),))
    invocation.mkdir(parents=True, exist_ok=True)
    flow = SimulateFlow()
    flow.context._reserved_invocation_dir = invocation
    flow._args = SimpleNamespace(
        target=["sim"],
        result_verbosity="brief",
        work_dir=tmp_path / "project",
        report_dir=invocation.parent,
        dry_run=False,
        mode=None,
    )
    monkeypatch.setattr(flow, "_campaign_policy", CampaignPolicy)
    monkeypatch.setattr(flow, "reserve_invocation_dir", lambda: invocation)
    # Destination admission/dependency publication is outside this typed execution test.
    monkeypatch.setattr(flow, "_prepare_campaign_resume_dependency", lambda *args: None)
    monkeypatch.setattr(
        flow,
        "_resume_campaign_plan",
        lambda *args, **kwargs: create_simulation_campaign_plan(manifest),
    )
    monkeypatch.setattr(
        flow, "_resume_campaign_executor", lambda *args: _executor(invocation / "build", counters)
    )
    return flow, PreparedSimulationEndpoint((), validated, ("sim",), {})


@pytest.mark.parametrize("interrupted", [False, True])
def test_production_resume_override_preserves_external_current_identity(
    tmp_path, monkeypatch, interrupted
):
    from tests.flows.sim.test_campaign_phase3_integrity import _admission

    original, _prior, _counters, _invocation = _resume_hook_campaign(
        tmp_path, monkeypatch, False, external=True
    )
    if interrupted:
        (original.pre_sim_firings[0].path.parents[3] / "result.json").unlink()
    counters = {"compile": 0, "durable_reuse": 0, "launch": 0}
    invocation = tmp_path / "third/reports/1"
    flow, prepared = _production_resume_flow(tmp_path, monkeypatch, original, invocation, counters)
    result = flow.run_prepared_simulation(prepared, _admission())
    assert counters["launch"] == int(interrupted)
    assert result.detail["pre_sim_current"] == int(interrupted)
    assert len(result.detail["pre_sim_lines"]) == int(interrupted)
    assert result.detail["pre_sim_historical"] == 1


@pytest.mark.parametrize("dry_run", [False, True])
def test_production_resume_corruption_uses_campaign_integrity_outcome(
    tmp_path, monkeypatch, dry_run
):
    from tests.flows.sim.test_campaign_phase3_integrity import _admission

    original, _prior, _counters, _invocation = _resume_hook_campaign(
        tmp_path, monkeypatch, False, external=True
    )
    counters = {"compile": 0, "durable_reuse": 0, "launch": 0}
    flow, prepared = _production_resume_flow(
        tmp_path, monkeypatch, original, tmp_path / "third/reports/1", counters
    )
    flow._args.dry_run = dry_run
    (original.pre_sim_firings[0].path.parents[3] / "result.json").write_text("{}")
    result = flow.run_prepared_simulation(prepared, _admission())
    assert result.exit_code == 2
    assert "Simulation Campaign integrity failure:" in result.report_text
    assert "Pre-Sim Commands evidence integrity failure" not in result.report_text
    assert counters["launch"] == 0


def _serialize_resume_waiter(monkeypatch, flow, waiting, completed):
    run = flow._run_validated_resume_campaign

    def wait_for_owner(*args, **kwargs):
        waiting.set()
        assert completed.wait(10)
        return run(*args, **kwargs)

    monkeypatch.setattr(flow, "_run_validated_resume_campaign", wait_for_owner)


def test_concurrent_production_resume_waiter_does_not_claim_owner_firing(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from tests.flows.sim.test_campaign_phase3_integrity import _admission

    original, _prior, _counters, _invocation = _resume_hook_campaign(
        tmp_path, monkeypatch, False, external=True
    )
    (original.pre_sim_firings[0].path.parents[3] / "result.json").unlink()
    first_counts = {"compile": 0, "durable_reuse": 0, "launch": 0}
    second_counts = dict(first_counts)
    owner, owner_prepared = _production_resume_flow(
        tmp_path, monkeypatch, original, tmp_path / "owner/reports/1", first_counts
    )
    waiter, waiter_prepared = _production_resume_flow(
        tmp_path, monkeypatch, original, tmp_path / "waiter/reports/1", second_counts
    )
    waiting, completed = Event(), Event()
    # Model serialization before store admission; the store itself rejects busy owners.
    _serialize_resume_waiter(monkeypatch, waiter, waiting, completed)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(waiter.run_prepared_simulation, waiter_prepared, _admission())
        assert waiting.wait(10)
        try:
            owner_result = owner.run_prepared_simulation(owner_prepared, _admission())
        finally:
            completed.set()
        waiter_result = future.result(timeout=10)
    assert first_counts["launch"] == 1
    assert second_counts["launch"] == 0
    assert owner_result.detail["pre_sim_current"] == 1
    assert waiter_result.detail["pre_sim_current"] == 0
    assert waiter_result.detail["pre_sim_lines"] == []
    assert waiter_result.detail["pre_sim_historical"] == 2


def test_campaign_scan_error_retains_independently_authenticated_hook_lines():
    from booley.flows.sim.flow import _pre_sim_integrity_failure
    from booley.runtime.endpoint_execution import EndpointOutcome

    line = "pre_run_commands rc=0 duration=0.01s"
    result = EndpointOutcome(
        exit_code=0,
        criterion_met=True,
        report_text=line,
        detail={"pre_sim_lines": [line], "pre_sim_current": 1},
    )
    result = _pre_sim_integrity_failure(
        result, ValueError("unrelated result corruption"), retain_authenticated=True
    )
    assert result.exit_code == 2
    assert result.criterion_met is False
    assert result.detail["pre_sim_lines"] == [line]
    assert result.report_text.count(line) == 1
    assert "Simulation Campaign integrity failure" in result.report_text


def test_production_postscan_corruption_preserves_authenticated_current_evidence(
    tmp_path, monkeypatch
):
    from tests.flows.sim.test_campaign_phase3_integrity import _admission

    original, _prior, _counters, _invocation = _resume_hook_campaign(
        tmp_path, monkeypatch, False, external=True
    )
    result_path = original.pre_sim_firings[0].path.parents[3] / "result.json"
    result_path.unlink()
    counters = {"compile": 0, "durable_reuse": 0, "launch": 0}
    flow, prepared = _production_resume_flow(
        tmp_path, monkeypatch, original, tmp_path / "third/reports/1", counters
    )
    project = flow._campaign_endpoint_outcome

    def corrupt_after_authenticated_projection(outcomes):
        result = project(outcomes)
        assert result.detail["pre_sim_current"] == 1
        result_path.write_text("{}")
        return result

    monkeypatch.setattr(flow, "_campaign_endpoint_outcome", corrupt_after_authenticated_projection)
    result = flow.run_prepared_simulation(prepared, _admission())
    assert counters["launch"] == 1
    assert result.exit_code == 2
    assert result.criterion_met is False
    assert result.detail["pre_sim_current"] == 1
    assert len(result.detail["pre_sim_lines"]) == 1
    assert result.report_text.count("pre_run_commands") == 1
    assert "Simulation Campaign integrity failure" in result.report_text


def _observed_hook_request(tmp_path, observer):
    from dataclasses import replace

    from booley.flows.sim.campaign.store import CampaignStore
    from booley.flows.sim.execution.pre_sim import run_pre_sim_commands
    from tests.flows.sim.test_campaign_phase3_integrity import _executor, _handle, _request
    from tests.flows.sim.test_endpoint_campaign_lifecycle import _pre_sim_plan

    commands = ("echo observed-hook",)
    plan, _disclosures = _pre_sim_plan("immutable", commands, lambda document: None)
    store = CampaignStore(tmp_path / "reports/1/targets/sim/campaign")
    store.publish_manifest(plan.manifest)
    request = _request(
        store, plan.manifest, plan.manifest.document["work_items"][0], tmp_path, invocation=1
    )
    request = replace(request, pre_sim_firing_published=observer)
    _executor(tmp_path / "build", {}).prepare_attempt(request)
    evidence = run_pre_sim_commands(
        _handle(tmp_path),
        test_names=("alpha",),
        build_root=tmp_path / "build",
        eda_tool="icarus",
        timeout_s=5,
        commands=commands,
        run_cwd=".",
    )
    return request, evidence


@pytest.mark.parametrize("stop", ["before", "after", "observer"])
def test_actual_publication_observer_checkpoint_and_exception_semantics(tmp_path, stop):
    from booley.flows.sim.campaign.pre_sim_evidence import publish_pre_sim_firing

    events = []

    def observer(key):
        events.append(key)
        if stop == "observer":
            raise RuntimeError("observer failure")

    def checkpoint(name):
        if name == f"{stop}:pre_sim_evidence":
            raise RuntimeError("checkpoint failure")

    request, evidence = _observed_hook_request(tmp_path, observer)
    with pytest.raises(RuntimeError, match="failure"):
        publish_pre_sim_firing(request, evidence, checkpoint=checkpoint)
    sidecars = tuple((request.attempt_directory / "pre-sim").glob("*.json"))
    assert len(events) == len(sidecars) == int(stop != "before")
    if sidecars:
        firings = request.store.read_pre_sim_firings()
        assert [firing.key for firing in firings] == events
        assert firings[0].document["stdout_tail"] == "observed-hook\n"


def _current_observed_hook_dag(tmp_path, monkeypatch, flow):
    from booley.flows.sim.campaign.model import create_simulation_campaign_plan
    from booley.flows.sim.campaign.store import CampaignStore
    from tests.flows.sim.test_campaign_dependency import _add_observed_cycles
    from tests.flows.sim.test_endpoint_campaign_lifecycle import _successful_pre_sim_campaign

    _add_observed_cycles(monkeypatch)
    invocation = tmp_path / "reports/1"
    baseline_root, candidate_root = tmp_path / "baseline", tmp_path / "candidate"
    baseline_root.mkdir()
    candidate_root.mkdir()
    flow.context._reserved_invocation_dir = invocation
    flow._current_published_pre_sim_keys = set()

    def baseline_role(document):
        document["campaign_id"] = "01234567-89ab-4def-8123-456789abcdef"
        document["target"].update(role="cycle_count_baseline", revision="baseline")

    _, baseline = _successful_pre_sim_campaign(
        baseline_root,
        monkeypatch,
        "immutable",
        transform=baseline_role,
        invocation_directory=invocation,
        observer=flow._record_pre_sim_firing,
    )
    manifest = CampaignStore(baseline.manifest_path.parent).load_manifest()
    prerequisites = flow._campaign_prerequisite_documents(
        create_simulation_campaign_plan(manifest), baseline.manifest_path.relative_to(invocation)
    )
    _, candidate = _successful_pre_sim_campaign(
        candidate_root,
        monkeypatch,
        "immutable",
        transform=lambda document: document.update(prerequisites=prerequisites),
        invocation_directory=invocation,
        observer=flow._record_pre_sim_firing,
    )
    return candidate


def test_retained_current_baseline_uses_actual_flow_publication_authority(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from booley.flows.sim.flow import SimulateFlow

    flow = SimulateFlow()
    flow._args = SimpleNamespace(target=["sim"], result_verbosity="brief")
    candidate = _current_observed_hook_dag(tmp_path, monkeypatch, flow)
    result = flow._campaign_endpoint_outcome([candidate])
    assert result.detail["pre_sim_current"] == 1
    firing = next(f for f in candidate.pre_sim_firings if f.document["role"] == "candidate")
    (firing.path.parents[3] / "result.json").write_text("{}")
    result = flow._attach_published_pre_sim(result)
    assert result.exit_code == 2
    assert result.detail["pre_sim_current"] == 2
    assert result.detail["pre_sim_historical"] == 0
    assert set(result.detail["pre_sim_roles"]) == {"candidate", "cycle_count_baseline"}
    assert len(result.detail["pre_sim_lines"]) == 2
    assert "Candidate:" in result.report_text and "Cycle Count baseline:" in result.report_text
    assert all(record["terminal"] is False for record in result.detail["pre_sim_runs"])


def _published_hook_budget_boundary(flow, candidate, roomy):
    from booley.flows.sim import flow as module
    from booley.runtime.endpoint_execution import EndpointOutcome

    baseline = EndpointOutcome(
        report_text="\n".join(module._campaign_base_report_lines([candidate], {}))
    )
    baseline = flow._attach_published_pre_sim(baseline)
    line = module._cycle_display_line(candidate.observations[0], 10000)
    allowance = len(line.encode()) + 1 + len(b"  1 cycle counts omitted") + 1
    budget = len(baseline.report_text.encode()) + 2 + (allowance if roomy else 0)
    assert len(baseline.report_text.encode()) + 2 <= budget
    assert not roomy or budget // 4 >= allowance
    if roomy:
        candidate_hooks = module._campaign_pre_sim_report_lines(
            [candidate], flow.context._reserved_invocation_dir
        )
        assert allowance < len("\n".join(candidate_hooks).encode()) + 1
    return {"baseline": baseline, "budget": budget, "candidate": candidate}


def _published_hook_budget_run(tmp_path, monkeypatch, *, roomy=False, corrupt=False):
    from booley.flows.sim import campaign
    from booley.flows.sim import flow as module
    from booley.flows.sim.request import SimRequest

    flow = module.SimulateFlow()
    flow._args = SimRequest(target="sim", coverage=False, mode=None, result_verbosity="compact")
    evidence = {}

    def run(*, prepared, admission):
        candidate = _current_observed_hook_dag(tmp_path, monkeypatch, flow)
        evidence.update(_published_hook_budget_boundary(flow, candidate, roomy))
        monkeypatch.setenv("BOOLEY_MCP_MAX_STDOUT_BYTES", str(evidence["budget"]))
        monkeypatch.setenv("BOOLEY_MCP_MAX_STDERR_BYTES", str(evidence["budget"]))
        reads = []
        reader = campaign.read_invocation_pre_sim_firings

        def read(invocation):
            reads.append(invocation)
            return reader(invocation)

        monkeypatch.setattr(campaign, "read_invocation_pre_sim_firings", read)
        if corrupt:
            firing = next(
                f for f in candidate.pre_sim_firings if f.document["role"] == "candidate"
            )
            (firing.path.parents[3] / "result.json").write_text("{}")
            flow.context._pending_criteria_set = ("before-final-integrity",)
        result = flow._campaign_endpoint_outcome([candidate])
        assert reads == []
        evidence["reads"] = reads
        evidence["endpoint"] = result
        evidence["endpoint_text"] = result.report_text
        return result

    monkeypatch.setattr(flow, "_run_selected_mode", run)
    prepared = module.PreparedSimulationEndpoint((), None, ("sim",), {})
    result = flow.run_prepared_simulation(prepared, None)
    return flow, result, evidence


@pytest.mark.parametrize("roomy", [False, True])
def test_final_published_baseline_hooks_bound_cycle_text(tmp_path, monkeypatch, roomy):
    flow, result, evidence = _published_hook_budget_run(tmp_path, monkeypatch, roomy=roomy)
    assert result.detail["pre_sim_current"] == 2
    assert len(flow._current_published_pre_sim_keys) == 2
    assert len(result.detail["pre_sim_lines"]) == 2
    assert evidence["reads"] == [flow.context._reserved_invocation_dir]
    assert result.report_text.count("pre_run_commands") == 2
    assert result.report_text.count("Candidate:") == 1
    assert result.report_text.count("Cycle Count baseline:") == 1
    size = len(result.report_text.encode())
    print(
        f"published baseline={len(evidence['baseline'].report_text.encode())} final={size} budget={evidence['budget']}"
    )
    assert size + 2 <= evidence["budget"]
    assert result.report_text.count("cycles=17") == int(roomy)
    if not roomy:
        assert result.report_text == evidence["baseline"].report_text


def test_missing_published_hook_key_suppresses_optional_budget(tmp_path, monkeypatch):
    from booley.flows.sim import flow as module
    from booley.flows.sim.request import SimRequest

    flow = module.SimulateFlow()
    candidate = _current_observed_hook_dag(tmp_path, monkeypatch, flow)
    keys = frozenset((*flow._current_published_pre_sim_keys, ("missing", "key", "id", 1)))
    before = candidate.pre_sim_firings
    budget = module._campaign_published_hook_lines(
        [candidate], flow.context._reserved_invocation_dir, keys
    )
    blocks = module._campaign_base_report_lines([candidate], {})
    assert budget is None
    assert module._campaign_report_lines([candidate], {}, budget, blocks=blocks) is blocks
    assert not any("cycles=" in block or "integrity" in block for block in blocks)
    assert candidate.pre_sim_firings is before
    flow._args = SimRequest(target="sim")
    flow._current_published_pre_sim_keys = set(keys)
    result = flow._campaign_endpoint_outcome([candidate])
    assert "cycles=" not in result.report_text and "integrity" not in result.report_text
    assert flow.context._simulation_campaign_outcomes == (candidate,)
    assert flow.context._simulation_report_outcomes == (candidate,)


def test_initialized_empty_hook_authority_keeps_authenticated_counts(tmp_path, monkeypatch):
    from booley.flows.sim import campaign
    from booley.flows.sim.flow import SimulateFlow
    from booley.flows.sim.request import SimRequest
    from tests.flows.sim.test_endpoint_campaign_lifecycle import _public_cycle_campaign

    outcome = _public_cycle_campaign(tmp_path, monkeypatch)
    flow = SimulateFlow()
    flow._args = SimRequest(target="sim")
    flow.context._reserved_invocation_dir = outcome.producer_invocation_directory
    flow._current_published_pre_sim_keys = set()

    def unexpected_read(*_args):
        raise AssertionError("endpoint must not scan hook storage")

    monkeypatch.setattr(campaign, "read_invocation_pre_sim_firings", unexpected_read)
    result = flow._campaign_endpoint_outcome([outcome])
    assert "smoke: cycles=1234" in result.report_text
    assert result.detail["pre_sim_current"] == 0


def test_authenticated_no_counts_skips_published_hook_probe(tmp_path, monkeypatch):
    from dataclasses import replace

    from booley.flows.sim import flow as module
    from booley.flows.sim.request import SimRequest
    from tests.flows.sim import test_campaign_phase3 as fixtures
    from tests.flows.sim.test_endpoint_campaign_lifecycle import _public_cycle_campaign

    original = fixtures._shared_outcome

    def nullable(*args):
        outcome = original(*args)
        return replace(outcome, tests=tuple(replace(test, cycles=None) for test in outcome.tests))

    monkeypatch.setattr(fixtures, "_shared_outcome", nullable)
    outcome = _public_cycle_campaign(tmp_path, monkeypatch)
    flow = module.SimulateFlow()
    flow._args = SimRequest(target="sim")
    flow.context._reserved_invocation_dir = outcome.producer_invocation_directory
    flow._current_published_pre_sim_keys = set()
    formatter = module._campaign_pre_sim_report_lines
    calls = []

    def format_hooks(*args):
        calls.append(True)
        return formatter(*args)

    def unexpected_probe(*_args):
        raise AssertionError("no-count path must not probe")

    monkeypatch.setattr(module, "_campaign_published_hook_lines", unexpected_probe)
    monkeypatch.setattr(module, "_campaign_pre_sim_report_lines", format_hooks)
    result = flow._campaign_endpoint_outcome([outcome])
    assert all(item["cycle_count"] is None for item in outcome.observations)
    assert result.report_text == "\n".join(module._campaign_base_report_lines([outcome], {}))
    assert calls == [True]


def test_corruption_before_endpoint_keeps_final_integrity_policy(tmp_path, monkeypatch):
    flow, result, evidence = _published_hook_budget_run(
        tmp_path, monkeypatch, roomy=True, corrupt=True
    )
    assert "cycles=17" in evidence["endpoint_text"]
    assert "integrity" not in evidence["endpoint_text"]
    assert result.exit_code == 2 and result.criterion_met is False
    assert "Simulation Campaign integrity failure:" in result.report_text
    assert flow.context._simulation_campaign_outcomes == ()
    assert flow.context._pending_criteria_set == ()
    assert result.detail["pre_sim_current"] == 2
    assert all(record["terminal"] is False for record in result.detail["pre_sim_runs"])


@pytest.mark.parametrize("fault", ["base", "structured"])
def test_original_renderer_and_detail_errors_precede_hook_probe(tmp_path, monkeypatch, fault):
    from types import SimpleNamespace

    from booley.flows.sim import flow as module

    events = []

    def base(*_args, report_path=None):
        events.append("base")
        if fault == "base":
            raise KeyError("original base")
        return ["baseline"]

    def structured(*_args):
        events.append("structured")
        raise ValueError("original structured")

    def probe(*_args):
        raise AssertionError("late probe must not outrank original errors")

    monkeypatch.setattr(module, "_campaign_base_report_lines", base)
    monkeypatch.setattr(module, "_campaign_structured_details", structured)
    monkeypatch.setattr(module, "_campaign_published_hook_lines", probe)
    owner = SimpleNamespace(
        context=SimpleNamespace(_reserved_invocation_dir=tmp_path),
        _current_published_pre_sim_keys=set(),
        _report_destination=lambda: None,
    )
    outcome = SimpleNamespace(
        target={"selector": "sim"},
        aggregate_grade="pass",
        coverage_reference=None,
        observations=[{"test": "alpha", "cycle_count": 17, "detail": {}}],
    )
    expected = KeyError if fault == "base" else ValueError
    with pytest.raises(expected, match="original " + fault):
        module.SimulateFlow._campaign_endpoint_outcome(owner, [outcome])
    assert events == (["base"] if fault == "base" else ["base", "structured"])


@pytest.mark.parametrize("interrupted", [False, True])
def test_resume_count_budget_uses_only_actual_current_hook_keys(
    tmp_path, monkeypatch, interrupted
):
    from booley.flows.sim import flow as module
    from tests.flows.sim.test_campaign_dependency import _add_observed_cycles
    from tests.flows.sim.test_campaign_phase3_integrity import _admission

    _add_observed_cycles(monkeypatch)
    original, _prior, _counters, _invocation = _resume_hook_campaign(
        tmp_path, monkeypatch, False, external=True
    )
    if interrupted:
        (original.pre_sim_firings[0].path.parents[3] / "result.json").unlink()
    counters = {"compile": 0, "durable_reuse": 0, "launch": 0}
    flow, prepared = _production_resume_flow(
        tmp_path, monkeypatch, original, tmp_path / "third/reports/1", counters
    )
    probe = module._campaign_published_hook_lines
    calls = []

    def inspect(outcomes, invocation, published_keys):
        assert outcomes[0].manifest_path == original.manifest_path
        lines = probe(outcomes, invocation, published_keys)
        calls.append((published_keys, lines))
        return lines

    monkeypatch.setattr(module, "_campaign_published_hook_lines", inspect)
    result = flow.run_prepared_simulation(prepared, _admission())
    assert counters["launch"] == int(interrupted)
    assert result.report_text.count("cycles=17") == 1
    assert result.detail["pre_sim_current"] == int(interrupted)
    assert result.detail["pre_sim_historical"] == 1
    assert len(calls) == 1
    keys, lines = calls[0]
    assert keys == frozenset(flow._current_published_pre_sim_keys)
    assert len(keys) == len(lines) == int(interrupted)
    assert result.report_text.count("pre_run_commands") == int(interrupted)


def test_prior_firing_reauthentication_rejects_byte_identical_symlink_attempt(
    tmp_path, monkeypatch
):
    from booley.flows.sim.campaign import SimulationCampaign, SimulationCampaignIntegrityError
    from tests.conftest import require_symlinks, symlink_or_skip
    from tests.flows.sim.test_endpoint_campaign_lifecycle import _successful_pre_sim_campaign

    require_symlinks(tmp_path)
    _invocation, campaign = _successful_pre_sim_campaign(tmp_path, monkeypatch, "immutable")
    firing = campaign.pre_sim_firings[0]
    directory = firing.path.parent.parent
    copy = tmp_path / "copied-attempt"
    sidecar_bytes = firing.path.read_bytes()
    snapshots = list(directory.rglob("simv"))
    assert len(snapshots) == 1, "successful campaign must retain one real executable snapshot"
    snapshot = snapshots[0]
    snapshot_relative = snapshot.relative_to(directory)
    snapshot_bytes, snapshot_mode = snapshot.read_bytes(), snapshot.stat().st_mode
    directory.rename(copy)
    assert (copy / snapshot_relative).read_bytes() == snapshot_bytes
    assert (copy / snapshot_relative).stat().st_mode == snapshot_mode
    assert not snapshot_mode & 0o222
    assert (copy / "pre-sim/0001.json").read_bytes() == sidecar_bytes
    symlink_or_skip(directory, copy, target_is_directory=True)
    assert firing.path.read_bytes() == sidecar_bytes
    with pytest.raises(SimulationCampaignIntegrityError):
        SimulationCampaign.reauthenticate_pre_sim_firings((firing,))


@pytest.mark.parametrize("defect", ["root", "descendant", "lock", "target_link"])
def test_alias_keeps_user_report_links_strict(flow_project, project_alias, defect):
    from booley.flows.sim.flow import SimulateFlow
    from booley.flows.sim.request import SimRequest

    alias, data = project_alias
    reports = data / "reports"
    reports.mkdir()
    if defect == "root":
        user = data / "user"
        user.symlink_to(reports, target_is_directory=True)
        report_dir = user
    elif defect == "target_link":
        (data / "linked").symlink_to(data, target_is_directory=True)
        report_dir = alias / "linked/reports"
    else:
        (reports / "sim").mkdir()
        if defect == "descendant":
            (reports / "sim").rmdir()
            (reports / "sim").symlink_to(data, target_is_directory=True)
        else:
            (reports / "sim/.invocation-1.lock").symlink_to(data / "outside")
        report_dir = alias / "reports"
    result = SimulateFlow().execute(
        SimRequest(
            target="sim",
            work_dir=flow_project,
            report_dir=report_dir,
            test=("first",),
            timeout_ms=30_000,
        )
    )
    assert result.exit_code == 2
    assert "Invocation lock paths must not contain symlinks" in result.outcome.report_text
    assert not list(reports.rglob("manifest.json"))


@pytest.mark.parametrize("selection", ["default", "dry_run", "elab"])
def test_alias_normalization_precedes_simulation_preparation(
    flow_project, project_alias, monkeypatch, selection
):
    from booley.flows.sim.flow import SimulateFlow
    from booley.flows.sim.mode import SimulationMode
    from booley.flows.sim.request import SimRequest

    alias, data = project_alias
    monkeypatch.setattr(
        "booley.runtime.project_dir.resolve_checkout_project_dir", lambda _root: alias
    )
    flow = SimulateFlow()
    result = flow.execute(
        SimRequest(
            target="sim",
            work_dir=flow_project,
            report_dir=None if selection == "default" else alias / "reports",
            dry_run=selection == "dry_run",
            mode=SimulationMode.ELAB_ONLY if selection == "elab" else None,
            test=None if selection == "elab" else ("first",),
            timeout_ms=30_000,
        )
    )
    assert result.exit_code == 0, result.outcome.report_text
    assert flow.context.args.report_dir == data / (
        "flow-reports" if selection == "default" else "reports"
    )

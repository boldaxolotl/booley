import json
from pathlib import Path

import pytest

from booley.flows.sim.coverage_reference import (
    REFERENCE_SCHEMA,
    resolve_coverage_campaign_reference,
)
from booley.flows.sim.flow import SimulateFlow
from booley.flows.sim.request import SimRequest
from booley.mcp.flow_adapter import flow_schema
from tests.flows.sim.test_coverage_invocation import project
from tests.flows.sim.test_coverage_transaction import NativeExecution


def _assert_enclosing_result_is_authenticated(campaign_path, resolved) -> None:
    from booley.flows.sim.campaign.codec import canonical_json_bytes
    from booley.flows.sim.campaign.store import CampaignStore
    from booley.flows.sim.coverage_analysis_input import read_coverage_campaign

    store = CampaignStore(campaign_path.parent / "campaign")
    work_item_id = resolved.reference.document["simulation_work_item_id"]
    simulation_result = store.work_item_directory(work_item_id) / "result.json"
    result_raw = simulation_result.read_bytes()
    hostile = json.loads(result_raw)
    hostile["evidence"][0]["sha256"] = "sha256:" + "0" * 64
    simulation_result.chmod(0o600)
    simulation_result.write_bytes(canonical_json_bytes(hostile))
    with pytest.raises(ValueError, match="enclosing Simulation Campaign"):
        read_coverage_campaign(campaign_path)
    simulation_result.write_bytes(result_raw)
    simulation_result.chmod(0o400)


def test_flow_produces_numbered_target_reports_with_public_coverage_input(tmp_path, monkeypatch):
    from booley.flows.sim.campaign_retention import prune_native_payload
    from booley.flows.sim.coverage_analysis_input import read_coverage_campaign

    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    project(tmp_path)
    data = tmp_path / ".booley_project"
    data.mkdir()
    (data / "tests.toml").write_text('[sim_0]\ntests = ["reset", "wrap"]\n')
    flow = SimulateFlow(coverage_execution=lambda handle, options: NativeExecution())
    result = flow.execute(
        SimRequest(
            target="sim_0", work_dir=tmp_path, coverage=True, report_dir=tmp_path / "reports"
        )
    )
    assert result.exit_code == 0
    target = result.outcome.detail["targets"]["sim_0"]
    campaign_path = tmp_path / target["coverage_campaign"]
    assert campaign_path == tmp_path / "reports/sim/1/targets/sim_0/coverage.json"
    assert json.loads(campaign_path.read_text())["$schema"] == REFERENCE_SCHEMA
    resolved = resolve_coverage_campaign_reference(campaign_path)
    assert resolved.loaded.campaign.evaluation["status"] == "not_requested"
    campaign = result.outcome.detail["campaigns"]["sim_0"]
    assert campaign["manifest"].endswith("/targets/sim_0/campaign/manifest.json")
    assert campaign["summary"].endswith("/targets/sim_0/campaign/summary.json")
    assert campaign["simulation"].endswith("/targets/sim_0/simulation.json")
    assert campaign["coverage"].endswith("/targets/sim_0/coverage.json")
    assert campaign["observation_counts"] == {
        "execution": {"completed": 2},
        "functional": {"pass": 2},
        "assertions": {"clean": 2},
    }
    reference = campaign_path.read_bytes()
    assert read_coverage_campaign(campaign_path).campaign.campaign_id
    _assert_enclosing_result_is_authenticated(campaign_path, resolved)
    prune_native_payload(tmp_path / "reports", 1, "sim_0")
    assert campaign_path.read_bytes() == reference
    assert not (resolved.campaign_path.parent / "native").exists()
    assert (resolved.campaign_path.parent / "availability.json").is_file()
    assert read_coverage_campaign(campaign_path).campaign.campaign_id
    projection_path = campaign_path.with_name("simulation.json")
    projection_raw = projection_path.read_bytes()
    projection = json.loads(projection_raw)
    projection["campaign_manifest"] = str(campaign_path.parent / "other/manifest.json")
    projection_path.write_text(json.dumps(projection))
    with pytest.raises(ValueError, match="projection pointers disagree"):
        read_coverage_campaign(campaign_path)
    projection_path.write_bytes(projection_raw)
    assert not (tmp_path / "reports/sim_sim_0.json").exists()
    assert flow_schema(flow)["properties"]["coverage"]["type"] == "boolean"


def test_multi_target_collector_error_preserves_completed_and_later_targets(tmp_path, monkeypatch):
    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    project(tmp_path, ("verilator", "verilator", "verilator"))
    data = tmp_path / ".booley_project"
    data.mkdir()
    (data / "tests.toml").write_text("".join(f'[sim_{i}]\ntests = ["reset"]\n' for i in range(3)))
    flow = SimulateFlow(
        coverage_execution=lambda handle, options: NativeExecution(missing=handle.name == "sim_1")
    )
    result = flow.execute(
        SimRequest(
            target="sim_2,sim_0,sim_1",
            work_dir=tmp_path,
            coverage=True,
            report_dir=tmp_path / "reports",
        )
    )
    assert result.exit_code == 2
    assert list(result.outcome.detail["targets"]) == ["sim_0", "sim_1", "sim_2"]
    assert result.outcome.detail["targets"]["sim_2"]["collection"] == "complete"


def test_shared_execution_failure_aborts_later_targets_without_losing_completed_reports(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    project(tmp_path, ("verilator", "verilator", "verilator"))
    data = tmp_path / ".booley_project"
    data.mkdir()
    (data / "tests.toml").write_text("".join(f'[sim_{i}]\ntests = ["reset"]\n' for i in range(3)))

    class Unavailable(NativeExecution):
        def build(self, request):
            raise FileNotFoundError("Sandbox executable disappeared")

    flow = SimulateFlow(
        coverage_execution=lambda handle, options: (
            Unavailable() if handle.name == "sim_1" else NativeExecution()
        )
    )
    result = flow.execute(
        SimRequest(
            target="sim_0,sim_1,sim_2",
            work_dir=tmp_path,
            coverage=True,
            report_dir=tmp_path / "reports",
        )
    )
    assert result.exit_code == 2
    assert result.outcome.detail["targets"]["sim_0"]["collection"] == "complete"
    assert result.outcome.detail["targets"]["sim_1"]["abort_remaining"] is True
    assert set(result.outcome.detail["campaigns"]) == {"sim_0"}
    assert result.outcome.detail["pending_targets"] == ["sim_2"]
    assert (tmp_path / "reports/sim/1/targets/sim_2/campaign/manifest.json").is_file()
    assert not list(
        (tmp_path / "reports/sim/1/targets/sim_2/campaign").glob("work-items/*/result.json")
    )


def test_atomic_preflight_creates_no_report_or_build_path(tmp_path, monkeypatch):
    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    project(tmp_path, ("verilator", "icarus"))
    data = tmp_path / ".booley_project"
    data.mkdir()
    (data / "tests.toml").write_text('[sim_0]\ntests = ["reset"]\n[sim_1]\ntests = ["reset"]\n')
    before = set(tmp_path.rglob("*"))
    result = SimulateFlow().execute(
        SimRequest(
            target="sim_0,sim_1", work_dir=tmp_path, report_dir=tmp_path / "reports", coverage=True
        )
    )
    assert result.exit_code == 2
    assert set(tmp_path.rglob("*")) == before


def test_interactive_coverage_criterion_does_not_mutate_or_save_state(tmp_path, monkeypatch):
    from booley.criteria.state import CriterionEntry, DevelopmentState

    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    project(tmp_path)
    data = tmp_path / ".booley_project"
    data.mkdir()
    (data / "tests.toml").write_text('[sim_0]\ntests = ["reset"]\n')
    state = DevelopmentState.load(tmp_path / "state.json")
    state.strict_criteria = True
    state.criteria = {
        "coverage_sim_0": CriterionEntry(
            params={"target": "sim_0", "tests": "all", "metrics": {"line": {"min_pct": 100}}}
        )
    }
    state.save()
    state_path = tmp_path / "state.json"
    before_bytes = state_path.read_bytes()
    before_mtime = state_path.stat().st_mtime_ns
    save_calls: list[Path | None] = []
    monkeypatch.setattr(
        DevelopmentState, "save", lambda current: save_calls.append(current._file_path)
    )
    monkeypatch.setenv("BOOLEY_STATE_FILE", str(state_path))
    monkeypatch.setenv("BOOLEY_LOGS_DIR", str(tmp_path / "logs"))
    result = SimulateFlow(coverage_execution=lambda handle, options: NativeExecution()).execute(
        SimRequest(target="sim_0", work_dir=tmp_path, coverage=True)
    )
    assert result.exit_code == 0, result.outcome
    assert save_calls == []
    assert state_path.read_bytes() == before_bytes
    assert state_path.stat().st_mtime_ns == before_mtime


def test_public_cli_aliases_select_same_request():
    from booley.flows.builtin_cli import build_parser

    flow = SimulateFlow()
    help_text = build_parser(flow).format_help()
    assert "--coverage" in help_text
    assert "--cov" in help_text
    assert flow.parse_args(["--target", "sim", "--coverage"]).coverage is True
    assert flow.parse_args(["--target", "sim", "--cov"]).coverage is True
    assert flow.parse_args(["--target", "sim"]).coverage is False


@pytest.mark.parametrize("config", ["coverage = true", "flows = []", "[flows]\nsim = 1"])
def test_invalid_coverage_tables_return_preflight_error(tmp_path, monkeypatch, config):
    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    project(tmp_path)
    data = tmp_path / ".booley_project"
    data.mkdir()
    monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(data))
    (data / "booley.toml").write_text(config)
    before = set(tmp_path.rglob("*"))
    result = SimulateFlow().execute(SimRequest(target="sim_0", work_dir=tmp_path, coverage=True))
    assert result.exit_code == 2
    assert "Coverage Preflight" in result.outcome.report_text
    assert set(tmp_path.rglob("*")) == before


def test_shared_build_prerequisite_failure_aborts_with_durable_inconclusive_results(
    tmp_path, monkeypatch
):
    from booley.flows.sim.verilator_coverage import SimulationBuildResult

    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    project(tmp_path, ("verilator", "verilator"))
    data = tmp_path / ".booley_project"
    data.mkdir()
    monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(data))
    (data / "tests.toml").write_text('[sim_0]\ntests=["reset"]\n[sim_1]\ntests=["reset"]\n')
    built = []

    class Unavailable(NativeExecution):
        def build(self, request):
            built.append(request.target.identity)
            return SimulationBuildResult(
                False, "Verilator identity unavailable", infrastructure_error=True
            )

    result = SimulateFlow(coverage_execution=lambda handle, options: Unavailable()).execute(
        SimRequest(
            target="sim_0,sim_1", work_dir=tmp_path, coverage=True, report_dir=tmp_path / "reports"
        )
    )
    assert result.exit_code == 2
    assert len(built) == 1
    assert result.outcome.detail["pending_targets"] == ["sim_1"]
    target = result.outcome.detail["targets"]["sim_0"]
    assert target["simulation"] == "not_run"
    assert "coverage_campaign" not in target


def _interrupt_coverage_invocation(tmp_path, monkeypatch):
    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    revision = "a" * 40
    monkeypatch.setattr("booley.flows.sim.flow.git_full_sha", lambda *_args: revision)
    monkeypatch.setattr("booley.flows.sim.campaign.resume.git_full_sha", lambda *_args: revision)
    project(tmp_path)
    data = tmp_path / ".booley_project"
    data.mkdir()
    (data / "tests.toml").write_text('[sim_0]\ntests = ["reset", "wrap"]\n')
    reports = tmp_path / "reports"

    class Interrupted(NativeExecution):
        def run(self, request):
            super().run(request)
            raise KeyboardInterrupt("simulated process interruption")

    request = SimRequest(target="sim_0", work_dir=tmp_path, coverage=True, report_dir=reports)
    with pytest.raises(KeyboardInterrupt):
        SimulateFlow(coverage_execution=lambda handle, options: Interrupted()).execute(request)
    return reports, request


def test_interrupted_and_pruned_invocations_are_never_reused(tmp_path, monkeypatch):
    from booley.flows.sim.campaign_retention import prune_invocation

    reports, request = _interrupt_coverage_invocation(tmp_path, monkeypatch)
    original_path = next(
        (reports / "sim/1/targets/sim_0/campaign").glob(
            "work-items/*/attempts/*/coverage-campaign/native/raw/001-reset.dat"
        )
    )
    original = original_path.read_bytes()
    manifest = reports / "sim/1/targets/sim_0/campaign/manifest.json"
    resumed_reports = tmp_path / "resumed-reports"
    result = SimulateFlow(coverage_execution=lambda handle, options: NativeExecution()).execute(
        SimRequest(
            resume_from=manifest,
            work_dir=tmp_path,
            report_dir=resumed_reports,
        )
    )
    assert result.exit_code == 0
    assert (reports / "sim/1/targets/sim_0/coverage.json").is_file()
    assert original_path.read_bytes() == original
    attempts = list((reports / "sim/1/targets/sim_0/campaign").glob("work-items/*/attempts/*"))
    assert len(attempts) == 2
    for projection in (
        reports / "sim/1/targets/sim_0/simulation.json",
        resumed_reports / "sim/1/targets/sim_0/simulation.json",
    ):
        document = json.loads(projection.read_text())
        assert document["campaign_manifest"] == str(manifest)
        assert document["coverage_campaign"] == "coverage.json"
        assert document["coverage_campaign_base"] == "origin_target"
    prune_invocation(resumed_reports, 1)
    result = SimulateFlow(coverage_execution=lambda handle, options: NativeExecution()).execute(
        request
    )
    assert result.exit_code == 0
    assert (reports / "sim/2/targets/sim_0/coverage.json").is_file()
    prune_invocation(reports, 1)


def _crash_coverage_publication(tmp_path, monkeypatch, boundary):
    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    revision = "b" * 40
    monkeypatch.setattr("booley.flows.sim.flow.git_full_sha", lambda *_args: revision)
    monkeypatch.setattr("booley.flows.sim.campaign.resume.git_full_sha", lambda *_args: revision)
    project(tmp_path)
    data = tmp_path / ".booley_project"
    data.mkdir()
    (data / "tests.toml").write_text('[sim_0]\ntests = ["reset", "wrap"]\n')
    reports = tmp_path / "reports"
    runs = []

    class CountedExecution(NativeExecution):
        def run(self, request):
            runs.append(request.test)
            return super().run(request)

    def execution_factory(_handle, _options):
        return CountedExecution()

    armed = True

    def checkpoint(actual):
        nonlocal armed
        if armed and actual == boundary:
            armed = False
            raise OSError(f"injected crash at {boundary}")

    request = SimRequest(target="sim_0", work_dir=tmp_path, coverage=True, report_dir=reports)
    interrupted = SimulateFlow(
        coverage_execution=execution_factory,
        campaign_publication_checkpoint=checkpoint,
    ).execute(request)
    assert interrupted.exit_code == 2
    public = reports / "sim/1/targets/sim_0/coverage.json"
    return reports, runs, public, execution_factory


@pytest.mark.parametrize(
    ("boundary", "published", "reruns"),
    [
        ("before:coverage_campaign", False, True),
        ("after:coverage_campaign", False, True),
        ("before:coverage_reference", False, False),
        ("after:coverage_reference", True, False),
    ],
)
def test_coverage_publication_crash_resumes_at_the_aggregate_boundary(
    tmp_path, monkeypatch, boundary, published, reruns
):
    from booley.flows.sim.coverage_campaign_store import load_coverage_campaign

    reports, runs, public, execution_factory = _crash_coverage_publication(
        tmp_path, monkeypatch, boundary
    )
    assert public.exists() is published
    completed_runs = tuple(runs)
    manifest = public.parent / "campaign/manifest.json"

    resumed = SimulateFlow(coverage_execution=execution_factory).execute(
        SimRequest(resume_from=manifest, work_dir=tmp_path, report_dir=reports)
    )

    assert resumed.exit_code == 0
    assert tuple(runs) == completed_runs * (2 if reruns else 1)
    assert resolve_coverage_campaign_reference(public).campaign_path.is_file()
    nested_campaigns = list(
        public.parent.glob("campaign/work-items/*/attempts/*/coverage-campaign/coverage.json")
    )
    expected_campaigns = 2 if boundary == "after:coverage_campaign" else 1
    assert len(nested_campaigns) == expected_campaigns
    assert all(load_coverage_campaign(path).campaign.campaign_id for path in nested_campaigns)


def test_coverage_lock_covers_final_flow_report_publication(tmp_path, monkeypatch):
    from pathlib import Path

    from booley.flows.sim.campaign_retention import prune_invocation
    from booley.runtime.file_lock import LockContentionError

    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    project(tmp_path)
    data = tmp_path / ".booley_project"
    data.mkdir()
    (data / "tests.toml").write_text('[sim_0]\ntests = ["reset"]\n')
    write = Path.write_text
    checked = []

    def check_lock(path, *args, **kwargs):
        if path.name == "report.json":
            with pytest.raises(LockContentionError):
                prune_invocation(tmp_path / "reports", 1)
            checked.append(True)
        return write(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", check_lock)
    result = SimulateFlow(coverage_execution=lambda handle, options: NativeExecution()).execute(
        SimRequest(
            target="sim_0", work_dir=tmp_path, coverage=True, report_dir=tmp_path / "reports"
        )
    )
    assert result.exit_code == 0
    assert checked == [True]
    prune_invocation(tmp_path / "reports", 1)


def test_pruning_during_allocation_does_not_reuse_campaign_number(tmp_path, monkeypatch):
    from pathlib import Path

    from booley.flows.sim.campaign_retention import prune_invocation
    from tests.flows.sim.test_campaign_retention import campaign

    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    campaign(tmp_path)
    data = tmp_path / ".booley_project"
    data.mkdir()
    (data / "tests.toml").write_text('[sim_0]\ntests = ["reset", "wrap"]\n')
    sim = tmp_path / "reports/sim"
    original = Path.iterdir
    triggered = False

    def interleaved(directory):
        nonlocal triggered
        entries = list(original(directory))
        if directory == sim and not triggered:
            triggered = True
            prune_invocation(tmp_path / "reports", 1)
        return iter(entries)

    monkeypatch.setattr(Path, "iterdir", interleaved)
    result = SimulateFlow(coverage_execution=lambda handle, options: NativeExecution()).execute(
        SimRequest(
            target="sim_0", work_dir=tmp_path, coverage=True, report_dir=tmp_path / "reports"
        )
    )
    assert result.exit_code == 0
    assert (sim / "2/targets/sim_0/coverage.json").is_file()
    assert not (sim / "1").exists()
    assert (sim / ".pruned-1").is_dir()


def test_interactive_collection_then_exact_campaign_analysis(tmp_path, monkeypatch):
    from booley.specialists.coverage_analyst import CoverageAnalystSpecialist
    from tests.mcp_tools.test_coverage_analyst import Model

    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    project(tmp_path)
    data = tmp_path / ".booley_project"
    data.mkdir()
    (data / "tests.toml").write_text('[sim_0]\ntests = ["reset"]\n')
    result = SimulateFlow(coverage_execution=lambda handle, options: NativeExecution()).execute(
        SimRequest(target="sim_0", work_dir=tmp_path, coverage=True)
    )
    assert result.exit_code == 0
    campaign = tmp_path / result.outcome.detail["targets"]["sim_0"]["coverage_campaign"]
    model = Model()
    analyst = CoverageAnalystSpecialist(model=model)
    analyst.parse_args(["--work-dir", str(tmp_path), "--campaign", str(campaign)])
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    report = analyst.coverage_analyst(campaign).to_dict()
    assert report["$schema"] == "booley.coverage-analysis/v2"
    assert report["observed_evidence"]["campaign_manifest"]["$schema"] == (
        "booley.coverage-campaign/v3"
    )
    assert "points" not in report["observed_evidence"]
    assert report["observed_evidence"]["point_store_sha256"].startswith("sha256:")
    assert report["eligibility"] == "eligible"
    assert len(model.calls) == 1
    prompt = json.loads(model.calls[0].prompt)
    assert "campaign" not in prompt
    assert prompt["campaign_reference"]["storage_schema"] == "booley.coverage-campaign/v3"
    assert prompt["campaign_reference"]["point_count"] == 1
    assert "points" not in prompt
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before


@pytest.mark.parametrize(
    "verdict,hits,missing,evaluation,exit_code",
    [
        ("pass", 2, False, "pass", 0),
        ("fail", 2, False, "pass", 1),
        ("pass", 0, False, "fail", 1),
        ("fail", 0, False, "fail", 1),
        ("pass", 2, True, "blocked", 2),
    ],
)
def test_interactive_collection_keeps_criteria_unchanged_across_verdicts(
    tmp_path, monkeypatch, verdict, hits, missing, evaluation, exit_code
):
    from booley.criteria.state import CriterionEntry, DevelopmentState

    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    project(tmp_path)
    data = tmp_path / ".booley_project"
    data.mkdir()
    (data / "tests.toml").write_text('[sim_0]\ntests = ["reset"]\n')
    state_path = tmp_path / "state.json"
    state = DevelopmentState.load(state_path)
    state.strict_criteria = True
    state.criteria = {
        "coverage_sim_0": CriterionEntry(
            params={"target": "sim_0", "tests": "all", "metrics": {"line": {"min_pct": 100}}}
        ),
        "sim_pass_sim_0": CriterionEntry(params={"target": "sim_0"}),
    }
    state.save()
    before = state_path.read_bytes()
    monkeypatch.setenv("BOOLEY_STATE_FILE", str(state_path))
    monkeypatch.setenv("BOOLEY_LOGS_DIR", str(tmp_path / "logs"))
    result = SimulateFlow(
        coverage_execution=lambda handle, options: NativeExecution(
            verdict=verdict, hits=hits, missing=missing
        )
    ).execute(SimRequest(target="sim_0", work_dir=tmp_path, coverage=True))
    assert result.exit_code == exit_code
    campaign_path = Path(result.outcome.detail["campaigns"]["sim_0"]["coverage"])
    document = resolve_coverage_campaign_reference(campaign_path).loaded.campaign
    assert document.evaluation["status"] == evaluation
    assert state_path.read_bytes() == before
    saved = DevelopmentState.load(state_path)
    assert saved.criteria["coverage_sim_0"].met is False
    assert saved.criteria["sim_pass_sim_0"].met is False

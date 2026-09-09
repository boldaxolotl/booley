import json
from pathlib import Path

from booley.flows.sim.flow import SimulateFlow
from booley.flows.sim.request import SimRequest
from booley.mcp.flow_adapter import flow_schema
from tests.flows.sim.test_coverage_invocation import project
from tests.flows.sim.test_coverage_transaction import NativeExecution


def test_flow_produces_numbered_target_reports_with_public_coverage_input(tmp_path, monkeypatch):
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
    assert json.loads(campaign_path.read_text())["evaluation"]["status"] == "not_requested"
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
            raise FileNotFoundError("Session Runtime executable disappeared")

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
    assert result.outcome.detail["pending_targets"] == ["sim_2"]
    assert not (tmp_path / "reports/sim/1/targets/sim_2").exists()


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


def test_ticket_with_only_coverage_criterion_is_admitted_and_records_evidence(
    tmp_path, monkeypatch
):
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
    monkeypatch.setenv("BOOLEY_STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setenv("BOOLEY_LOGS_DIR", str(tmp_path / "logs"))
    result = SimulateFlow(coverage_execution=lambda handle, options: NativeExecution()).execute(
        SimRequest(target="sim_0", work_dir=tmp_path, coverage=True)
    )
    assert result.exit_code == 0, result.outcome
    assert DevelopmentState.load(tmp_path / "state.json").criteria["coverage_sim_0"].met is True


def test_public_cli_aliases_select_same_request():
    from booley.flows.builtin_cli import build_parser

    flow = SimulateFlow()
    help_text = build_parser(flow).format_help()
    assert "--coverage" in help_text
    assert "--cov" in help_text
    assert flow.parse_args(["--target", "sim", "--coverage"]).coverage is True
    assert flow.parse_args(["--target", "sim", "--cov"]).coverage is True
    assert flow.parse_args(["--target", "sim"]).coverage is False


import pytest


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
    assert target["simulation"] == "inconclusive"
    assert (tmp_path / target["coverage_campaign"]).is_file()


def test_interrupted_and_pruned_invocations_are_never_reused(tmp_path, monkeypatch):
    from booley.flows.sim.campaign_retention import prune_invocation

    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
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
    original = (reports / "sim/1/targets/sim_0/native/raw/001-reset.dat").read_bytes()
    result = SimulateFlow(coverage_execution=lambda handle, options: NativeExecution()).execute(
        request
    )
    assert result.exit_code == 0
    assert (reports / "sim/2/targets/sim_0/coverage.json").is_file()
    assert (reports / "sim/1/targets/sim_0/native/raw/001-reset.dat").read_bytes() == original
    prune_invocation(reports, 2)
    result = SimulateFlow(coverage_execution=lambda handle, options: NativeExecution()).execute(
        request
    )
    assert result.exit_code == 0
    assert (reports / "sim/3/targets/sim_0/coverage.json").is_file()
    prune_invocation(reports, 1)


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
    assert report["$schema"] == "booley.coverage-analysis/v1"
    assert report["eligibility"] == "eligible"
    assert len(model.calls) == 1
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
def test_ticket_public_collection_keeps_durable_coverage_and_simulation_independent(
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
    monkeypatch.setenv("BOOLEY_STATE_FILE", str(state_path))
    monkeypatch.setenv("BOOLEY_LOGS_DIR", str(tmp_path / "logs"))
    result = SimulateFlow(
        coverage_execution=lambda handle, options: NativeExecution(
            verdict=verdict, hits=hits, missing=missing
        )
    ).execute(SimRequest(target="sim_0", work_dir=tmp_path, coverage=True))
    assert result.exit_code == exit_code
    saved = DevelopmentState.load(state_path)
    coverage = saved.criteria["coverage_sim_0"]
    document = json.loads(Path(coverage.detail["coverage_campaign"]).read_text())
    assert document["evaluation"]["status"] == evaluation
    assert coverage.met is (evaluation == "pass")
    assert saved.criteria["sim_pass_sim_0"].met is (verdict == "pass")

import json
import os
import shutil
from pathlib import Path

import pytest

from booley.criteria.state import DevelopmentState
from booley.evidence.acceptance import ResolvedFlowAcceptance
from booley.evidence.fields import SOURCE_FINGERPRINT_DETAIL_KEY
from booley.flows.sim.acceptance import record_campaign_acceptance
from booley.flows.sim.campaign import SimulationCampaign, resolve_report_artifact_reference
from booley.flows.sim.coverage_campaign_store import load_coverage_campaign
from booley.flows.sim.coverage_reference import (
    REFERENCE_SCHEMA,
    resolve_coverage_campaign_reference,
)
from booley.flows.sim.flow import SimulateFlow
from booley.flows.sim.request import SimRequest
from booley.flows.sim.verilator_coverage import SimulationRunResult
from booley.mcp.flow_adapter import flow_schema
from booley.ticket_board.criteria_acceptance import check_criteria_acceptance
from booley.ticket_board.criteria_projection import project_ticket_criteria
from booley.ticket_board.flow_execution import TicketAcceptanceRecorder
from booley.ticket_board.ticket_document import (
    TicketAuthoringView,
    TicketConversionContext,
    convert_ticket_document,
)
from tests.flows.sim.test_coverage_invocation import project
from tests.flows.sim.test_coverage_transaction import NativeExecution


class _AcceptanceAdapter(TicketAcceptanceRecorder):
    def validate_and_resolve(self, _request: SimRequest) -> ResolvedFlowAcceptance:
        return ResolvedFlowAcceptance(ticket_backed=True)


class _SplitMetricsExecution(NativeExecution):
    def payload(self, _hits):
        records = (
            ("line", 1, "block", 1),
            ("expr", 2, "true", 1),
            ("branch", 3, "true", 0),
        )
        body = "".join(
            "C '\x01f\x02rtl/counter.sv"
            f"\x01l\x02{line}\x01n\x021\x01h\x02TOP.counter"
            f"\x01t\x02{metric}\x01o\x02{outcome}' {hits}\n"
            for metric, line, outcome, hits in records
        )
        return "# SystemC::Coverage-3\n" + body


def _atomic_coverage_projection():
    text = (
        "---\nsummary: Close coverage gap\ntype: verification\nbranch: main\n"
        "scope: [rtl/counter.sv]\non_success: []\nCRITERIA_MANDATORY:\n"
        "  COVERAGE:\n    sim_custom:\n      tests: [gap]\n"
        "      metrics: {line: {min_pct: 70}, expression: {min_pct: 66}}\n"
        "CRITERIA_OPTIONAL:\n  COVERAGE:\n    sim_custom:\n      tests: [gap]\n"
        "      metrics: {branch: {min_pct: 51}}\n"
        "---\n\n## Description\n\nClose the coverage gap.\n"
    )
    view = TicketAuthoringView(
        lambda selector, _flow: f"acme:demo:counter:1#{selector}",
        lambda _target: ("gap",),
    )
    converted = convert_ticket_document(
        text, TicketConversionContext("draft", lambda _generated: view)
    )
    assert converted.document is not None, converted.diagnostics
    return project_ticket_criteria(converted.document.spec)


def _ledger_json(log_dir: Path) -> dict[Path, bytes]:
    return {
        path.relative_to(log_dir): path.read_bytes()
        for path in (log_dir / "acceptance").rglob("*.json")
    }


def _prepare_atomic_coverage_ticket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, bytes]:
    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    project(tmp_path)
    core = tmp_path / "counter.core"
    core.write_text(core.read_text().replace("sim_0", "sim_custom"))
    project_data = tmp_path / ".booley_project"
    project_data.mkdir()
    (project_data / "tests.toml").write_text('[sim_custom]\ntests = ["gap"]\n')
    projection = _atomic_coverage_projection()
    state_path = tmp_path / "state.json"
    state = DevelopmentState.load(state_path)
    state.slug = "ticket"
    state.ticket_type = "verification"
    state.init_criteria(
        {**projection.required, "_report_submitted": True},
        flow_key_aliases=projection.aliases,
        criterion_params={**projection.params, "_report_submitted": {}},
        strict=True,
    )
    state.save()
    monkeypatch.setenv("BOOLEY_STATE_FILE", str(state_path))
    return state_path, state_path.read_bytes()


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


def _assert_campaign_report_schema(tmp_path: Path, campaign: dict[str, object]) -> None:
    report = json.loads((tmp_path / "reports/sim/1/report.json").read_text())
    assert report["$schema"] == "booley.simulation-report/v2"
    assert report["detail"]["campaigns"]["sim_0"]["artifacts"] == campaign["artifacts"]


def _assert_projection_tamper_rejected(campaign_path: Path) -> None:
    from booley.flows.sim.coverage_analysis_input import read_coverage_campaign

    projection_path = campaign_path.with_name("simulation.json")
    projection_raw = projection_path.read_bytes()
    projection = json.loads(projection_raw)
    projection["campaign_manifest"]["path"] = "other/manifest.json"
    projection_path.write_text(json.dumps(projection))
    with pytest.raises(ValueError, match="artifact"):
        read_coverage_campaign(campaign_path)
    projection_path.write_bytes(projection_raw)


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
    coverage_reference = target["coverage_campaign"]
    assert coverage_reference["path_base"] == "report_invocation"
    campaign_path = tmp_path / "reports/sim/1" / coverage_reference["path"]
    assert campaign_path == tmp_path / "reports/sim/1/targets/sim_0/coverage.json"
    assert json.loads(campaign_path.read_text())["$schema"] == REFERENCE_SCHEMA
    resolved = resolve_coverage_campaign_reference(campaign_path)
    assert resolved.loaded.campaign.evaluation["status"] == "not_requested"
    campaign = result.outcome.detail["campaigns"]["sim_0"]
    assert campaign["dependency"] == "local_campaign"
    assert campaign["artifacts"]["manifest"]["path"] == ("targets/sim_0/campaign/manifest.json")
    assert campaign["artifacts"]["simulation"]["path"] == ("targets/sim_0/simulation.json")
    assert campaign["artifacts"]["coverage"]["path"] == "targets/sim_0/coverage.json"
    _assert_campaign_report_schema(tmp_path, campaign)
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
    _assert_projection_tamper_rejected(campaign_path)
    assert not (tmp_path / "reports/sim_sim_0.json").exists()
    assert flow_schema(flow)["properties"]["coverage"]["type"] == "boolean"


@pytest.mark.skipif(os.name == "nt", reason="POSIX unreadable-directory regression")
@pytest.mark.parametrize("relative", ["unreadable", ".booley_project/unreadable"])
def test_unreadable_unrelated_directory_does_not_block_coverage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, relative: str
) -> None:
    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    project(tmp_path)
    project_data = tmp_path / ".booley_project"
    project_data.mkdir()
    (project_data / "tests.toml").write_text('[sim_0]\ntests = ["reset"]\n')
    unreadable = tmp_path / relative
    unreadable.mkdir(parents=True)
    unreadable.chmod(0)
    try:
        result = SimulateFlow(
            coverage_execution=lambda _handle, _options: NativeExecution()
        ).execute(
            SimRequest(
                target="sim_0",
                work_dir=tmp_path,
                coverage=True,
                report_dir=tmp_path / "reports",
            )
        )
    finally:
        unreadable.chmod(0o700)

    assert result.exit_code == 0
    assert result.outcome.detail["targets"]["sim_0"]["collection"] == "complete"


def test_copied_complete_invocation_remains_analyzable_and_independently_prunable(
    tmp_path, monkeypatch
):
    from booley.flows.sim.campaign_retention import prune_invocation
    from booley.flows.sim.coverage_analysis_input import read_coverage_campaign

    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    project(tmp_path)
    data = tmp_path / ".booley_project"
    data.mkdir()
    (data / "tests.toml").write_text('[sim_0]\ntests = ["reset", "wrap"]\n')
    reports = tmp_path / "reports"
    result = SimulateFlow(coverage_execution=lambda handle, options: NativeExecution()).execute(
        SimRequest(
            target="sim_0",
            work_dir=tmp_path,
            coverage=True,
            report_dir=reports,
        )
    )
    assert result.exit_code == 0
    original = reports / "sim/1"
    copied_reports = tmp_path / "copied-reports"
    copied = copied_reports / "sim/1"
    copied.parent.mkdir(parents=True)
    shutil.copytree(original, copied)
    copied_coverage = copied / "targets/sim_0/coverage.json"

    assert read_coverage_campaign(copied_coverage).campaign.campaign_id
    copied_projection = copied_coverage.with_name("simulation.json")
    legacy = json.loads(copied_projection.read_text())
    legacy.pop("$schema")
    legacy.pop("campaign_id")
    legacy["campaign_manifest"] = str(original / "targets/sim_0/campaign/manifest.json")
    legacy["campaign_summary"] = str(original / "targets/sim_0/campaign/summary.json")
    legacy["coverage_campaign"] = "coverage.json"
    legacy["coverage_campaign_base"] = "origin_target"
    copied_projection.write_text(json.dumps(legacy))
    hidden_original = original.with_name(".origin-hidden")
    original.rename(hidden_original)
    try:
        assert read_coverage_campaign(copied_coverage).campaign.campaign_id
    finally:
        hidden_original.rename(original)
    prune_invocation(copied_reports, 1)

    original_coverage = original / "targets/sim_0/coverage.json"
    assert read_coverage_campaign(original_coverage).campaign.campaign_id
    prune_invocation(reports, 1)


def test_multi_target_collector_error_preserves_completed_and_later_targets(tmp_path, monkeypatch):
    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    project(tmp_path, ("verilator", "verilator", "verilator"))
    data = tmp_path / ".booley_project"
    data.mkdir()
    (data / "tests.toml").write_text("".join(f'[sim_{i}]\ntests = ["reset"]\n' for i in range(3)))
    flow = SimulateFlow(
        coverage_execution=lambda handle, options: NativeExecution(missing=handle.name == "sim_1")
    )
    request = flow.parse_args(
        [
            "--target",
            "sim_2",
            "--target",
            "sim_0,sim_1",
            "--work-dir",
            str(tmp_path),
            "--coverage",
            "--report-dir",
            str(tmp_path / "reports"),
        ]
    )
    result = flow.execute(request)
    assert result.exit_code == 2
    assert request.target == "sim_2,sim_0,sim_1"
    assert list(result.outcome.detail["targets"]) == ["sim_2", "sim_0", "sim_1"]
    assert list(result.outcome.detail["campaigns"]) == ["sim_2", "sim_0", "sim_1"]
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
    outer = result.outcome.detail["targets"]["sim_1"]["evaluation"]
    target_root = tmp_path / "reports/sim/1/targets/sim_1"
    nested_path = next(
        target_root.glob("campaign/work-items/*/attempts/*/coverage-campaign/coverage.json")
    )
    nested = load_coverage_campaign(nested_path).campaign
    assert outer == nested.evaluation["status"] == "not_requested"
    assert set(result.outcome.detail["campaigns"]) == {"sim_0"}
    assert result.outcome.detail["pending_targets"] == ["sim_1", "sim_2"]
    assert (tmp_path / "reports/sim/1/targets/sim_2/campaign/manifest.json").is_file()
    assert not list(
        (tmp_path / "reports/sim/1/targets/sim_2/campaign").glob("work-items/*/result.json")
    )


def test_gated_shared_execution_failure_preserves_blocked_evaluation(tmp_path, monkeypatch):
    _state_path, _before = _prepare_atomic_coverage_ticket(tmp_path, monkeypatch)

    class Unavailable(NativeExecution):
        def build(self, request):
            raise FileNotFoundError("Sandbox executable disappeared")

    result = SimulateFlow(coverage_execution=lambda _handle, _options: Unavailable()).execute(
        SimRequest(
            target="sim_custom",
            work_dir=tmp_path,
            coverage=True,
            report_dir=tmp_path / "reports",
        )
    )

    assert result.exit_code == 2
    detail = result.outcome.detail["targets"]["sim_custom"]
    target_root = tmp_path / "reports/sim/1/targets/sim_custom"
    nested_path = next(
        target_root.glob("campaign/work-items/*/attempts/*/coverage-campaign/coverage.json")
    )
    nested = load_coverage_campaign(nested_path).campaign
    assert detail["simulation"] == "not_run"
    assert detail["collection"] == "infrastructure_error"
    assert detail["evaluation"] == nested.evaluation["status"] == "blocked"


@pytest.mark.parametrize(
    ("gated", "target", "expected"),
    [(False, "sim_0", "not_requested"), (True, "sim_custom", "blocked")],
)
def test_pre_outcome_coverage_failure_uses_prepared_policy(
    tmp_path, monkeypatch, gated, target, expected
):
    if gated:
        _prepare_atomic_coverage_ticket(tmp_path, monkeypatch)
    else:
        monkeypatch.setenv("BOOLEY_CONTAINER", "1")
        project(tmp_path)
        project_data = tmp_path / ".booley_project"
        project_data.mkdir()
        (project_data / "tests.toml").write_text('[sim_0]\ntests = ["reset"]\n')

    def fail_before_outcome(_campaign, _request):
        raise RuntimeError("failed before nested outcome")

    monkeypatch.setattr(SimulationCampaign, "run", fail_before_outcome)
    result = SimulateFlow().execute(
        SimRequest(
            target=target,
            work_dir=tmp_path,
            coverage=True,
            report_dir=tmp_path / "reports",
        )
    )

    assert result.exit_code == 2
    assert result.outcome.detail["targets"][target]["evaluation"] == expected


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


@pytest.mark.parametrize(
    ("selected", "expected_runs", "exit_code"),
    [
        (("wrap",), ["wrap"], 1),
        (("reset", "wrap"), ["reset", "wrap"], 1),
        (None, ["reset"], 0),
    ],
)
def test_explicit_coverage_selection_executes_configured_skips(
    tmp_path, monkeypatch, selected, expected_runs, exit_code
):
    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    project(tmp_path)
    data = tmp_path / ".booley_project"
    data.mkdir()
    (data / "tests.toml").write_text('[sim_0]\ntests = ["reset", "wrap"]\nskip = ["wrap"]\n')

    class PerTestVerdict(NativeExecution):
        def run(self, request):
            super().run(request)
            return SimulationRunResult("fail" if request.test.name == "wrap" else "pass")

    execution = PerTestVerdict()
    result = SimulateFlow(coverage_execution=lambda _handle, _options: execution).execute(
        SimRequest(target="sim_0", test=selected, work_dir=tmp_path, coverage=True)
    )

    assert result.exit_code == exit_code
    assert [request.test.name for request in execution.runs] == expected_runs


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


def test_ticket_campaign_acceptance_preserves_atomic_coverage_verdicts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_path, initial_state = _prepare_atomic_coverage_ticket(tmp_path, monkeypatch)

    identity = {"generation": "d" * 32, "authored_sha256": "e" * 64}
    adapter = _AcceptanceAdapter(log_dir=tmp_path / "logs", ticket_identity=identity)
    flow = SimulateFlow(coverage_execution=lambda _handle, _options: _SplitMetricsExecution())
    result = flow.execute(
        SimRequest(
            target="sim_custom",
            work_dir=tmp_path,
            coverage=True,
            report_dir=tmp_path / "reports",
        ),
        adapter=adapter,
    )

    assert result.exit_code == 1
    saved = DevelopmentState.load(state_path)
    verdicts = {
        next(iter(entry.params["metrics"])): entry.met
        for key, entry in saved.criteria.items()
        if key.startswith("coverage_")
    }
    assert verdicts == {"line": True, "expression": True, "branch": False}
    branch = next(
        entry for entry in saved.criteria.values() if "branch" in entry.params["metrics"]
    )
    assert branch.mandatory is False
    assert len(saved.acceptance_transactions) == 1
    mandatory = [
        entry
        for key, entry in saved.criteria.items()
        if key.startswith("coverage_") and entry.mandatory
    ]
    assert all(SOURCE_FINGERPRINT_DETAIL_KEY in entry.detail for entry in mandatory)

    evidence = sorted((tmp_path / "logs/acceptance/evidence").rglob("record.json"))
    records = [json.loads(path.read_text()) for path in evidence]
    assert {
        record["detail"]["criterion_metric"]: (record["met"], record["mandatory"])
        for record in records
    } == {
        "line": (True, True),
        "expression": (True, True),
        "branch": (False, False),
    }

    ledger = _ledger_json(tmp_path / "logs")
    state_path.write_bytes(initial_state)
    flow.context._state = DevelopmentState.load(state_path)
    record_campaign_acceptance(flow.context, flow.context._simulation_campaign_outcomes)
    replayed = DevelopmentState.load(state_path)
    assert replayed.acceptance_transactions == saved.acceptance_transactions
    assert _ledger_json(tmp_path / "logs") == ledger

    branch_key = next(
        key
        for key, entry in replayed.criteria.items()
        if "branch" in entry.params.get("metrics", {})
    )
    replayed.set_criterion(
        "_report_submitted",
        True,
        detail={"unmet_optional_criteria": [branch_key]},
    )
    replayed.save()
    verdict = check_criteria_acceptance(state_path, work_dir=tmp_path)
    assert verdict.disposition == "review"
    assert verdict.unmet_mandatory == []


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
                False,
                "Verilator 5.050 is not the pinned coverage collector; refresh the Sandbox image",
                infrastructure_error=True,
            )

    result = SimulateFlow(coverage_execution=lambda handle, options: Unavailable()).execute(
        SimRequest(
            target="sim_0,sim_1", work_dir=tmp_path, coverage=True, report_dir=tmp_path / "reports"
        )
    )
    assert result.exit_code == 2
    assert "Verilator 5.050 is not the pinned coverage collector" in result.outcome.report_text
    assert len(built) == 1
    assert result.outcome.detail["pending_targets"] == ["sim_0", "sim_1"]
    target = result.outcome.detail["targets"]["sim_0"]
    assert target["simulation"] == "not_run"
    assert "Verilator 5.050 is not the pinned coverage collector" in target["error"]
    report = json.loads((tmp_path / "reports/sim/1/report.json").read_text())
    assert (
        "Verilator 5.050 is not the pinned coverage collector"
        in (report["detail"]["targets"]["sim_0"]["error"])
    )
    assert "coverage_campaign" not in target
    progress = json.loads((tmp_path / "reports/sim/1/progress.json").read_text())
    assert progress["phase"] == "aborted"
    assert progress["completed_targets"] == []
    assert progress["pending_targets"] == ["sim_0", "sim_1"]


def _interrupt_coverage_invocation(tmp_path, monkeypatch, *, selected=None, skipped=()):
    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    revision = "a" * 40
    monkeypatch.setattr("booley.flows.sim.flow.git_full_sha", lambda *_args: revision)
    monkeypatch.setattr("booley.flows.sim.campaign.resume.git_full_sha", lambda *_args: revision)
    project(tmp_path)
    data = tmp_path / ".booley_project"
    data.mkdir()
    (data / "tests.toml").write_text(
        f'[sim_0]\ntests = ["reset", "wrap"]\nskip = {json.dumps(list(skipped))}\n'
    )
    reports = tmp_path / "reports"

    class Interrupted(NativeExecution):
        def run(self, request):
            super().run(request)
            raise KeyboardInterrupt("simulated process interruption")

    request = SimRequest(
        target="sim_0",
        test=selected,
        work_dir=tmp_path,
        coverage=True,
        report_dir=reports,
    )
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
    resumed_reports = reports
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
    projection = reports / "sim/1/targets/sim_0/simulation.json"
    document = json.loads(projection.read_text())
    assert document["campaign_manifest"]["path"] == "campaign/manifest.json"
    assert document["campaign_manifest"]["path_base"] == "origin_target"
    assert document["coverage_campaign"]["path"] == "coverage.json"
    assert document["coverage_campaign"]["path_base"] == "origin_target"
    assert not (resumed_reports / "sim/2/targets/sim_0/simulation.json").exists()
    resume_report = json.loads((resumed_reports / "sim/2/report.json").read_text())
    resume_campaign = resume_report["detail"]["campaigns"]["sim_0"]
    assert resume_campaign["dependency"] == "external_origin_campaign"
    assert resume_campaign["artifacts"]["manifest"]["path_base"] == "reports_root"
    assert resume_campaign["artifacts"]["manifest"]["path"] == (
        "sim/1/targets/sim_0/campaign/manifest.json"
    )
    prune_invocation(resumed_reports, 2)
    result = SimulateFlow(coverage_execution=lambda handle, options: NativeExecution()).execute(
        request
    )
    assert result.exit_code == 0
    assert (reports / "sim/3/targets/sim_0/coverage.json").is_file()
    prune_invocation(reports, 1)


def test_resume_retains_explicit_configured_skipped_test(tmp_path, monkeypatch):
    reports, _request = _interrupt_coverage_invocation(
        tmp_path, monkeypatch, selected=("wrap",), skipped=("wrap",)
    )
    manifest = reports / "sim/1/targets/sim_0/campaign/manifest.json"
    execution = NativeExecution()

    result = SimulateFlow(coverage_execution=lambda _handle, _options: execution).execute(
        SimRequest(resume_from=manifest, work_dir=tmp_path, report_dir=tmp_path / "resumed")
    )

    assert result.exit_code == 0
    assert [request.test.name for request in execution.runs] == ["wrap"]
    report_path = tmp_path / "resumed/sim/1/report.json"
    report = json.loads(report_path.read_text())
    detail = report["detail"]
    campaign = detail["campaigns"]["sim_0"]
    assert campaign["dependency"] == "external_origin_campaign"
    assert set(campaign["artifacts"]) == {"manifest", "simulation", "coverage"}
    assert {reference["path_base"] for reference in campaign["artifacts"].values()} == {
        "external_origin_target"
    }
    assert detail["targets"]["sim_0"]["coverage_campaign"] == campaign["artifacts"]["coverage"]
    resolved = resolve_report_artifact_reference(
        report_path,
        campaign["artifacts"]["manifest"],
        expected_kind="simulation_campaign_manifest",
        expected_owner=campaign["campaign_id"],
        maximum=1024 * 1024,
        external_origin_target=manifest.parents[1],
    )
    assert resolved.path == manifest


def test_coverage_resume_uses_manifest_when_origin_progress_is_missing(tmp_path, monkeypatch):
    reports, _request = _interrupt_coverage_invocation(tmp_path, monkeypatch)
    manifest = reports / "sim/1/targets/sim_0/campaign/manifest.json"
    (reports / "sim/1/progress.json").unlink()
    resumed_reports = tmp_path / "resumed"

    result = SimulateFlow(coverage_execution=lambda *_args: NativeExecution()).execute(
        SimRequest(resume_from=manifest, work_dir=tmp_path, report_dir=resumed_reports)
    )

    assert result.exit_code == 0
    progress = json.loads((resumed_reports / "sim/1/progress.json").read_text())
    assert progress["phase"] == "complete"
    assert progress["completed_targets"] == ["sim_0"]
    assert progress["pending_targets"] == []


def test_interrupted_coverage_resume_publishes_terminal_progress(tmp_path, monkeypatch):
    reports, _request = _interrupt_coverage_invocation(tmp_path, monkeypatch)
    manifest = reports / "sim/1/targets/sim_0/campaign/manifest.json"
    resumed_reports = tmp_path / "resumed"

    class InterruptedAgain(NativeExecution):
        def run(self, request):
            super().run(request)
            raise KeyboardInterrupt("resume interrupted")

    with pytest.raises(KeyboardInterrupt, match="resume interrupted"):
        SimulateFlow(coverage_execution=lambda *_args: InterruptedAgain()).execute(
            SimRequest(resume_from=manifest, work_dir=tmp_path, report_dir=resumed_reports)
        )

    progress = json.loads((resumed_reports / "sim/1/progress.json").read_text())
    assert progress["phase"] == "aborted"
    assert progress["completed_targets"] == []
    assert progress["pending_targets"] == ["sim_0"]


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

    from booley.flows.sim.campaign_retention import CampaignRetentionError, prune_invocation

    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    project(tmp_path)
    data = tmp_path / ".booley_project"
    data.mkdir()
    (data / "tests.toml").write_text('[sim_0]\ntests = ["reset"]\n')
    write = Path.write_text
    checked = []

    def check_lock(path, *args, **kwargs):
        if path.name == "report.json":
            with pytest.raises(CampaignRetentionError, match="still being produced"):
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


def test_full_pruning_rejects_extra_nested_coverage_payload(tmp_path, monkeypatch):
    from booley.flows.sim.campaign_retention import CampaignRetentionError, prune_invocation

    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    project(tmp_path)
    data = tmp_path / ".booley_project"
    data.mkdir()
    (data / "tests.toml").write_text('[sim_0]\ntests = ["reset"]\n')
    reports = tmp_path / "reports"
    result = SimulateFlow(coverage_execution=lambda handle, options: NativeExecution()).execute(
        SimRequest(target="sim_0", work_dir=tmp_path, coverage=True, report_dir=reports)
    )
    assert result.exit_code == 0
    native = next(
        (reports / "sim/1/targets/sim_0/campaign").glob(
            "work-items/*/attempts/*/coverage-campaign/native/raw"
        )
    )
    stray = native / "999-extra.dat"
    stray.write_text("do not delete", encoding="utf-8")

    with pytest.raises(CampaignRetentionError, match=r"999-extra\.dat"):
        prune_invocation(reports, 1)

    assert stray.read_text(encoding="utf-8") == "do not delete"
    assert (reports / "sim/1").is_dir()


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
    campaign = next(tmp_path.rglob("targets/sim_0/coverage.json"))
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
    campaign_path = next(tmp_path.rglob("targets/sim_0/coverage.json"))
    document = resolve_coverage_campaign_reference(campaign_path).loaded.campaign
    assert document.evaluation["status"] == evaluation
    assert state_path.read_bytes() == before
    saved = DevelopmentState.load(state_path)
    assert saved.criteria["coverage_sim_0"].met is False
    assert saved.criteria["sim_pass_sim_0"].met is False

import json
import tempfile
from dataclasses import replace
from fractions import Fraction
from pathlib import Path

import pytest

from booley.flows.sim.campaign_retention import _coverage_owned_files
from booley.flows.sim.coverage_campaign import DurableTargetIdentity, freeze_coverage_mapping
from booley.flows.sim.coverage_campaign_store import load_coverage_campaign
from booley.flows.sim.coverage_invocation import (
    CoverageInvocationRequest,
    prepare_coverage_invocation,
)
from booley.flows.sim.coverage_policy import CoverageCriterion, CoverageThreshold
from booley.flows.sim.coverage_transaction import CoverageTargetOutcome, run_coverage_target
from booley.flows.sim.execution.contract import PreSimEvidence
from booley.flows.sim.flow import SimulateFlow
from booley.flows.sim.verilator_coverage import (
    PINNED_VERILATOR,
    SimulationBuildResult,
    SimulationCommandResult,
    SimulationRunResult,
)
from booley.specialists.coverage_analyst import CoverageAnalystSpecialist
from tests.flows.sim.test_coverage_invocation import project


class NativeExecution:
    """Only the external simulator/native-merge port is substituted."""

    def __init__(self, *, verdict="pass", hits=2, missing=False):
        self.verdict, self.hits, self.missing = verdict, hits, missing
        self.runs = []
        self.build_root = Path(tempfile.mkdtemp(prefix="booley-coverage-test-"))
        self.executable = self.build_root / "simv"
        self.executable.write_bytes(b"authenticated simulator image")

    def build(self, request):
        return SimulationBuildResult(True, collector=PINNED_VERILATOR)

    def payload(self, hits):
        return (
            "# SystemC::Coverage-3\n"
            "C '\x01f\x02rtl/counter.sv\x01l\x021\x01n\x021\x01h\x02TOP.counter"
            f"\x01t\x02line\x01o\x02block' {hits}\n"
        )

    def run(self, request):
        self.runs.append(request)
        if not self.missing:
            request.raw_path.parent.mkdir(parents=True, exist_ok=True)
            request.raw_path.write_text(self.payload(self.hits))
        return SimulationRunResult(self.verdict)

    def command(self, request):
        request.output_path.parent.mkdir(parents=True, exist_ok=True)
        request.output_path.write_text(self.payload(self.hits * len(self.runs)))
        return SimulationCommandResult(0)

    def authenticated_image(self):
        return self.build_root, (self.executable,)

    def bind_authenticated_attempt(self, snapshot_root, run_cwd):
        self.executable = snapshot_root / self.executable.name
        self.run_cwd = run_cwd


class Progress:
    def __init__(self):
        self.outcomes = []

    def completed(self, outcome):
        self.outcomes.append(outcome)


def test_ungated_target_persists_valid_campaign_and_independent_simulation(tmp_path: Path):
    context = project(tmp_path)
    prepared = prepare_coverage_invocation(CoverageInvocationRequest(("sim_0",)), context)
    plan = replace(prepared.plan.targets[0], invocation_dir=tmp_path / "reports/sim/1")
    execution, progress = NativeExecution(), Progress()
    outcome = run_coverage_target(plan, execution, progress)
    assert outcome.exit_code == 0
    assert len(execution.runs) == 2
    document = json.loads(outcome.campaign_path.read_text())
    assert document["$schema"] == "booley.coverage-campaign/v4"
    assert document["scoring"] == {"status": "valid", "reason": None}
    assert document["source_rollups"]
    assert "points" not in document
    campaign = load_coverage_campaign(
        outcome.campaign_path, DurableTargetIdentity(plan.handle.identity)
    ).campaign
    assert campaign.evaluation["status"] == "not_requested"
    assert campaign.rollups[0].eligible_points == 1
    assert campaign.points[0].hits_by_run == {"run:001:reset": 2, "run:002:wrap": 2}
    simulation = json.loads(outcome.simulation_path.read_text())
    assert simulation["passed"] is True
    assert simulation["coverage_campaign"] == "coverage.json"
    assert "source_rollups" not in simulation
    assert "source_rollups" not in outcome.detail
    assert progress.outcomes == [outcome]


def test_simulation_projection_preserves_pre_sim_failure_detail(tmp_path: Path):
    context = project(tmp_path)
    prepared = prepare_coverage_invocation(CoverageInvocationRequest(("sim_0",)), context)
    plan = replace(prepared.plan.targets[0], invocation_dir=tmp_path / "reports/sim/1")

    class PreSimFailure(NativeExecution):
        def run(self, request):
            self.runs.append(request)
            evidence = PreSimEvidence(
                ("prepare-vectors",),
                (request.test.name,),
                "failed",
                0.2,
                "vector generator rejected input",
            )
            return SimulationRunResult("elab_error", evidence.detail, evidence)

        def command(self, request):
            raise AssertionError("merge must not run after a Pre-Sim failure")

    outcome = run_coverage_target(plan, PreSimFailure(), Progress())

    assert outcome.exit_code == 2
    simulation = json.loads(outcome.simulation_path.read_text())
    assert simulation["tests"][0]["pre_sim"]["status"] == "failed"
    assert simulation["tests"][0]["error_tail"] == (
        "Pre-Sim Commands failed (failed): vector generator rejected input"
    )


def test_missing_input_is_a_design_failure_with_structured_abort_projection(
    tmp_path: Path,
) -> None:
    context = project(tmp_path)
    prepared = prepare_coverage_invocation(CoverageInvocationRequest(("sim_0",)), context)
    plan = replace(prepared.plan.targets[0], invocation_dir=tmp_path / "reports/sim/1")

    class MissingInput(NativeExecution):
        def run(self, request):
            self.runs.append(request)
            return SimulationRunResult(
                "fail",
                "$readmemh: Cannot open memory.hex",
                termination="fatal_init",
                failure_kind="missing_input",
                simulator_returncode=3,
            )

        def command(self, request):
            raise AssertionError("merge must not run after a design abort")

    outcome = run_coverage_target(plan, MissingInput(), Progress())

    assert outcome.exit_code == 1
    assert outcome.abort_remaining is False
    simulation = json.loads(outcome.simulation_path.read_text())
    assert simulation["passed"] is False
    assert simulation["simulation"] == "aborted"
    assert simulation["collection"] == "incomplete"
    assert simulation["tests"][0] == {
        "name": "reset",
        "verdict": "fail",
        "passed": False,
        "collection": "collector_error",
        "termination": "fatal_init",
        "failure_kind": "missing_input",
        "error_tail": "$readmemh: Cannot open memory.hex",
        "simulator_returncode": 3,
    }
    campaign = json.loads(outcome.campaign_path.read_text())
    assert campaign["collection"]["status"] == "incomplete"


def test_coverage_cli_renders_one_concise_line_per_pre_sim_firing(tmp_path: Path):
    detail = freeze_coverage_mapping(
        {
            "simulation": "elab_error",
            "collection": "collector_error",
            "evaluation": "not_requested",
            "tests": [
                {
                    "name": "reset",
                    "pre_sim": {"status": "failed", "elapsed_s": 0.125},
                },
                {
                    "name": "wrap",
                    "pre_sim": {"status": "passed", "elapsed_s": 0.25},
                },
            ],
        }
    )
    outcome = CoverageTargetOutcome(
        "sim_0",
        2,
        tmp_path / "missing-coverage.json",
        tmp_path / "missing-simulation.json",
        detail,
    )

    rendered = SimulateFlow()._coverage_result([outcome]).report_text

    assert "sim_0: pre-sim=failed test=reset duration=0.125s" in rendered
    assert "sim_0: pre-sim=passed test=wrap duration=0.250s" in rendered


@pytest.mark.parametrize(
    ("failure", "events", "expected_finding", "gated"),
    [
        ("missing_start", None, "COV_WINDOW_HOOK_MISSING", False),
        (
            "duplicate_start",
            [
                {"hook": "start", "sequence": 1, "success": True},
                {"hook": "start", "sequence": 2, "success": True},
            ],
            "COV_WINDOW_HOOK_DUPLICATE",
            True,
        ),
        (
            "duplicate_write",
            [
                {"hook": "write", "sequence": 1, "success": True},
                {"hook": "write", "sequence": 2, "success": True},
            ],
            "COV_WRITE_HOOK_DUPLICATE",
            False,
        ),
    ],
)
def test_invalid_hook_evidence_withholds_scores_but_retains_diagnostics(
    tmp_path: Path,
    failure: str,
    events: list[dict[str, object]] | None,
    expected_finding: str,
    gated: bool,
) -> None:
    plan = _invalid_hook_plan(tmp_path, failure, gated)
    outcome = run_coverage_target(plan, InvalidHookExecution(events), Progress())
    _assert_invalid_hook_campaign(outcome, plan, events, expected_finding, gated)
    _assert_invalid_hook_consumers(tmp_path, outcome)


def _invalid_hook_plan(tmp_path: Path, failure: str, gated: bool):
    context = project(tmp_path)
    if gated:
        criterion = CoverageCriterion(
            DurableTargetIdentity("acme:demo:counter:1#sim_0"),
            (CoverageThreshold("line", Fraction(100)),),
            ("reset",),
        )
        context = replace(context, criteria={"coverage_sim_0": criterion})
    prepared = prepare_coverage_invocation(
        CoverageInvocationRequest(("sim_0",), ("reset",)), context
    )
    plan = replace(prepared.plan.targets[0], invocation_dir=tmp_path / "reports/sim/1")
    request = plan.collection_request
    assert request is not None
    custom_main = failure == "duplicate_write"
    target = replace(
        request.target,
        harness="custom_main" if custom_main else "generated_main",
        custom_main_hooks=("write_hook",) if custom_main else (),
    )
    return replace(
        plan,
        collection_request=replace(request, target=target, reset_included=custom_main),
    )


class InvalidHookExecution(NativeExecution):
    def __init__(self, events: list[dict[str, object]] | None) -> None:
        super().__init__()
        self.events = events

    def run(self, request):
        self.runs.append(request)
        request.raw_path.parent.mkdir(parents=True, exist_ok=True)
        request.raw_path.write_text(self.payload(self.hits), encoding="utf-8")
        if self.events is not None:
            assert request.hook_evidence_path is not None
            request.hook_evidence_path.parent.mkdir(parents=True, exist_ok=True)
            request.hook_evidence_path.write_text(
                json.dumps(
                    {
                        "$schema": "booley.coverage-hook/v1",
                        "run_id": request.run_id,
                        "events": self.events,
                    }
                ),
                encoding="utf-8",
            )
        return SimulationRunResult("pass")


def _assert_invalid_hook_campaign(outcome, plan, events, expected_finding, gated) -> None:
    assert outcome.exit_code == 2
    document = json.loads(outcome.campaign_path.read_text(encoding="utf-8"))
    assert document["$schema"] == "booley.coverage-campaign/v4"
    assert document["collection"]["status"] == "collector_error"
    assert document["scoring"] == {"status": "invalid", "reason": "collector_error"}
    assert document["rollups"] == []
    assert document["source_rollups"] == []
    assert document["point_store"]["point_count"] > 0
    assert any(item["kind"] == "raw_native" for item in document["artifacts"])
    if events is not None:
        assert any(item["kind"] == "coverage_hook_evidence" for item in document["artifacts"])
    assert expected_finding in {item["code"] for item in document["findings"]}
    persisted = load_coverage_campaign(
        outcome.campaign_path, DurableTargetIdentity(plan.handle.identity)
    )
    loaded = persisted.campaign
    assert loaded.points
    assert loaded.rollups == ()
    target_dir = outcome.campaign_path.parent
    owned = _coverage_owned_files(target_dir, target_dir, persisted)
    assert all(path.absolute() in owned for path in target_dir.glob("hooks/*.json"))
    if gated:
        assert loaded.evaluation["status"] == "blocked"
        assert loaded.evaluation["metrics"] == ()
        assert all(
            item["code"] != "COV_EVAL_EMPTY_DENOMINATOR"
            for item in loaded.evaluation["diagnostics"]
        )


def _assert_invalid_hook_consumers(tmp_path: Path, outcome: CoverageTargetOutcome) -> None:
    simulation = json.loads(outcome.simulation_path.read_text(encoding="utf-8"))
    assert simulation["passed"] is True
    assert simulation["collection"] == "collector_error"
    assert outcome.detail["collection"] == "collector_error"
    reporting = replace(
        outcome,
        campaign_path=tmp_path / "missing-coverage.json",
        simulation_path=tmp_path / "missing-simulation.json",
    )
    assert "collection=collector_error" in SimulateFlow()._coverage_result([reporting]).report_text
    provider_calls = []
    analyst = CoverageAnalystSpecialist(model=provider_calls.append)
    result = analyst.execute_cli(
        ["--work-dir", str(tmp_path), "--campaign", str(outcome.campaign_path)]
    )
    assert result.exit_code == 2
    assert provider_calls == []
    assert getattr(analyst, "_evidence_campaign_path", None) is None


@pytest.mark.parametrize(
    "verdict,hits,tests,expected,evaluation",
    [
        ("pass", 2, None, 0, "pass"),
        ("fail", 2, None, 1, "pass"),
        ("pass", 0, None, 0, "fail"),
        ("fail", 0, None, 1, "fail"),
        ("pass", 2, ("reset",), 2, "blocked"),
    ],
)
def test_gated_verdicts_preserve_independent_truth(
    tmp_path, verdict, hits, tests, expected, evaluation
):
    from fractions import Fraction

    from booley.flows.sim.coverage_policy import CoverageCriterion, CoverageThreshold

    context = project(tmp_path)
    criterion = CoverageCriterion(
        DurableTargetIdentity("acme:demo:counter:1#sim_0"),
        (CoverageThreshold("line", Fraction(100)),),
        ("reset", "wrap"),
    )
    context = replace(context, criteria={"coverage_sim_0": criterion})
    prepared = prepare_coverage_invocation(CoverageInvocationRequest(("sim_0",), tests), context)
    plan = replace(prepared.plan.targets[0], invocation_dir=tmp_path / "reports/sim/1")
    outcome = run_coverage_target(plan, NativeExecution(verdict=verdict, hits=hits), Progress())
    assert outcome.exit_code == expected
    document = json.loads(outcome.campaign_path.read_text())
    assert document["evaluation"]["status"] == evaluation
    assert {run["simulation_verdict"] for run in document["tests"]["runs"]} == {verdict}


def test_threshold_miss_exits_zero_with_valid_coverage(tmp_path):
    from fractions import Fraction

    from booley.flows.sim.coverage_policy import CoverageCriterion, CoverageThreshold

    criterion = CoverageCriterion(
        DurableTargetIdentity("acme:demo:counter:1#sim_0"),
        (CoverageThreshold("line", Fraction(100)),),
        None,
    )
    context = replace(project(tmp_path), criteria={"coverage_sim_0": criterion})
    prepared = prepare_coverage_invocation(CoverageInvocationRequest(("sim_0",)), context)
    plan = replace(prepared.plan.targets[0], invocation_dir=tmp_path / "reports/sim/1")

    outcome = run_coverage_target(plan, NativeExecution(hits=0), Progress())

    assert outcome.exit_code == 0
    assert json.loads(outcome.campaign_path.read_text())["evaluation"]["status"] == "fail"


def test_all_tests_criterion_reports_suite_mismatch_after_configured_skip(tmp_path):
    from fractions import Fraction

    from booley.flows.sim.coverage_policy import CoverageCriterion, CoverageThreshold

    context = project(tmp_path)
    criterion = CoverageCriterion(
        DurableTargetIdentity("acme:demo:counter:1#sim_0"),
        (CoverageThreshold("line", Fraction(100)),),
        None,
    )
    context = replace(
        context,
        criteria={"coverage_sim_0": criterion},
        skipped_tests={"sim_0": ("wrap",)},
    )
    prepared = prepare_coverage_invocation(CoverageInvocationRequest(("sim_0",)), context)
    assert prepared.plan.targets[0].selected_tests == ("reset",)
    plan = replace(prepared.plan.targets[0], invocation_dir=tmp_path / "reports/sim/1")

    outcome = run_coverage_target(plan, NativeExecution(), Progress())

    assert outcome.exit_code == 2
    evaluation = json.loads(outcome.campaign_path.read_text())["evaluation"]
    assert evaluation["status"] == "blocked"
    assert [item["code"] for item in evaluation["diagnostics"]] == ["COV_EVAL_SUITE_MISMATCH"]


@pytest.mark.parametrize(
    "payload,missing",
    [
        (None, True),
        ("# SystemC::Coverage-4\n", False),
        ("# SystemC::Coverage-3\ninvalid\n", False),
    ],
)
def test_collector_errors_are_durable_command_errors_without_changing_simulation(
    tmp_path, payload, missing
):
    context = project(tmp_path)
    prepared = prepare_coverage_invocation(CoverageInvocationRequest(("sim_0",)), context)
    plan = replace(prepared.plan.targets[0], invocation_dir=tmp_path / "reports/sim/1")

    class BrokenNative(NativeExecution):
        def payload(self, hits):
            return payload

    outcome = run_coverage_target(plan, BrokenNative(missing=missing), Progress())
    assert outcome.exit_code == 2
    document = json.loads(outcome.campaign_path.read_text())
    load_coverage_campaign(outcome.campaign_path, DurableTargetIdentity(plan.handle.identity))
    assert document["evaluation"]["status"] == "not_requested"
    assert document["findings"]
    assert json.loads(outcome.simulation_path.read_text())["passed"] is True


@pytest.mark.parametrize("gated", [False, True])
def test_invalid_waivers_block_only_requested_evaluation(tmp_path, gated):
    from fractions import Fraction

    from booley.flows.sim.coverage_policy import CoverageCriterion, CoverageThreshold
    from booley.flows.sim.coverage_waivers import CoverageWaiverConfig

    context = project(tmp_path)
    criterion = CoverageCriterion(
        DurableTargetIdentity("acme:demo:counter:1#sim_0"),
        (CoverageThreshold("line", Fraction(100)),),
        None,
    )
    context = replace(
        context,
        criteria={"coverage_sim_0": criterion} if gated else {},
        waiver_config=CoverageWaiverConfig("rtl_repository", "../unsafe"),
    )
    prepared = prepare_coverage_invocation(CoverageInvocationRequest(("sim_0",)), context)
    plan = replace(prepared.plan.targets[0], invocation_dir=tmp_path / "reports/sim/1")
    outcome = run_coverage_target(plan, NativeExecution(), Progress())
    assert outcome.exit_code == (2 if gated else 0)
    document = json.loads(outcome.campaign_path.read_text())
    assert document["evaluation"]["status"] == ("blocked" if gated else "not_requested")
    assert document["collection"]["status"] == "complete"


def test_ticket_publishes_campaign_before_independent_acceptance_evidence(tmp_path):
    from fractions import Fraction

    from booley.criteria.state import CriterionEntry, DevelopmentState
    from booley.flows.sim.coverage_acceptance import CoverageAcceptance
    from booley.flows.sim.coverage_policy import CoverageCriterion, CoverageThreshold
    from booley.ticket_board.flow_execution import TicketAcceptanceRecorder

    context = project(tmp_path)
    state = DevelopmentState.load(tmp_path / "state.json")
    state.strict_criteria = True
    state.criteria = {"coverage_sim_0": CriterionEntry(), "sim_pass_sim_0": CriterionEntry()}
    criterion = CoverageCriterion(
        DurableTargetIdentity("acme:demo:counter:1#sim_0"),
        (CoverageThreshold("line", Fraction(100)),),
        None,
    )
    context = replace(context, criteria={"coverage_sim_0": criterion})
    prepared = prepare_coverage_invocation(CoverageInvocationRequest(("sim_0",)), context)
    plan = replace(
        prepared.plan.targets[0],
        invocation_dir=tmp_path / "reports/sim/1",
        acceptance=CoverageAcceptance(
            state,
            TicketAcceptanceRecorder(log_dir=tmp_path / "logs"),
        ),
    )
    outcome = run_coverage_target(plan, NativeExecution(verdict="fail"), Progress())
    saved = DevelopmentState.load(tmp_path / "state.json")
    assert saved.criteria["coverage_sim_0"].met is True
    assert "_source_fingerprint" in saved.criteria["coverage_sim_0"].detail
    assert saved.criteria["sim_pass_sim_0"].met is False
    records = [
        json.loads(path.read_text())
        for path in (tmp_path / "logs/acceptance/evidence").rglob("record.json")
    ]
    assert {record["criterion"]: record["met"] for record in records} == {
        "coverage_sim_0": True,
        "sim_pass_sim_0": False,
    }
    assert all(
        record["detail"]["campaign_id"]
        == load_coverage_campaign(
            outcome.campaign_path,
            DurableTargetIdentity("acme:demo:counter:1#sim_0"),
        ).campaign.campaign_id
        for record in records
    )
    assert all(str(outcome.campaign_path) not in json.dumps(record) for record in records)


def test_unknown_native_records_remain_in_canonical_campaign(tmp_path):
    context = project(tmp_path)
    prepared = prepare_coverage_invocation(CoverageInvocationRequest(("sim_0",)), context)
    plan = replace(prepared.plan.targets[0], invocation_dir=tmp_path / "reports/sim/1")

    class FutureNative(NativeExecution):
        def payload(self, hits):
            known = super().payload(hits)
            return known + known.splitlines(keepends=True)[1].replace("\x02line", "\x02future")

    outcome = run_coverage_target(plan, FutureNative(), Progress())
    assert outcome.exit_code == 0
    document = json.loads(outcome.campaign_path.read_text())
    assert document["normalization"]["status"] == "complete_with_unknown_records"
    records = document["normalization"]["unrecognized_records"]
    assert len(records) == 2
    assert records[0]["native_type"] == "future"
    assert records[0]["hits"] == 2
    assert "native_key" in records[0]


def test_simulation_report_failure_leaves_prior_criterion_evidence_unchanged(tmp_path):
    from booley.criteria.state import CriterionEntry, DevelopmentState
    from booley.flows.sim.coverage_acceptance import CoverageAcceptance
    from booley.ticket_board.flow_execution import TicketAcceptanceRecorder

    context = project(tmp_path)
    state = DevelopmentState.load(tmp_path / "state.json")
    state.criteria = {"sim_pass_sim_0": CriterionEntry(met=True, detail={"prior": "valid"})}
    state.save()
    before = (tmp_path / "state.json").read_bytes()
    prepared = prepare_coverage_invocation(CoverageInvocationRequest(("sim_0",)), context)
    plan = replace(
        prepared.plan.targets[0],
        invocation_dir=tmp_path / "reports/sim/1",
        acceptance=CoverageAcceptance(
            state,
            TicketAcceptanceRecorder(log_dir=tmp_path / "logs"),
        ),
    )
    (tmp_path / "reports/sim/1/targets/sim_0/simulation.json").mkdir(parents=True)
    outcome = run_coverage_target(plan, NativeExecution(verdict="fail"), Progress())
    assert outcome.exit_code == 2
    assert outcome.abort_remaining is True
    assert outcome.campaign_path.is_file()
    assert (tmp_path / "state.json").read_bytes() == before
    assert not (tmp_path / "logs").exists()


def test_source_drift_after_preflight_never_runs_with_stale_provenance(tmp_path):
    context = project(tmp_path)
    prepared = prepare_coverage_invocation(CoverageInvocationRequest(("sim_0",)), context)
    plan = replace(prepared.plan.targets[0], invocation_dir=tmp_path / "reports/sim/1")
    (tmp_path / "rtl/counter.sv").write_text("module changed; endmodule\n")
    execution = NativeExecution()
    outcome = run_coverage_target(plan, execution, Progress())
    assert outcome.exit_code == 2
    assert execution.runs == []
    assert not outcome.campaign_path.exists()


@pytest.mark.parametrize("failure_at", ["merge", "second_run"])
def test_infrastructure_failure_preserves_completed_simulation_truth(tmp_path, failure_at):
    context = project(tmp_path)
    prepared = prepare_coverage_invocation(CoverageInvocationRequest(("sim_0",)), context)
    plan = replace(prepared.plan.targets[0], invocation_dir=tmp_path / "reports/sim/1")

    class Interrupted(NativeExecution):
        def run(self, request):
            if failure_at == "second_run" and self.runs:
                raise FileNotFoundError("runner unavailable")
            return super().run(request)

        def command(self, request):
            raise FileNotFoundError("merge executable unavailable")

    outcome = run_coverage_target(plan, Interrupted(), Progress())
    assert outcome.exit_code == 2
    assert outcome.abort_remaining is True
    campaign = load_coverage_campaign(
        outcome.campaign_path, DurableTargetIdentity(plan.handle.identity)
    ).campaign
    expected = ["pass", "pass"] if failure_at == "merge" else ["pass", "inconclusive"]
    assert [run.simulation_verdict for run in campaign.runs] == expected
    assert campaign.collection["status"] == "collector_error"
    assert campaign.evaluation["status"] == "not_requested"
    simulation = json.loads(outcome.simulation_path.read_text())
    assert [run["verdict"] for run in simulation["tests"]] == expected
    assert simulation["passed"] is (failure_at == "merge")
    assert simulation["simulation"] == ("pass" if failure_at == "merge" else "inconclusive")


def test_merge_failure_keeps_simulation_truth_when_later_publication_fails(tmp_path):
    context = project(tmp_path)
    prepared = prepare_coverage_invocation(CoverageInvocationRequest(("sim_0",)), context)
    plan = replace(prepared.plan.targets[0], invocation_dir=tmp_path / "reports/sim/1")

    class MergeUnavailable(NativeExecution):
        def command(self, request):
            return SimulationCommandResult(1, stderr="merge executable unavailable")

    def checkpoint(boundary):
        if boundary == "after:coverage_campaign":
            raise OSError("injected post-merge publication failure")

    outcome = run_coverage_target(
        plan,
        MergeUnavailable(),
        Progress(),
        publication_checkpoint=checkpoint,
    )

    assert outcome.exit_code == 2
    assert outcome.detail["passed"] is True
    assert outcome.detail["simulation"] == "pass"
    assert outcome.detail["collection"] == "infrastructure_error"
    assert outcome.detail["evaluation"] == "not_requested"
    assert outcome.detail["error"] == "injected post-merge publication failure"


def test_build_infrastructure_failure_does_not_invent_a_simulation_verdict(tmp_path):
    context = project(tmp_path)
    prepared = prepare_coverage_invocation(CoverageInvocationRequest(("sim_0",)), context)
    plan = replace(prepared.plan.targets[0], invocation_dir=tmp_path / "reports/sim/1")

    class Unavailable(NativeExecution):
        def build(self, request):
            return SimulationBuildResult(False, "builder unavailable", infrastructure_error=True)

    outcome = run_coverage_target(plan, Unavailable(), Progress())

    assert outcome.exit_code == 2
    assert outcome.detail["passed"] is None
    assert outcome.detail["simulation"] == "not_run"
    assert outcome.detail["collection"] == "infrastructure_error"


def test_real_adapter_reports_missing_verilator_provenance_as_shared_failure(tmp_path):
    from booley.flows.base import SubprocessResult
    from booley.flows.sim.execution.contract import SimulationOptions
    from booley.flows.sim.verilator_coverage_execution import VerilatorCoverageExecution

    context = project(tmp_path)
    prepared = prepare_coverage_invocation(CoverageInvocationRequest(("sim_0",)), context)
    plan = replace(prepared.plan.targets[0], invocation_dir=tmp_path / "reports/sim/1")
    execution = VerilatorCoverageExecution(
        plan.handle,
        invoke=lambda command, timeout: SubprocessResult(0, "Verilator 5.052", ""),
        options=SimulationOptions(),
        provenance_path=tmp_path / "missing-provenance",
    )
    outcome = run_coverage_target(plan, execution, Progress())
    assert outcome.exit_code == 2
    assert outcome.abort_remaining is True
    assert outcome.detail["simulation"] == "not_run"
    assert outcome.detail["passed"] is None
    assert outcome.campaign_path.is_file()


def test_failed_state_commit_keeps_prior_acceptance_usable(tmp_path, monkeypatch):
    from booley.criteria.state import CriterionEntry, DevelopmentState
    from booley.flows.sim.coverage_acceptance import CoverageAcceptance
    from booley.ticket_board.acceptance_ledger import freeze_acceptance
    from booley.ticket_board.flow_execution import TicketAcceptanceRecorder

    context = project(tmp_path)
    state_path = tmp_path / "state.json"
    state = DevelopmentState.load(state_path)
    state.strict_criteria = True
    state.criteria = {"sim_pass_sim_0": CriterionEntry(met=True, detail={"prior": "valid"})}
    state.save()
    before = state_path.read_bytes()
    prepared = prepare_coverage_invocation(CoverageInvocationRequest(("sim_0",)), context)
    plan = replace(
        prepared.plan.targets[0],
        invocation_dir=tmp_path / "reports/sim/1",
        acceptance=CoverageAcceptance(
            state,
            TicketAcceptanceRecorder(log_dir=tmp_path / "logs"),
        ),
    )
    original = Path.replace

    def fail_state(source, destination):
        if destination == state_path:
            raise OSError("injected state commit failure")
        return original(source, destination)

    # Fault the filesystem boundary, leaving every Booley collaborator real.
    with monkeypatch.context() as patch:
        patch.setattr(Path, "replace", fail_state)
        outcome = run_coverage_target(plan, NativeExecution(verdict="fail"), Progress())
    assert outcome.exit_code == 2
    assert state_path.read_bytes() == before
    saved = DevelopmentState.load(state_path)
    snapshot = freeze_acceptance(
        tmp_path / "logs",
        saved,
        execution_id="prior",
        ticket_identity={},
        participant_heads={"outer": "a" * 40},
    )
    assert snapshot.criteria["sim_pass_sim_0"]["met"] is True
    assert snapshot.evidence == ()


@pytest.mark.parametrize(
    "boundary",
    [
        "point_store",
        "campaign",
        "simulation",
        "first_evidence",
        "second_evidence",
        "state",
        "progress",
    ],
)
def test_persistence_boundaries_preserve_a_trustworthy_acceptance_projection(
    tmp_path, monkeypatch, boundary
):
    import os

    from booley.criteria.state import DevelopmentState
    from booley.flows.sim import coverage_campaign_store
    from booley.flows.sim.coverage_progress import CoverageProgress
    from booley.ticket_board.acceptance_ledger import freeze_acceptance

    plan, state, state_path = gated_ticket_plan(tmp_path)
    prior = run_coverage_target(plan, NativeExecution(), Progress())
    assert prior.exit_code == 0
    invocation = tmp_path / "reports/sim/2"
    plan = replace(plan, invocation_dir=invocation)
    replace_file, link_file, commit_file = persistence_fault(boundary)
    with monkeypatch.context() as patch:
        patch.setattr(Path, "replace", replace_file)
        patch.setattr(os, "link", link_file)
        patch.setattr(coverage_campaign_store, "_commit_new", commit_file)
        outcome = run_coverage_target(
            plan, NativeExecution(verdict="fail", hits=0), CoverageProgress(invocation, ("sim_0",))
        )
    assert outcome.exit_code == 2
    assert outcome.abort_remaining
    saved = DevelopmentState.load(state_path)
    snapshot = freeze_acceptance(
        tmp_path / "logs",
        saved,
        execution_id="test",
        ticket_identity={},
        participant_heads={"outer": "a" * 40},
    )
    committed = boundary == "progress"
    assert {entry["met"] for entry in snapshot.criteria.values()} == {not committed}
    assert len(snapshot.evidence) == (4 if committed else 2)
    assert {entry.met for entry in state.criteria.values()} == {not committed}
    assert outcome.campaign_path.exists() == (boundary not in {"point_store", "campaign"})
    assert outcome.simulation_path.exists() == (
        boundary not in {"point_store", "campaign", "simulation"}
    )
    expected_evaluation = "blocked" if boundary in {"point_store", "campaign"} else "fail"
    assert outcome.detail["evaluation"] == expected_evaluation
    assert outcome.detail["simulation"] == "fail"
    assert outcome.detail["passed"] is False
    assert outcome.detail["collection"] == (
        "infrastructure_error" if boundary in {"point_store", "campaign"} else "complete"
    )


def test_target_transaction_never_resumes_existing_native_state(tmp_path):
    context = project(tmp_path)
    prepared = prepare_coverage_invocation(CoverageInvocationRequest(("sim_0",)), context)
    plan = replace(prepared.plan.targets[0], invocation_dir=tmp_path / "reports/sim/1")
    original = run_coverage_target(plan, NativeExecution(), Progress())
    before = original.campaign_path.read_bytes()
    second = NativeExecution(verdict="fail")
    outcome = run_coverage_target(plan, second, Progress())
    assert outcome.exit_code == 2
    assert second.runs == []
    assert original.campaign_path.read_bytes() == before


def persistence_fault(boundary):
    import os

    from booley.flows.sim import coverage_campaign_store

    original_replace, original_link = Path.replace, os.link
    original_commit = coverage_campaign_store._commit_new
    evidence_count = 0
    filenames = {
        "point_store": "coverage-points.jsonl.gz",
        "campaign": "coverage.json",
        "simulation": "simulation.json",
        "state": "state.json",
        "progress": "progress.json",
    }

    def replace_file(source, destination):
        if Path(destination).name == filenames.get(boundary):
            raise OSError(f"injected {boundary} failure")
        return original_replace(source, destination)

    def link_file(source, destination, **kwargs):
        nonlocal evidence_count
        if Path(destination).name == "record.json":
            evidence_count += 1
            if (boundary == "first_evidence" and evidence_count == 1) or (
                boundary == "second_evidence" and evidence_count == 2
            ):
                raise OSError("injected evidence failure")
        return original_link(source, destination, **kwargs)

    def commit_file(temporary, final):
        if final.name == filenames.get(boundary):
            raise OSError(f"injected {boundary} failure")
        return original_commit(temporary, final)

    return replace_file, link_file, commit_file


def gated_ticket_plan(tmp_path):
    from fractions import Fraction

    from booley.criteria.state import CriterionEntry, DevelopmentState
    from booley.flows.sim.coverage_acceptance import CoverageAcceptance
    from booley.flows.sim.coverage_policy import CoverageCriterion, CoverageThreshold
    from booley.ticket_board.flow_execution import TicketAcceptanceRecorder

    context = project(tmp_path)
    state_path = tmp_path / "state.json"
    state = DevelopmentState.load(state_path)
    state.strict_criteria = True
    state.criteria = {
        key: CriterionEntry(met=True, detail={"prior": "valid"})
        for key in ("coverage_sim_0", "sim_pass_sim_0")
    }
    state.save()
    criterion = CoverageCriterion(
        DurableTargetIdentity("acme:demo:counter:1#sim_0"),
        (CoverageThreshold("line", Fraction(100)),),
        None,
    )
    prepared = prepare_coverage_invocation(
        CoverageInvocationRequest(("sim_0",)),
        replace(context, criteria={"coverage_sim_0": criterion}),
    )
    invocation = tmp_path / "reports/sim/1"
    plan = replace(
        prepared.plan.targets[0],
        invocation_dir=invocation,
        acceptance=CoverageAcceptance(
            state,
            TicketAcceptanceRecorder(log_dir=tmp_path / "logs"),
        ),
    )
    return plan, state, state_path

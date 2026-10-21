import json
import tempfile
from dataclasses import replace
from fractions import Fraction
from pathlib import Path

import pytest

from booley.flows.sim.campaign_retention import _coverage_owned_files
from booley.flows.sim.coverage_campaign import (
    NO_DECLARED_INPUTS_FINGERPRINT,
    DurableTargetIdentity,
    freeze_coverage_mapping,
)
from booley.flows.sim.coverage_campaign_store import (
    load_coverage_campaign,
)
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
    assert document["$schema"] == "booley.coverage-campaign/v5"
    assert document["scoring"] == {"status": "valid", "reason": None}
    assert document["source_rollups"]
    assert "points" not in document
    campaign = load_coverage_campaign(
        outcome.campaign_path, DurableTargetIdentity(plan.handle.identity)
    ).campaign
    assert campaign.evaluation["status"] == "not_requested"
    assert campaign.rollups[0].eligible_points == 1
    assert campaign.points[0].hits_by_run == {"run:001:reset@1": 2, "run:002:wrap@1": 2}
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
        "test_run": "reset@1",
        "variant": None,
        "seed": 1,
        "fingerprint": NO_DECLARED_INPUTS_FINGERPRINT,
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

    result = SimulateFlow()._coverage_result([outcome])
    rendered = result.report_text
    assert result.detail["pre_sim_lines"] == [
        line for line in rendered.splitlines() if line.startswith("pre_run_commands (")
    ]

    assert "pre_run_commands (0 line(s)) for sim_0/reset: rc=unavailable in 0.1s" in rendered
    assert "pre_run_commands (0 line(s)) for sim_0/wrap: rc=unavailable in 0.2s" in rendered


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
    assert document["$schema"] == "booley.coverage-campaign/v5"
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


def _run_with_waivers(
    tmp_path: Path,
    waiver_config,
    *,
    invocation: str,
    gated: bool = False,
    no_waivers: bool = False,
    prepare=None,
):
    """Run ``sim_0`` once under ``reports/sim/<invocation>``; return (context, plan, outcome)."""
    context = prepare() if prepare is not None else project(tmp_path)
    criterion = CoverageCriterion(
        DurableTargetIdentity("acme:demo:counter:1#sim_0"),
        (CoverageThreshold("line", Fraction(100)),),
        None,
    )
    context = replace(
        context,
        criteria={"coverage_sim_0": criterion} if gated else {},
        waiver_config=waiver_config,
    )
    request = CoverageInvocationRequest(("sim_0",), no_waivers=no_waivers, diagnostic=no_waivers)
    prepared = prepare_coverage_invocation(request, context)
    plan = replace(prepared.plan.targets[0], invocation_dir=tmp_path / "reports/sim" / invocation)
    return plan, run_coverage_target(plan, NativeExecution(), Progress())


def test_ungated_run_applies_approved_waivers_and_no_waivers_reports_raw(tmp_path):
    import hashlib

    from booley.flows.sim.coverage_evidence import CoverageEvidenceSession
    from booley.flows.sim.coverage_waivers import CoverageWaiverConfig

    context = project(tmp_path)
    _plan, first = _run_with_waivers(tmp_path, None, invocation="1", prepare=lambda: context)
    assert first.exit_code == 0
    point_id = load_coverage_campaign(first.campaign_path).campaign.points[0].id
    digest = hashlib.sha256((tmp_path / "rtl/counter.sv").read_bytes()).hexdigest()
    waiver_file = tmp_path / "project-data/coverage-waivers/rtl/counter.sv.toml"
    waiver_file.parent.mkdir(parents=True)
    waiver_file.write_text(
        f'''schema = "booley.coverage-waivers/v1"
source = "rtl/counter.sv"
source_sha256 = "sha256:{digest}"

[[approval]]
id = "counter-line"
target = "acme:demo:counter:1#sim_0"
point_id = "{point_id}"
reason = "excluded"
justification = "Reserved behavior is intentionally excluded."
approved_by = "verification-owner@example.test"
approved_at = "2026-08-31T09:00:00Z"
approval_ref = "review:CR-1042"
''',
        encoding="utf-8",
    )
    config = CoverageWaiverConfig("project_data_repository", "coverage-waivers")

    _plan, second = _run_with_waivers(tmp_path, config, invocation="2", prepare=lambda: context)
    assert second.exit_code == 0
    loaded = load_coverage_campaign(second.campaign_path)
    assert loaded.campaign.evaluation["status"] == "not_requested"
    assert loaded.campaign.points[0].disposition["kind"] == "waived"
    rollup = loaded.campaign.rollups[0]
    assert (rollup.eligible_points, rollup.waived_points, rollup.percent) == (0, 1, None)
    session = CoverageEvidenceSession(loaded.campaign, None)
    matched = session.query({"view": "points", "disposition": "waived"})["matched_points"]
    assert matched == 1

    _plan, third = _run_with_waivers(
        tmp_path, config, invocation="3", no_waivers=True, prepare=lambda: context
    )
    assert third.exit_code == 0
    raw = load_coverage_campaign(third.campaign_path).campaign
    assert raw.evaluation["status"] == "not_requested"
    assert raw.points[0].disposition["kind"] == "eligible"
    assert (raw.rollups[0].eligible_points, raw.rollups[0].waived_points) == (1, 0)


@pytest.mark.parametrize("invalid", ["unsafe_directory", "malformed_file"])
def test_invalid_waivers_block_every_coverage_run(tmp_path, invalid):
    from booley.flows.sim.coverage_waivers import CoverageWaiverConfig
    from booley.flows.sim.request import SimRequest

    results = {}
    for gated in (False, True):
        root = tmp_path / f"gated-{gated}"
        root.mkdir()
        context = project(root)
        if invalid == "unsafe_directory":
            config = CoverageWaiverConfig("rtl_repository", "../unsafe")
        else:
            bad = root / "project-data/coverage-waivers/rtl/counter.sv.toml"
            bad.parent.mkdir(parents=True)
            bad.write_text("this is = not [valid toml\n", encoding="utf-8")
            config = CoverageWaiverConfig("project_data_repository", "coverage-waivers")
        _plan, outcome = _run_with_waivers(
            root, config, invocation="1", gated=gated, prepare=lambda context=context: context
        )
        assert outcome.exit_code == 2
        document = json.loads(outcome.campaign_path.read_text())
        assert document["evaluation"]["status"] == "blocked"
        assert document["collection"]["status"] == "complete"
        flow = SimulateFlow()
        flow.context._args = SimRequest(work_dir=root)
        assert "--no-waivers" in flow._coverage_result([outcome]).report_text
        results[gated] = [
            (item["code"], item["pointer"], item["message"])
            for item in document["evaluation"]["diagnostics"]
        ]
    assert results[False] and results[False] == results[True]
    if invalid == "unsafe_directory":
        assert results[False][0][0] == "COV_WAIVER_DIRECTORY_UNSAFE"


def test_no_waivers_diagnostic_criterion_skips_invalid_waivers(tmp_path):
    from fractions import Fraction

    from booley.flows.sim.coverage_policy import CoverageCriterion, CoverageThreshold
    from booley.flows.sim.coverage_waivers import CoverageWaiverConfig, load_approved_waiver_set

    context = project(tmp_path)
    criterion = CoverageCriterion(
        DurableTargetIdentity("acme:demo:counter:1#sim_0"),
        (CoverageThreshold("line", Fraction(100)),),
        None,
    )
    context = replace(
        context,
        criteria={"coverage_sim_0": criterion},
        waiver_config=CoverageWaiverConfig("rtl_repository", "../unsafe"),
    )
    prepared = prepare_coverage_invocation(
        CoverageInvocationRequest(("sim_0",), no_waivers=True, diagnostic=True), context
    )
    plan = replace(prepared.plan.targets[0], invocation_dir=tmp_path / "reports/sim/1")
    outcome = run_coverage_target(plan, NativeExecution(), Progress())
    evaluation = json.loads(outcome.campaign_path.read_text())["evaluation"]
    assert evaluation["status"] != "blocked"
    assert evaluation["approved_waiver_set_digest"] == (
        load_approved_waiver_set(None, tmp_path, ()).digest
    )


def test_ticket_publishes_campaign_before_independent_acceptance_evidence(tmp_path):
    from fractions import Fraction

    from booley.criteria.state import CriterionEntry, DevelopmentState
    from booley.flows.sim.coverage_acceptance import CoverageAcceptance
    from booley.flows.sim.coverage_policy import CoverageCriterion, CoverageThreshold
    from tests.criteria.ledger_support import FixtureRecorder

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
            FixtureRecorder(log_dir=tmp_path / "logs"),
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
    from tests.criteria.ledger_support import FixtureRecorder

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
            FixtureRecorder(log_dir=tmp_path / "logs"),
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

    original_replace, original_link = Path.replace, os.link
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
        # The point store and manifest are published by linking them into place.
        if Path(destination).name == filenames.get(boundary):
            raise OSError(f"injected {boundary} failure")
        if Path(destination).name == "record.json":
            evidence_count += 1
            if (boundary == "first_evidence" and evidence_count == 1) or (
                boundary == "second_evidence" and evidence_count == 2
            ):
                raise OSError("injected evidence failure")
        return original_link(source, destination, **kwargs)

    return replace_file, link_file


def gated_ticket_plan(tmp_path):
    from fractions import Fraction

    from booley.criteria.state import CriterionEntry, DevelopmentState
    from booley.flows.sim.coverage_acceptance import CoverageAcceptance
    from booley.flows.sim.coverage_policy import CoverageCriterion, CoverageThreshold
    from tests.criteria.ledger_support import FixtureRecorder

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
            FixtureRecorder(log_dir=tmp_path / "logs"),
        ),
    )
    return plan, state, state_path


def gap_plan(tmp_path, sources=None, *, includes=(), testbench=None, criterion=False):
    """A one-Target project whose rtl fileset adds ``sources`` (path to text) to counter.sv.

    ``includes`` names rtl paths flagged ``is_include_file``; ``testbench`` maps paths into a
    ``tb``-tagged fileset; ``criterion`` binds a 100% line Criterion to the Target.
    """
    sources = {"rtl/unused.sv": "module unused; endmodule\n"} if sources is None else sources
    testbench = testbench or {}
    tmp_path.mkdir(exist_ok=True)
    context = project(tmp_path)
    (tmp_path / "tb").mkdir()
    for path, text in {**sources, **testbench}.items():
        (tmp_path / path).write_text(text)

    def entries(paths, flag=""):
        return "".join(
            f"      - {p}: {{file_type: systemVerilogSource{flag if p in includes else ''}}}\n"
            for p in paths
        )

    tb_fileset = f"  tb:\n    files:\n{entries(testbench)}    tags: [tb]\n" if testbench else ""
    (tmp_path / "counter.core").write_text(
        "CAPI=2:\nname: acme:demo:counter:1\nfilesets:\n  rtl:\n    files:\n"
        f"{entries(['rtl/counter.sv', *sources], ', is_include_file: true')}"
        f"{tb_fileset}targets:\n  sim_0:\n    flow: sim\n    default_tool: verilator\n"
        "    flow_options: {tool: verilator}\n"
        f"    filesets: [rtl{', tb' if testbench else ''}]\n    toplevel: counter\n"
    )
    if criterion:
        key = "coverage_sim_0"
        context = replace(
            context,
            criteria={
                key: CoverageCriterion(
                    DurableTargetIdentity("acme:demo:counter:1#sim_0"),
                    (CoverageThreshold("line", Fraction(100)),),
                    None,
                )
            },
        )
    prepared = prepare_coverage_invocation(CoverageInvocationRequest(("sim_0",)), context)
    return replace(prepared.plan.targets[0], invocation_dir=tmp_path / "reports/sim/1")


def test_direct_publication_preserves_source_gaps_and_card_preview(tmp_path):
    from booley.flows.sim import coverage_source_gaps as gaps
    from booley.flows.sim.coverage_evidence import CoverageEvidenceSession
    from booley.flows.sim.request import SimRequest

    plan = gap_plan(tmp_path / "gap", criterion=True)
    outcome = run_coverage_target(plan, NativeExecution(), Progress())
    loaded = load_coverage_campaign(outcome.campaign_path)
    assert gaps.source_gap_paths(loaded.campaign) == ("rtl/unused.sv",)
    session = CoverageEvidenceSession(loaded.campaign, None)
    assert session.query({"view": "zero_point_sources"})["paths"] == ["rtl/unused.sv"]
    flow = SimulateFlow()
    flow.context._args = SimRequest(work_dir=tmp_path / "gap")
    card = flow._coverage_result([outcome]).report_text
    assert "RTL sources without coverage points: 1" in card and "rtl/unused.sv" in card
    # The finding is advisory: exit code, Criterion and verdict match a project without the gap.
    clean = gap_plan(tmp_path / "clean", {}, criterion=True)
    baseline = run_coverage_target(clean, NativeExecution(), Progress())
    other = load_coverage_campaign(baseline.campaign_path).campaign
    assert not gaps.source_gap_paths(other)
    assert (outcome.exit_code, loaded.campaign.evaluation, loaded.campaign.rollups) == (
        baseline.exit_code,
        other.evaluation,
        other.rollups,
    )
    assert loaded.campaign.evaluation["status"] == "pass"
    assert [r.simulation_verdict for r in loaded.campaign.runs] == [
        r.simulation_verdict for r in other.runs
    ]


def test_incomplete_collection_never_accuses_module_sources(tmp_path):
    from booley.flows.sim.coverage_source_gaps import GAP_CODE

    execution = NativeExecution()
    execution.missing = True
    outcome = run_coverage_target(gap_plan(tmp_path), execution, Progress())
    campaign = load_coverage_campaign(outcome.campaign_path).campaign
    assert outcome.exit_code == 2
    assert not any(f.code == GAP_CODE for f in campaign.findings)


def test_v030_campaign_with_declaration_evidence_loads_without_gap_claim(tmp_path):
    from booley.flows.sim.coverage_evidence import CoverageEvidenceSession
    from booley.flows.sim.coverage_provenance import content_digest
    from booley.flows.sim.coverage_source_gaps import source_gap_overview, source_gap_report_lines
    from booley.flows.sim.request import SimRequest
    from tests.flows.sim.coverage_campaign_test_support import rewrite_manifest_as_v4

    outcome = run_coverage_target(gap_plan(tmp_path, {}), NativeExecution(), Progress())
    root = outcome.campaign_path.parent
    manifest = json.loads(outcome.campaign_path.read_text())
    rewrite_manifest_as_v4(manifest)
    manifest["artifacts"] = [
        a for a in manifest["artifacts"] if not a["kind"].startswith("declaration_")
    ]
    for kind, name in (
        ("declaration_inventory", "declarations/inventory.json"),
        ("declaration_raw", "build-evidence/cells.tree.json"),
    ):
        (root / name).parent.mkdir(exist_ok=True)
        (root / name).write_text("{}")
        manifest["artifacts"].append(
            {
                "id": f"artifact:{kind}",
                "kind": kind,
                "path": name,
                "sha256": content_digest(b"{}"),
                "bytes": 2,
                "state": "fresh_queryable",
                "contract_version": 1,
                "discovery_status": "incomplete",
            }
        )
    manifest["findings"].append(
        {
            "severity": "warning",
            "code": "COV_RTL_SOURCE_DISCOVERY_INCOMPLETE",
            "pointer": "/artifacts",
            "message": "RTL source discovery is incomplete.",
        }
    )
    outcome.campaign_path.write_text(json.dumps(manifest))
    campaign = load_coverage_campaign(outcome.campaign_path).campaign
    assert {a.kind for a in campaign.artifacts} >= {"declaration_inventory", "declaration_raw"}
    assert any(f.code == "COV_RTL_SOURCE_DISCOVERY_INCOMPLETE" for f in campaign.findings)
    assert source_gap_report_lines(campaign) == []
    assert source_gap_overview(campaign)["total_sources"] == 0
    assert (
        CoverageEvidenceSession(campaign, None).query({"view": "zero_point_sources"})["paths"]
        == []
    )
    flow = SimulateFlow()
    flow.context._args = SimRequest(work_dir=tmp_path)
    assert "RTL source" not in flow._coverage_result([outcome]).report_text


def test_coverage_ledger_failure_retains_evaluated_final_report(tmp_path):
    from booley.flows.sim.coverage_acceptance import CoverageAcceptance
    from booley.runtime.endpoint_execution import EndpointOutcome
    from tests.mcp_tools.test_base import ConcreteMcpTool

    plan, state, state_path = gated_ticket_plan(tmp_path)
    del state.criteria["sim_pass_sim_0"]
    state.save()
    before = state_path.read_bytes()
    tool = ConcreteMcpTool()
    tool.endpoint_kind = "specialist"
    tool.parse_args(["--work-dir", str(tmp_path), "--report-dir", str(tmp_path / "reports")])
    tool._state = state

    from tests.criteria.ledger_support import FixtureRecorder

    class FailedRecorder(FixtureRecorder):
        def record_changes(self, *_args, **_kwargs):
            raise OSError("coverage ledger unavailable")

    recorder = FailedRecorder(identity={"generation": "d" * 32})
    tool._acceptance_recorder = recorder
    plan = replace(
        plan, acceptance=CoverageAcceptance(state, recorder, tool.record_report_criteria)
    )
    outcome = run_coverage_target(plan, NativeExecution(), Progress())
    assert outcome.exit_code == 2
    from booley.flows.endpoint_report_criteria import freeze

    freeze(tool)
    report = json.loads(tool.write_report(EndpointOutcome(exit_code=2)).read_text())
    assert (report["criterion_key"], report["criterion_met"]) == ("coverage_sim_0", True)
    assert report["passed"] is False
    assert state_path.read_bytes() == before


def test_failed_state_commit_keeps_prior_acceptance_usable(tmp_path, monkeypatch):
    from booley.criteria.state import CriterionEntry, DevelopmentState
    from booley.flows.sim.coverage_acceptance import CoverageAcceptance
    from booley.mcp.unbound_recorder import UnboundAcceptanceRecorder

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
            UnboundAcceptanceRecorder(log_dir=tmp_path / "logs"),
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
    assert saved.criteria["sim_pass_sim_0"].met
    assert saved.criteria["sim_pass_sim_0"].detail == {"prior": "valid"}
    assert saved.acceptance_transactions == []


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
    from booley.flows.sim.coverage_progress import CoverageProgress

    plan, state, state_path = gated_ticket_plan(tmp_path)
    prior = run_coverage_target(plan, NativeExecution(), Progress())
    assert prior.exit_code == 0
    invocation = tmp_path / "reports/sim/2"
    plan = replace(plan, invocation_dir=invocation)
    replace_file, link_file = persistence_fault(boundary)
    with monkeypatch.context() as patch:
        patch.setattr(Path, "replace", replace_file)
        patch.setattr(os, "link", link_file)
        outcome = run_coverage_target(
            plan, NativeExecution(verdict="fail", hits=0), CoverageProgress(invocation, ("sim_0",))
        )
    assert outcome.exit_code == 2
    assert outcome.abort_remaining
    saved = DevelopmentState.load(state_path)
    committed = boundary == "progress"
    assert {entry.met for entry in saved.criteria.values()} == {not committed}
    from booley.criteria import evidence_ledger
    from tests.criteria.ledger_support import FIXTURE_SCOPE

    records = evidence_ledger.read_evidence_records(FIXTURE_SCOPE, tmp_path / "logs", saved, {})
    assert len(records) == (4 if committed else 2)
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


def test_direct_coverage_outcome_includes_metrics(tmp_path):
    from booley.flows.sim.coverage_waivers import CoverageWaiverConfig

    context = project(tmp_path)
    for number, (gated, invalid) in enumerate(((False, False), (True, False), (True, True)), 1):
        plan, outcome = _run_with_waivers(
            tmp_path,
            CoverageWaiverConfig("rtl_repository", "../unsafe") if invalid else None,
            invocation=str(number),
            gated=gated,
            prepare=lambda: context,
        )
        assert "coverage_metrics" in outcome.detail
        floor, met = (100, True) if gated else (None, None)
        expected = {"line": {"percent": 100.0, "threshold": floor, "met": met}}
        assert outcome.detail["coverage_metrics"] == expected
        document = json.loads(outcome.campaign_path.read_text())
        assert document["evaluation"]["thresholds"] == ({"line": 100} if gated else {})
        assert outcome.detail["evaluation"] == (
            "blocked" if invalid else "pass" if gated else "not_requested"
        )
        assert outcome.exit_code == (2 if invalid else 0)
        if invalid:
            assert document["evaluation"]["metrics"] == []
        retained_plan = replace(plan, invocation_dir=tmp_path / "reports/sim" / str(number + 3))

        def checkpoint(boundary):
            if boundary == "after:coverage_campaign":
                raise OSError("publication interrupted")

        retained = run_coverage_target(
            retained_plan, NativeExecution(), Progress(), publication_checkpoint=checkpoint
        )
        assert retained.exit_code == 2 and retained.detail["coverage_metrics"] == expected
    from booley.flows.sim.coverage_report import coverage_metrics

    campaign = load_coverage_campaign(outcome.campaign_path).campaign
    for covered, eligible, percent, met in ((2, 3, 66.67, False), (0, 0, None, False)):
        rollup = replace(
            campaign.rollups[0], covered_points=covered, eligible_points=eligible, percent=percent
        )
        edge = replace(
            campaign,
            rollups=(rollup,),
            evaluation=freeze_coverage_mapping({"thresholds": {"line": 66.67}}),
        )
        assert coverage_metrics(edge) == {
            "line": {"percent": percent, "threshold": 66.67, "met": met}
        }

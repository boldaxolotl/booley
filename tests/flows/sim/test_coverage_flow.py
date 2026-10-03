import json
import os
import re
import shutil
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

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
from booley.flows.sim.execution.contract import SimulationOptions
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


class _PassingSplitMetricsExecution(_SplitMetricsExecution):
    def payload(self, hits):
        return super().payload(hits).replace("\x01o\x02true' 0", "\x01o\x02true' 1")


class _CustomMainHookExecution(NativeExecution):
    def __init__(self, events: list[dict[str, object]], *, missing_raw: bool = False) -> None:
        super().__init__(missing=missing_raw)
        self.events = events

    def run(self, request):
        result = super().run(request)
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
        return result


def _configure_custom_main_coverage(root: Path) -> None:
    (root / "main.cpp").write_text("int main() { return 0; }\n")
    core = root / "counter.core"
    core.write_text(
        core.read_text()
        .replace(
            "files: [rtl/counter.sv]",
            "files: [rtl/counter.sv, {main.cpp: {file_type: cppSource}}]",
        )
        .replace(
            "flow_options: {tool: verilator}",
            "flow_options: {tool: verilator, booley: {coverage: "
            "{custom_main_hooks: [start_hook, write_hook], reset_included: false}}}",
        )
    )


def _prepare_console_scope_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from booley.criteria.state import CriterionEntry

    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    project(tmp_path, ("verilator", "verilator"))
    project_data = tmp_path / ".booley_project"
    project_data.mkdir()
    (project_data / "tests.toml").write_text(
        '[sim_0]\ntests = ["half", "reset", "wrap", "carry", "zero"]\n'
        '[sim_1]\ntests = ["gap", "overflow", "underflow", "saturate"]\n'
    )
    state_path = tmp_path / "state.json"
    state = DevelopmentState.load(state_path)
    state.criteria = {
        "coverage_sim_0": CriterionEntry(
            params={
                "target": "sim_0",
                "tests": ["half"],
                "metrics": {"line": {"min_pct": 1}},
            }
        ),
        "coverage_sim_1": CriterionEntry(
            params={
                "target": "sim_1",
                "tests": ["gap"],
                "metrics": {"line": {"min_pct": 1}},
            }
        ),
    }
    state.save()
    monkeypatch.setenv("BOOLEY_STATE_FILE", str(state_path))
    monkeypatch.setenv("BOOLEY_LOGS_DIR", str(tmp_path / "logs"))
    return state_path


def test_console_lifecycle_reports_executed_suite_not_criterion_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prepare_console_scope_project(tmp_path, monkeypatch)
    events: list[dict[str, object]] = []
    monkeypatch.setattr("booley.flows.endpoint_session._write_display_event", events.append)
    monkeypatch.setattr("booley.flows.endpoint_reporting._write_display_event", events.append)

    result = SimulateFlow(
        coverage_execution=lambda _handle, _options, _commands, _access: NativeExecution()
    ).execute(
        SimRequest(
            target="sim_0,sim_1",
            work_dir=tmp_path,
            coverage=True,
            report_dir=tmp_path / "reports",
        )
    )

    lifecycle = [event for event in events if event["type"] in {"endpoint_start", "endpoint_end"}]
    assert result.exit_code == 2, result.outcome
    assert [event["display_label"] for event in lifecycle] == [
        "2 targets · 9 tests",
        "2 targets · 9 tests",
    ]
    assert {
        observation["test"]
        for campaign in result.outcome.detail["campaigns"].values()
        for observation in campaign["observations"]
    } == {"half", "reset", "wrap", "carry", "zero", "gap", "overflow", "underflow", "saturate"}
    for campaign in result.outcome.detail["campaigns"].values():
        path = tmp_path / "reports/sim/1" / campaign["artifacts"]["coverage"]["path"]
        evaluation = resolve_coverage_campaign_reference(path).loaded.campaign.evaluation
        assert evaluation["status"] == "blocked"
        assert evaluation["suite"]["status"] == "mismatch"


def test_pre_outcome_coverage_failure_keeps_prepared_console_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prepare_console_scope_project(tmp_path, monkeypatch)
    events: list[dict[str, object]] = []
    monkeypatch.setattr("booley.flows.endpoint_session._write_display_event", events.append)
    monkeypatch.setattr("booley.flows.endpoint_reporting._write_display_event", events.append)

    def fail_before_outcome(_campaign, _request):
        raise RuntimeError("failed before nested outcome")

    monkeypatch.setattr(SimulationCampaign, "run", fail_before_outcome)
    result = SimulateFlow().execute(
        SimRequest(
            target="sim_0,sim_1",
            work_dir=tmp_path,
            coverage=True,
            report_dir=tmp_path / "reports",
        )
    )

    lifecycle = [event for event in events if event["type"] in {"endpoint_start", "endpoint_end"}]
    assert result.exit_code == 2
    assert [event["display_label"] for event in lifecycle] == [
        "2 targets · 9 tests",
        "2 targets · 9 tests",
    ]


@pytest.mark.parametrize(
    ("observations", "expected"),
    [
        (({"test": "half"}, {"test": ""}), "2 targets · tests"),
        (({"test": "half"},), "2 targets · test half"),
    ],
)
def test_campaign_completion_label_requires_complete_test_identity(
    tmp_path: Path,
    observations: tuple[dict[str, object], ...],
    expected: str,
) -> None:
    flow = SimulateFlow()
    flow.parse_args(["--work-dir", str(tmp_path), "--target", "sim_0,sim_1", "--coverage"])

    label = flow._campaign_completed_display_label(
        [SimpleNamespace(observations=observations)]  # type: ignore[list-item]
    )

    assert label == expected


def _target_headline(report_text: str, selector: str) -> str:
    return next(line for line in report_text.splitlines() if line.startswith(f"{selector}:"))


def _assert_ungated_report(result, tmp_path: Path) -> None:
    campaign_id = result.outcome.detail["campaigns"]["sim_0"]["campaign_id"]
    expected_headline = (
        "sim_0: simulation PASS · coverage collection COMPLETE · "
        f"evaluation NOT_REQUESTED (Simulation Campaign {campaign_id})"
    )
    assert _target_headline(result.outcome.report_text, "sim_0") == expected_headline
    persisted = json.loads((tmp_path / "reports/sim/1/report.json").read_text())
    assert persisted["report_text"] == result.outcome.report_text


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
    assert report["$schema"] == "booley.simulation-report/v3"
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
    flow = SimulateFlow(
        coverage_execution=lambda handle, options, _commands, _access: NativeExecution()
    )
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
    _assert_ungated_report(result, tmp_path)
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
    assert flow_schema(flow)["properties"]["no_waivers"]["type"] == "boolean"


def test_coverage_simulation_projection_error_tail_is_text(tmp_path, monkeypatch):
    monkeypatch.setenv("BOOLEY_CONTAINER", "1")

    for verdict, exit_code in (("pass", 0), ("fail", 1)):
        root = tmp_path / verdict
        root.mkdir()
        project(root)
        data = root / ".booley_project"
        data.mkdir()
        (data / "tests.toml").write_text('[sim_0]\ntests = ["reset"]\n')

        result = SimulateFlow(
            coverage_execution=lambda *_args, verdict=verdict: NativeExecution(verdict=verdict)
        ).execute(
            SimRequest(
                target="sim_0",
                work_dir=root,
                coverage=True,
                report_dir=root / "reports",
            )
        )

        assert result.exit_code == exit_code
        projection = json.loads((root / "reports/sim/1/targets/sim_0/simulation.json").read_text())
        error_tail = projection["tests"][0]["error_tail"]
        if verdict == "pass":
            assert error_tail == ""
        else:
            assert error_tail == "coverage simulation fail"
            assert "{" not in error_tail


def _frozen_hook_execution(tmp_path, data, received):
    class PreSimPassingExecution(NativeExecution):
        def run(self, request):
            result = super().run(request)
            from booley.flows.sim.execution.pre_sim import run_pre_sim_commands

            evidence = run_pre_sim_commands(
                SimpleNamespace(project_root=tmp_path, selector="sim_0"),
                test_names=(request.test.name,),
                build_root=tmp_path / "build",
                eda_tool="verilator",
                timeout_s=5,
                commands=received[-1][1],
            )
            return SimulationRunResult(result.verdict, result.output, evidence)

    def execution_factory(_handle, options, commands, access):
        received.append((options, commands, access))
        (data / "booley.toml").write_text(
            '[flows.sim]\npre_run_commands = ["echo live-hook; exit 7"]\n'
        )
        return PreSimPassingExecution()

    return execution_factory


def test_injected_coverage_execution_receives_frozen_pre_sim_policy(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    project(tmp_path)
    data = tmp_path / ".booley_project"
    data.mkdir()
    (data / "tests.toml").write_text('[sim_0]\ntests = ["reset"]\n')
    (data / "booley.toml").write_text(
        "[flows.sim]\n"
        "build_timeout_ms = 7000\n"
        "timeout_ms = 5000\n"
        'pre_run_commands = ["echo frozen-hook"]\n'
        'pre_sim_build_access = "legacy-per-test"\n'
    )
    received = []

    execution_factory = _frozen_hook_execution(tmp_path, data, received)

    result = SimulateFlow(coverage_execution=execution_factory).execute(
        SimRequest(
            target="sim_0",
            work_dir=tmp_path,
            coverage=True,
            report_dir=tmp_path / "reports",
        )
    )

    assert result.exit_code == 0
    assert received == [
        (
            SimulationOptions(timeout_ms=5000, build_timeout_ms=7000),
            ("echo frozen-hook",),
            "legacy-per-test",
        )
    ]
    assert "pre_run_commands (1 line(s)) for sim_0/reset: rc=0 in " in result.outcome.report_text
    from booley.flows.sim.campaign import read_pre_sim_firings

    firings = read_pre_sim_firings(tmp_path / "reports/sim/1/targets/sim_0/campaign/manifest.json")
    assert len(firings) == 1
    assert firings[0].document["stdout_tail"] == "frozen-hook\n"


def _publish_real_hook_series(request, handle, commands, count):
    from booley.flows.sim.campaign.pre_sim_evidence import publish_pre_sim_firing
    from booley.flows.sim.execution.pre_sim import run_pre_sim_commands

    for ordinal in range(1, count + 1):
        evidence = run_pre_sim_commands(
            handle,
            test_names=("alpha",),
            build_root=request.attempt_directory / "build",
            eda_tool="icarus",
            timeout_s=5,
            commands=commands,
            run_cwd=".",
        )
        assert evidence is not None
        publish_pre_sim_firing(request, evidence, ordinal=ordinal)


def test_many_current_coverage_firings_keep_all_lines_and_bounded_preview(tmp_path, monkeypatch):
    import json

    from booley.flows.sim.campaign import read_pre_sim_firings
    from booley.flows.sim.campaign.store import CampaignStore
    from booley.runtime.endpoint_execution import EndpointOutcome
    from tests.flows.sim.test_campaign_phase3_integrity import _executor, _handle, _request
    from tests.flows.sim.test_endpoint_campaign_lifecycle import _pre_sim_plan

    def transform(document):
        document["workload"]["coverage"] = True
        document["work_items"][0]["kind"] = "coverage_aggregate"

    commands = ("echo hook-out; echo hook-err >&2",)
    plan, _disclosures = _pre_sim_plan("immutable", commands, transform)
    invocation = tmp_path / "reports/1"
    store = CampaignStore(invocation / "targets/sim/campaign")
    store.publish_manifest(plan.manifest)
    handle = _handle(tmp_path)
    request = _request(
        store, plan.manifest, plan.manifest.document["work_items"][0], tmp_path, invocation=1
    )
    flow = SimulateFlow()
    flow._current_published_pre_sim_keys = set()
    request = replace(request, pre_sim_firing_published=flow._record_pre_sim_firing)
    _executor(tmp_path / "build", {}).prepare_attempt(request)
    _publish_real_hook_series(request, handle, commands, 40)
    flow.context._reserved_invocation_dir = invocation
    reads = _observe_pre_sim_projection(monkeypatch)
    result = flow._attach_published_pre_sim(
        EndpointOutcome(exit_code=2, report_text="interrupted coverage")
    )
    assert result.detail["pre_sim_total"] == result.detail["pre_sim_current"] == 40
    assert (
        len(result.detail["pre_sim_lines"]) == result.report_text.count("pre_run_commands") == 40
    )
    assert len(result.detail["pre_sim_runs"]) <= 32
    assert len(json.dumps(result.detail["pre_sim_runs"]).encode()) <= 16 * 1024
    assert result.detail["pre_sim_preview_truncated"] is True
    assert reads["manifest"] == 1
    assert reads["decoder_manifest"] == 1
    assert reads["preview"] <= 32
    firings = read_pre_sim_firings(store.manifest_path)
    assert len(firings) == 40
    assert all(
        firing.document["stdout_tail"] == "hook-out\n"
        and firing.document["stderr_tail"] == "hook-err\n"
        for firing in firings
    )


@pytest.mark.parametrize(
    ("coverage_recipe", "message"),
    [
        (
            '{reset_included: "false"}',
            "targets.sim_0.flow_options.booley.coverage.reset_included must be a boolean",
        ),
        (
            "{bogus_key: 1}",
            "targets.sim_0.flow_options.booley.coverage.bogus_key is not a supported coverage key",
        ),
        (
            "{custom_main_hooks: write_hook}",
            "targets.sim_0.flow_options.booley.coverage.custom_main_hooks must be an array",
        ),
    ],
)
def test_coverage_recipe_schema_errors_are_atomic_preflight_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    coverage_recipe: str,
    message: str,
) -> None:
    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    project(tmp_path)
    core = tmp_path / "counter.core"
    core.write_text(
        core.read_text().replace(
            "flow_options: {tool: verilator}",
            f"flow_options: {{tool: verilator, booley: {{coverage: {coverage_recipe}}}}}",
        )
    )
    project_data = tmp_path / ".booley_project"
    project_data.mkdir()
    (project_data / "tests.toml").write_text('[sim_0]\ntests = ["reset"]\n')
    before = {path.relative_to(tmp_path) for path in tmp_path.rglob("*")}

    result = SimulateFlow(
        coverage_execution=lambda _handle, _options, _commands, _access: NativeExecution()
    ).execute(
        SimRequest(
            target="sim_0",
            work_dir=tmp_path,
            coverage=True,
            report_dir=tmp_path / "reports",
        )
    )

    assert result.exit_code == 2
    assert message in result.outcome.report_text
    assert result.outcome.detail["findings"][0]["code"] == "COV_TARGET_INVALID"
    assert {path.relative_to(tmp_path) for path in tmp_path.rglob("*")} == before


def test_valid_custom_main_coverage_recipe_passes_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    project(tmp_path)
    (tmp_path / "main.cpp").write_text("int main() { return 0; }\n")
    core = tmp_path / "counter.core"
    core.write_text(
        core.read_text()
        .replace(
            "files: [rtl/counter.sv]",
            "files: [rtl/counter.sv, {main.cpp: {file_type: cppSource}}]",
        )
        .replace(
            "flow_options: {tool: verilator}",
            "flow_options: {tool: verilator, booley: {coverage: "
            "{custom_main_hooks: [start_hook, write_hook], reset_included: false}}}",
        )
    )
    project_data = tmp_path / ".booley_project"
    project_data.mkdir()
    (project_data / "tests.toml").write_text('[sim_0]\ntests = ["reset"]\n')

    result = SimulateFlow(
        coverage_execution=lambda _handle, _options, _commands, _access: NativeExecution()
    ).execute(
        SimRequest(
            target="sim_0",
            work_dir=tmp_path,
            coverage=True,
            report_dir=tmp_path / "reports",
        )
    )

    assert result.exit_code == 2
    assert "COV_WINDOW_HOOK_MISSING" in result.outcome.report_text
    assert (tmp_path / "reports/sim/1").is_dir()


@pytest.mark.parametrize(
    ("events", "missing_raw", "expected_code"),
    [
        (
            [
                {"hook": "start", "sequence": 1, "success": True},
                {"hook": "write", "sequence": 2, "success": False},
            ],
            True,
            "COV_WRITE_HOOK_FAILED",
        ),
        (
            [
                {"hook": "write", "sequence": 1, "success": True},
                {"hook": "start", "sequence": 2, "success": True},
                {"hook": "write", "sequence": 3, "success": True},
            ],
            False,
            "COV_CUSTOM_MAIN_HOOK_OUT_OF_ORDER",
        ),
    ],
)
def test_custom_main_hook_failures_are_public_collector_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    events: list[dict[str, object]],
    missing_raw: bool,
    expected_code: str,
) -> None:
    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    project(tmp_path)
    _configure_custom_main_coverage(tmp_path)
    project_data = tmp_path / ".booley_project"
    project_data.mkdir()
    (project_data / "tests.toml").write_text('[sim_0]\ntests = ["reset"]\n')

    result = SimulateFlow(
        coverage_execution=lambda _handle, _options, _commands, _access: _CustomMainHookExecution(
            events, missing_raw=missing_raw
        )
    ).execute(
        SimRequest(
            target="sim_0",
            work_dir=tmp_path,
            coverage=True,
            report_dir=tmp_path / "reports",
        )
    )

    assert result.exit_code == 2
    target = result.outcome.detail["targets"]["sim_0"]
    assert target["collection"] == "collector_error"
    assert expected_code in result.outcome.report_text


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
            coverage_execution=lambda _handle, _options, _commands, _access: NativeExecution()
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
    result = SimulateFlow(
        coverage_execution=lambda handle, options, _commands, _access: NativeExecution()
    ).execute(
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
        coverage_execution=lambda handle, options, _commands, _access: NativeExecution(
            missing=handle.name == "sim_1"
        )
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
    campaign_id = result.outcome.detail["campaigns"]["sim_1"]["campaign_id"]
    campaign_path = next(
        (tmp_path / "reports/sim/1/targets/sim_1").rglob("coverage-campaign/coverage.json")
    )
    invalid_manifest = json.loads(campaign_path.read_text())
    assert invalid_manifest["scoring"] == {
        "status": "invalid",
        "reason": "collector_error",
    }
    assert invalid_manifest["rollups"] == []
    headline = _target_headline(result.outcome.report_text, "sim_1")
    assert headline == (
        "sim_1: simulation PASS · coverage collection COLLECTOR_ERROR "
        "(COV_RAW_FILE_MISSING) · evaluation NOT_REQUESTED "
        f"(Simulation Campaign {campaign_id})"
    )
    assert f"sim_1: PASS (Simulation Campaign {campaign_id})" not in result.outcome.report_text


def test_shared_execution_failure_terminalizes_without_losing_completed_reports(
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
        coverage_execution=lambda handle, options, _commands, _access: (
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
    assert result.outcome.detail["targets"]["sim_1"]["abort_remaining"] is False
    outer = result.outcome.detail["targets"]["sim_1"]["evaluation"]
    target_root = tmp_path / "reports/sim/1/targets/sim_1"
    nested_path = next(
        target_root.glob("campaign/work-items/*/attempts/*/coverage-campaign/coverage.json")
    )
    nested = load_coverage_campaign(nested_path).campaign
    assert outer == nested.evaluation["status"] == "not_requested"
    assert set(result.outcome.detail["campaigns"]) == {"sim_0", "sim_1", "sim_2"}
    assert result.outcome.detail["campaigns"]["sim_1"]["grade"] == "error"
    assert result.outcome.detail["campaigns"]["sim_2"]["grade"] == "pass"
    assert result.outcome.detail["pending_targets"] == []
    assert "completion_error" not in result.outcome.detail
    assert list((target_root / "campaign").glob("work-items/*/result.json"))


def test_gated_shared_execution_failure_preserves_blocked_evaluation(tmp_path, monkeypatch):
    _state_path, _before = _prepare_atomic_coverage_ticket(tmp_path, monkeypatch)

    class Unavailable(NativeExecution):
        def build(self, request):
            raise FileNotFoundError("Sandbox executable disappeared")

    result = SimulateFlow(
        coverage_execution=lambda _handle, _options, _commands, _access: Unavailable()
    ).execute(
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
    assert detail["passed"] is False
    assert detail["simulation"] == "aborted"
    assert detail["collection"] == "collector_error"
    assert detail["evaluation"] == nested.evaluation["status"] == "blocked"
    assert "completion_error" not in result.outcome.detail
    assert result.outcome.detail["pending_targets"] == []


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


def test_nested_coverage_publication_failure_preserves_simulation_truth(tmp_path, monkeypatch):
    _prepare_atomic_coverage_ticket(tmp_path, monkeypatch)

    def checkpoint(boundary):
        if boundary == "before:coverage_campaign":
            raise OSError("injected nested Coverage Campaign publication failure")

    result = SimulateFlow(
        coverage_execution=lambda _handle, _options, _commands, _access: NativeExecution(),
        campaign_publication_checkpoint=checkpoint,
    ).execute(
        SimRequest(
            target="sim_custom",
            work_dir=tmp_path,
            coverage=True,
            report_dir=tmp_path / "reports",
        )
    )

    assert result.exit_code == 2
    detail = result.outcome.detail["targets"]["sim_custom"]
    assert detail["passed"] is True
    assert detail["simulation"] == "pass"
    assert detail["collection"] == "infrastructure_error"
    assert detail["evaluation"] == "blocked"


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
    result = SimulateFlow(
        coverage_execution=lambda _handle, _options, _commands, _access: execution
    ).execute(SimRequest(target="sim_0", test=selected, work_dir=tmp_path, coverage=True))

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
    result = SimulateFlow(
        coverage_execution=lambda handle, options, _commands, _access: NativeExecution()
    ).execute(SimRequest(target="sim_0", work_dir=tmp_path, coverage=True))
    assert result.exit_code == 0, result.outcome
    assert save_calls == []
    assert state_path.read_bytes() == before_bytes
    assert state_path.stat().st_mtime_ns == before_mtime


def test_projection_publication_failure_returns_structured_report_with_target_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    state_path, initial_state = _prepare_atomic_coverage_ticket(tmp_path, monkeypatch)
    identity = {"generation": "d" * 32, "authored_sha256": "e" * 64}
    adapter = _AcceptanceAdapter(log_dir=tmp_path / "logs", ticket_identity=identity)

    def fail_projection(*_args, **_kwargs) -> None:
        raise OSError(5, "injected projection publication failure")

    monkeypatch.setattr(
        "booley.flows.sim.acceptance.write_compatibility_projection", fail_projection
    )
    result = SimulateFlow(
        coverage_execution=lambda _handle, _options, _commands, _access: _SplitMetricsExecution()
    ).execute(
        SimRequest(
            target="sim_custom",
            work_dir=tmp_path,
            coverage=True,
            report_dir=tmp_path / "reports",
        ),
        adapter=adapter,
    )

    assert result.exit_code == 2
    assert result.outcome.detail["targets"]["sim_custom"]["simulation"] == "pass"
    assert result.outcome.detail["completion_error"]["operation"] == (
        "record acceptance and projections"
    )
    acceptance = result.outcome.detail["acceptance"]
    assert acceptance["status"] == "partial"
    assert acceptance["targets"][0]["committed"] is True
    assert acceptance["targets"][0]["transaction_id"]
    report = json.loads((tmp_path / "reports/sim/1/report.json").read_text())
    assert report["detail"] == result.outcome.detail
    assert state_path.read_bytes() != initial_state
    assert len(DevelopmentState.load(state_path).acceptance_transactions) == 1
    assert not (tmp_path / "reports/sim/1/targets/sim_custom/simulation.json").exists()
    assert "Traceback" not in capsys.readouterr().err


def _acceptance_log_context(tmp_path, monkeypatch, authority_context):
    log_dir = tmp_path / "logs"
    if authority_context == "environment":
        monkeypatch.setenv("BOOLEY_LOGS_DIR", str(log_dir))
        return None
    monkeypatch.setenv("BOOLEY_LOGS_DIR", str(tmp_path / "unrelated-logs"))
    return log_dir


@pytest.mark.parametrize("authority_context", ["explicit", "environment"])
def test_ticket_campaign_acceptance_preserves_atomic_coverage_verdicts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, authority_context: str
) -> None:
    state_path, initial_state = _prepare_atomic_coverage_ticket(tmp_path, monkeypatch)

    identity = {"generation": "d" * 32, "authored_sha256": "e" * 64}
    adapter = _AcceptanceAdapter(log_dir=tmp_path / "logs", ticket_identity=identity)
    flow = SimulateFlow(
        coverage_execution=lambda _handle, _options, _commands, _access: _SplitMetricsExecution()
    )
    result = flow.execute(
        SimRequest(
            target="sim_custom",
            work_dir=tmp_path,
            coverage=True,
            report_dir=tmp_path / "reports",
        ),
        adapter=adapter,
    )

    assert result.exit_code == 0
    assert result.outcome.criterion_key == ""
    assert result.outcome.criterion_met is None
    progress = json.loads((tmp_path / "reports/sim/1/progress.json").read_text())
    assert progress["detail"]["sim_custom"]["exit_code"] == 0
    campaign_id = result.outcome.detail["campaigns"]["sim_custom"]["campaign_id"]
    headline = _target_headline(result.outcome.report_text, "sim_custom")
    assert headline == (
        "sim_custom: simulation PASS · coverage collection COMPLETE · evaluation FAIL "
        "(branch: observed 0/1 points; displayed 0%; minimum 51%) "
        f"(Simulation Campaign {campaign_id})"
    )
    assert (
        f"sim_custom: PASS (Simulation Campaign {campaign_id})" not in result.outcome.report_text
    )
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
    explicit_log_dir = _acceptance_log_context(tmp_path, monkeypatch, authority_context)
    verdict = check_criteria_acceptance(
        state_path, work_dir=tmp_path, log_dir=explicit_log_dir, ticket_identity=identity
    )
    assert verdict.disposition == "review"
    assert verdict.unmet_mandatory == []


def test_arbitrary_state_and_recorder_paths_allow_report_absent_legacy_flow(tmp_path, monkeypatch):
    state_path, _initial = _prepare_atomic_coverage_ticket(tmp_path, monkeypatch)
    state = DevelopmentState.load(state_path)
    state.criteria.pop("_report_submitted")
    branch_key = next(
        key for key, entry in state.criteria.items() if "branch" in entry.params.get("metrics", {})
    )
    state.criteria.pop(branch_key)
    state.save()
    monkeypatch.setattr("booley.config.project_config.is_run_report_enabled", lambda: False)
    identity = {"generation": "d" * 32, "authored_sha256": "e" * 64}
    log_dir = tmp_path / "independent-evidence"
    result = SimulateFlow(
        coverage_execution=lambda _handle, _options, _commands, _access: _SplitMetricsExecution()
    ).execute(
        SimRequest(
            target="sim_custom", work_dir=tmp_path, coverage=True, report_dir=tmp_path / "reports"
        ),
        adapter=_AcceptanceAdapter(log_dir=log_dir, ticket_identity=identity),
    )
    assert result.exit_code == 0
    assert list((log_dir / "acceptance/evidence").rglob("record.json"))
    assert (
        check_criteria_acceptance(
            state_path, work_dir=tmp_path, log_dir=log_dir, ticket_identity=identity
        ).disposition
        == "review"
    )
    assert "_report_submitted" not in DevelopmentState.load(state_path).criteria
    assert not (log_dir / ".runtime/report-submission.json").exists()


def test_gated_passing_coverage_headline_reports_evaluation_pass(tmp_path, monkeypatch):
    _prepare_atomic_coverage_ticket(tmp_path, monkeypatch)
    result = SimulateFlow(
        coverage_execution=lambda _handle, _options, _commands, _access: (
            _PassingSplitMetricsExecution()
        )
    ).execute(
        SimRequest(
            target="sim_custom",
            work_dir=tmp_path,
            coverage=True,
            report_dir=tmp_path / "reports",
        )
    )

    assert result.exit_code == 0
    campaign_id = result.outcome.detail["campaigns"]["sim_custom"]["campaign_id"]
    assert _target_headline(result.outcome.report_text, "sim_custom") == (
        "sim_custom: simulation PASS · coverage collection COMPLETE · evaluation PASS "
        f"(Simulation Campaign {campaign_id})"
    )


def test_gated_suite_mismatch_headline_attaches_diagnostic_to_evaluation(tmp_path, monkeypatch):
    _prepare_atomic_coverage_ticket(tmp_path, monkeypatch)
    tests_path = tmp_path / ".booley_project/tests.toml"
    tests_path.write_text('[sim_custom]\ntests = ["gap", "other"]\n')
    result = SimulateFlow(
        coverage_execution=lambda _handle, _options, _commands, _access: NativeExecution()
    ).execute(
        SimRequest(
            target="sim_custom",
            test=("other",),
            work_dir=tmp_path,
            coverage=True,
            report_dir=tmp_path / "reports",
        )
    )

    assert result.exit_code == 2
    campaign_id = result.outcome.detail["campaigns"]["sim_custom"]["campaign_id"]
    assert _target_headline(result.outcome.report_text, "sim_custom") == (
        "sim_custom: simulation PASS · coverage collection COMPLETE · evaluation BLOCKED "
        f"(COV_EVAL_SUITE_MISMATCH) (Simulation Campaign {campaign_id})"
    )


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


def test_shared_build_prerequisite_failure_publishes_durable_terminal_results(
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

    result = SimulateFlow(
        coverage_execution=lambda handle, options, _commands, _access: Unavailable()
    ).execute(
        SimRequest(
            target="sim_0,sim_1", work_dir=tmp_path, coverage=True, report_dir=tmp_path / "reports"
        )
    )
    assert result.exit_code == 2
    assert "Verilator 5.050 is not the pinned coverage collector" in result.outcome.report_text
    assert len(built) == 2
    assert result.outcome.detail["pending_targets"] == []
    target = result.outcome.detail["targets"]["sim_0"]
    assert target["passed"] is False
    assert target["simulation"] == "aborted"
    assert result.outcome.detail["campaigns"]["sim_0"]["grade"] == "error"
    assert "completion_error" not in result.outcome.detail
    assert "coverage_campaign" in target
    progress = json.loads((tmp_path / "reports/sim/1/progress.json").read_text())
    assert progress["phase"] == "aborted"
    assert progress["complete"] is True
    assert progress["completed_targets"] == ["sim_0", "sim_1"]
    assert progress["pending_targets"] == []


def _prepare_coverage_origin(tmp_path, monkeypatch, *, skipped=()):
    """Create the two-test coverage Project shared by the interruption fixtures."""
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
    return tmp_path / "reports"


def _interrupt_coverage_collection(
    tmp_path, monkeypatch, *, selected=None, skipped=(), boundary=None, no_waivers=False
):
    """Interrupt a coverage run before its collection records a result.

    Without ``boundary`` the interrupt lands inside the executor, leaving the
    aggregate work item ``interrupted``.  With ``"after:manifest_commit"`` it
    lands right after the Manifest is published, leaving the item ``pending``.
    """
    reports = _prepare_coverage_origin(tmp_path, monkeypatch, skipped=skipped)

    class Interrupted(NativeExecution):
        def run(self, request):
            super().run(request)
            raise KeyboardInterrupt("simulated process interruption")

    def checkpoint(actual):
        if actual == boundary:
            raise KeyboardInterrupt(f"simulated interruption at {boundary}")

    request = SimRequest(
        target="sim_0",
        test=selected,
        work_dir=tmp_path,
        coverage=True,
        no_waivers=no_waivers,
        report_dir=reports,
    )
    with pytest.raises(KeyboardInterrupt):
        SimulateFlow(
            coverage_execution=lambda handle, options, _commands, _access: Interrupted(),
            campaign_publication_checkpoint=checkpoint if boundary else None,
        ).execute(request)
    return reports, request


def _interrupt_coverage_publication(tmp_path, monkeypatch, *, selected=None, skipped=()):
    """Interrupt a coverage run after its collection result committed.

    The aggregate work item is complete, so the origin is a legitimate
    publication-only resume source.  Returns ``(reports, request, runs)``.
    """
    reports = _prepare_coverage_origin(tmp_path, monkeypatch, skipped=skipped)
    runs: list[str | None] = []

    class Counted(NativeExecution):
        def run(self, request):
            runs.append(request.test)
            return super().run(request)

    def checkpoint(actual):
        if actual == "before:summary_replace":
            raise KeyboardInterrupt("simulated publication interruption")

    request = SimRequest(
        target="sim_0",
        test=selected,
        work_dir=tmp_path,
        coverage=True,
        report_dir=reports,
    )
    with pytest.raises(KeyboardInterrupt):
        SimulateFlow(
            coverage_execution=lambda handle, options, _commands, _access: Counted(),
            campaign_publication_checkpoint=checkpoint,
        ).execute(request)
    return reports, request, runs


def test_interrupted_and_pruned_invocations_are_never_reused(tmp_path, monkeypatch):
    from booley.flows.sim.campaign_retention import prune_invocation

    reports, request, _runs = _interrupt_coverage_publication(tmp_path, monkeypatch)
    original_path = next(
        (reports / "sim/1/targets/sim_0/campaign").glob(
            "work-items/*/attempts/*/coverage-campaign/native/raw/001-reset.dat"
        )
    )
    original = original_path.read_bytes()
    manifest = reports / "sim/1/targets/sim_0/campaign/manifest.json"
    resumed_reports = reports
    result = SimulateFlow(
        coverage_execution=lambda handle, options, _commands, _access: NativeExecution()
    ).execute(
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
    assert len(attempts) == 1
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
    result = SimulateFlow(
        coverage_execution=lambda handle, options, _commands, _access: NativeExecution()
    ).execute(request)
    assert result.exit_code == 0
    assert (reports / "sim/3/targets/sim_0/coverage.json").is_file()
    prune_invocation(reports, 1)


def test_full_pruning_refuses_resume_dependent_unless_explicitly_included(tmp_path, monkeypatch):
    from booley.flows.sim.campaign.artifact_reference import (
        resolve_report_artifact_reference,
    )
    from booley.flows.sim.campaign.codec import MANIFEST_MAX_BYTES
    from booley.flows.sim.campaign_retention import (
        CampaignRetentionError,
        prune_invocation,
    )

    reports, request, _runs = _interrupt_coverage_publication(tmp_path, monkeypatch)
    manifest = reports / "sim/1/targets/sim_0/campaign/manifest.json"
    result = SimulateFlow(
        coverage_execution=lambda handle, options, _commands, _access: NativeExecution()
    ).execute(SimRequest(resume_from=manifest, work_dir=tmp_path, report_dir=reports))
    assert result.exit_code == 0

    with pytest.raises(CampaignRetentionError, match=re.escape(str(reports / "sim/2"))):
        prune_invocation(reports, 1)

    assert manifest.is_file()
    assert (reports / "sim/2/report.json").is_file()
    survivor = SimulateFlow(
        coverage_execution=lambda handle, options, _commands, _access: NativeExecution()
    ).execute(request)
    assert survivor.exit_code == 0
    survivor_report = reports / "sim/3/report.json"
    prune_invocation(reports, 1, include_dependents=True)
    assert list((reports / "sim/.pruned-1").iterdir()) == []
    assert list((reports / "sim/.pruned-2").iterdir()) == []
    report = json.loads(survivor_report.read_text())
    campaign = report["detail"]["campaigns"]["sim_0"]
    for reference in campaign["artifacts"].values():
        resolved = resolve_report_artifact_reference(
            survivor_report,
            reference,
            expected_kind=reference["kind"],
            expected_owner=reference["owner"],
            maximum=MANIFEST_MAX_BYTES,
        )
        assert resolved.path.is_file()
    prune_invocation(reports, 1, include_dependents=True)
    assert not (reports / "sim/1").exists()


@pytest.mark.parametrize("legacy", [False, True])
def test_dependent_pruning_retries_after_dependent_cleanup_interruption(
    tmp_path, monkeypatch, legacy
):
    from booley.flows.sim.campaign_retention import prune_invocation

    reports, _request, _runs = _interrupt_coverage_publication(tmp_path, monkeypatch)
    manifest = reports / "sim/1/targets/sim_0/campaign/manifest.json"
    result = SimulateFlow(
        coverage_execution=lambda handle, options, _commands, _access: NativeExecution()
    ).execute(SimRequest(resume_from=manifest, work_dir=tmp_path, report_dir=reports))
    assert result.exit_code == 0
    if legacy:
        receipts = manifest.parent / "dependency-receipts"
        for receipt in receipts.iterdir():
            receipt.unlink()
        receipts.rmdir()

    original = Path.unlink
    failed_root = reports / "sim/.pruned-2"
    with monkeypatch.context() as patch:

        def interrupt(path, *args, **kwargs):
            if path.is_relative_to(failed_root):
                raise OSError("dependent cleanup interrupted")
            return original(path, *args, **kwargs)

        patch.setattr(Path, "unlink", interrupt)
        with pytest.raises(OSError, match="dependent cleanup interrupted"):
            prune_invocation(reports, 1, include_dependents=True)

    assert (reports / "sim/1/.prune-batch.json").is_file()
    assert (failed_root / ".prune.json").is_file()
    prune_invocation(reports, 1, include_dependents=True)
    assert list((reports / "sim/.pruned-1").iterdir()) == []
    assert list((reports / "sim/.pruned-2").iterdir()) == []


def test_dependent_pruning_retries_after_origin_cleanup_interruption(tmp_path, monkeypatch):
    from booley.flows.sim.campaign_retention import prune_invocation

    reports, _request, _runs = _interrupt_coverage_publication(tmp_path, monkeypatch)
    manifest = reports / "sim/1/targets/sim_0/campaign/manifest.json"
    result = SimulateFlow(
        coverage_execution=lambda handle, options, _commands, _access: NativeExecution()
    ).execute(SimRequest(resume_from=manifest, work_dir=tmp_path, report_dir=reports))
    assert result.exit_code == 0

    original = Path.unlink
    failed_root = reports / "sim/.pruned-1"
    with monkeypatch.context() as patch:

        def interrupt(path, *args, **kwargs):
            if path.is_relative_to(failed_root):
                raise OSError("origin cleanup interrupted")
            return original(path, *args, **kwargs)

        patch.setattr(Path, "unlink", interrupt)
        with pytest.raises(OSError, match="origin cleanup interrupted"):
            prune_invocation(reports, 1, include_dependents=True)

    assert (failed_root / ".prune-batch.json").is_file()
    prune_invocation(reports, 1, include_dependents=True)
    assert list((reports / "sim/.pruned-1").iterdir()) == []
    assert list((reports / "sim/.pruned-2").iterdir()) == []


def test_dependent_pruning_preflights_all_members_before_mutation(tmp_path, monkeypatch):
    from booley.flows.sim import campaign_retention
    from booley.flows.sim.campaign_retention import (
        CampaignRetentionError,
        prune_invocation,
    )

    reports, _request, _runs = _interrupt_coverage_publication(tmp_path, monkeypatch)
    origin = reports / "sim/1"
    manifest = origin / "targets/sim_0/campaign/manifest.json"
    result = SimulateFlow(
        coverage_execution=lambda handle, options, _commands, _access: NativeExecution()
    ).execute(SimRequest(resume_from=manifest, work_dir=tmp_path, report_dir=reports))
    assert result.exit_code == 0
    original = campaign_retention._preflight_campaign_resources

    def reject_origin(root, project_data):
        if root == origin:
            raise CampaignRetentionError("injected origin preflight failure")
        return original(root, project_data)

    monkeypatch.setattr(campaign_retention, "_preflight_campaign_resources", reject_origin)
    with pytest.raises(CampaignRetentionError, match="injected origin preflight failure"):
        prune_invocation(reports, 1, include_dependents=True)

    assert origin.is_dir()
    assert (reports / "sim/2").is_dir()
    assert not (reports / "sim/.pruned-1").exists()
    assert not (reports / "sim/.pruned-2").exists()
    assert not (origin / ".prune-batch.json").exists()


def test_dependent_pruning_refuses_locked_member_before_mutation(tmp_path, monkeypatch):
    from booley.flows.sim.campaign_reports import campaign_invocation_lock
    from booley.flows.sim.campaign_retention import (
        CampaignRetentionError,
        prune_invocation,
    )

    reports, _request, _runs = _interrupt_coverage_publication(tmp_path, monkeypatch)
    origin = reports / "sim/1"
    manifest = origin / "targets/sim_0/campaign/manifest.json"
    result = SimulateFlow(
        coverage_execution=lambda handle, options, _commands, _access: NativeExecution()
    ).execute(SimRequest(resume_from=manifest, work_dir=tmp_path, report_dir=reports))
    assert result.exit_code == 0
    dependent = reports / "sim/2"

    with (
        campaign_invocation_lock(dependent),
        pytest.raises(CampaignRetentionError, match="invocation 2 is still being produced"),
    ):
        prune_invocation(reports, 1, include_dependents=True)

    assert origin.is_dir()
    assert dependent.is_dir()
    assert not (reports / "sim/.pruned-1").exists()
    assert not (reports / "sim/.pruned-2").exists()


def test_dependent_pruning_removes_multiple_dependents(tmp_path, monkeypatch):
    from booley.flows.sim.campaign_retention import prune_invocation

    reports, _request, _runs = _interrupt_coverage_publication(tmp_path, monkeypatch)
    manifest = reports / "sim/1/targets/sim_0/campaign/manifest.json"
    for _expected in (2, 3):
        result = SimulateFlow(
            coverage_execution=lambda handle, options, _commands, _access: NativeExecution()
        ).execute(SimRequest(resume_from=manifest, work_dir=tmp_path, report_dir=reports))
        assert result.exit_code == 0

    prune_invocation(reports, 1, include_dependents=True)

    assert list((reports / "sim/.pruned-1").iterdir()) == []
    assert list((reports / "sim/.pruned-2").iterdir()) == []
    assert list((reports / "sim/.pruned-3").iterdir()) == []


def test_cross_root_resume_dependency_is_registered_and_pruned(tmp_path, monkeypatch):
    from booley.flows.sim.campaign_retention import (
        CampaignRetentionError,
        prune_invocation,
    )

    reports, _request, _runs = _interrupt_coverage_publication(tmp_path, monkeypatch)
    manifest = reports / "sim/1/targets/sim_0/campaign/manifest.json"
    resumed = tmp_path / "resumed"
    result = SimulateFlow(
        coverage_execution=lambda handle, options, _commands, _access: NativeExecution()
    ).execute(SimRequest(resume_from=manifest, work_dir=tmp_path, report_dir=resumed))
    assert result.exit_code == 0

    with pytest.raises(CampaignRetentionError, match=re.escape(str(resumed / "sim/1"))):
        prune_invocation(reports, 1)

    prune_invocation(reports, 1, include_dependents=True)
    assert list((reports / "sim/.pruned-1").iterdir()) == []
    assert list((resumed / "sim/.pruned-1").iterdir()) == []


def test_native_pruning_preserves_live_resume_dependency(tmp_path, monkeypatch):
    from booley.flows.sim.campaign_retention import (
        CampaignRetentionError,
        prune_invocation,
        prune_native_payload,
    )
    from booley.flows.sim.coverage_analysis_input import read_coverage_campaign

    reports, _request, _runs = _interrupt_coverage_publication(tmp_path, monkeypatch)
    manifest = reports / "sim/1/targets/sim_0/campaign/manifest.json"
    result = SimulateFlow(
        coverage_execution=lambda handle, options, _commands, _access: NativeExecution()
    ).execute(SimRequest(resume_from=manifest, work_dir=tmp_path, report_dir=reports))
    assert result.exit_code == 0

    prune_native_payload(reports, 1, "sim_0")

    assert read_coverage_campaign(
        reports / "sim/1/targets/sim_0/coverage.json"
    ).campaign.campaign_id
    assert (reports / "sim/2/report.json").is_file()
    with pytest.raises(CampaignRetentionError, match=re.escape(str(reports / "sim/2"))):
        prune_invocation(reports, 1)


def test_cross_root_dependent_pruning_rejects_forged_report_reference(tmp_path, monkeypatch):
    from booley.flows.sim.campaign_retention import (
        CampaignRetentionError,
        prune_invocation,
    )

    reports, _request, _runs = _interrupt_coverage_publication(tmp_path, monkeypatch)
    manifest = reports / "sim/1/targets/sim_0/campaign/manifest.json"
    resumed = tmp_path / "resumed"
    result = SimulateFlow(
        coverage_execution=lambda handle, options, _commands, _access: NativeExecution()
    ).execute(SimRequest(resume_from=manifest, work_dir=tmp_path, report_dir=resumed))
    assert result.exit_code == 0
    report_path = resumed / "sim/1/report.json"
    report = json.loads(report_path.read_text())
    reference = report["detail"]["campaigns"]["sim_0"]["artifacts"]["manifest"]
    reference.pop("bytes")
    report_path.write_text(json.dumps(report), encoding="utf-8")

    with pytest.raises(CampaignRetentionError, match="does not authenticate"):
        prune_invocation(reports, 1, include_dependents=True)

    assert manifest.is_file()
    assert report_path.is_file()


def test_cross_root_dependent_pruning_rejects_matching_forged_digests(tmp_path, monkeypatch):
    from booley.flows.sim.campaign_retention import (
        CampaignRetentionError,
        prune_invocation,
    )

    reports, _request, _runs = _interrupt_coverage_publication(tmp_path, monkeypatch)
    manifest = reports / "sim/1/targets/sim_0/campaign/manifest.json"
    resumed = tmp_path / "resumed"
    result = SimulateFlow(
        coverage_execution=lambda handle, options, _commands, _access: NativeExecution()
    ).execute(SimRequest(resume_from=manifest, work_dir=tmp_path, report_dir=resumed))
    assert result.exit_code == 0
    forged_digest = "sha256:" + "0" * 64
    receipt = next((manifest.parent / "dependency-receipts").iterdir())
    receipt_document = json.loads(receipt.read_text())
    receipt_document["manifest_sha256"] = forged_digest
    receipt.write_text(json.dumps(receipt_document), encoding="utf-8")
    report_path = resumed / "sim/1/report.json"
    report = json.loads(report_path.read_text())
    reference = report["detail"]["campaigns"]["sim_0"]["artifacts"]["manifest"]
    reference["sha256"] = forged_digest
    report_path.write_text(json.dumps(report), encoding="utf-8")

    with pytest.raises(CampaignRetentionError, match="does not authenticate"):
        prune_invocation(reports, 1, include_dependents=True)

    assert manifest.is_file()
    assert report_path.is_file()


def test_same_root_legacy_resume_without_receipt_is_still_detected(tmp_path, monkeypatch):
    from booley.flows.sim.campaign_retention import (
        CampaignRetentionError,
        prune_invocation,
    )

    reports, _request, _runs = _interrupt_coverage_publication(tmp_path, monkeypatch)
    manifest = reports / "sim/1/targets/sim_0/campaign/manifest.json"
    result = SimulateFlow(
        coverage_execution=lambda handle, options, _commands, _access: NativeExecution()
    ).execute(SimRequest(resume_from=manifest, work_dir=tmp_path, report_dir=reports))
    assert result.exit_code == 0
    receipts = manifest.parent / "dependency-receipts"
    for receipt in receipts.iterdir():
        receipt.unlink()
    receipts.rmdir()

    with pytest.raises(CampaignRetentionError, match=re.escape(str(reports / "sim/2"))):
        prune_invocation(reports, 1)

    prune_invocation(reports, 1, include_dependents=True)
    assert list((reports / "sim/.pruned-1").iterdir()) == []
    assert list((reports / "sim/.pruned-2").iterdir()) == []


def test_resume_reports_dependency_registration_failure(tmp_path, monkeypatch):
    reports, _request, _runs = _interrupt_coverage_publication(tmp_path, monkeypatch)
    manifest = reports / "sim/1/targets/sim_0/campaign/manifest.json"

    def fail_registration(*_args, **_kwargs):
        raise ValueError("injected dependency failure")

    monkeypatch.setattr(
        "booley.flows.sim.campaign.dependency.register_campaign_dependency",
        fail_registration,
    )
    result = SimulateFlow(coverage_execution=lambda *_args: NativeExecution()).execute(
        SimRequest(resume_from=manifest, work_dir=tmp_path, report_dir=reports)
    )

    assert result.exit_code == 2
    assert "resume dependency failed" in result.outcome.report_text
    assert "injected dependency failure" in result.outcome.report_text


def test_resume_reports_busy_origin_lock(tmp_path, monkeypatch):
    from booley.flows.sim.campaign_reports import campaign_invocation_lock

    reports, _request, _runs = _interrupt_coverage_publication(tmp_path, monkeypatch)
    manifest = reports / "sim/1/targets/sim_0/campaign/manifest.json"

    with campaign_invocation_lock(reports / "sim/1"):
        result = SimulateFlow(coverage_execution=lambda *_args: NativeExecution()).execute(
            SimRequest(resume_from=manifest, work_dir=tmp_path, report_dir=reports)
        )

    assert result.exit_code == 2
    assert "resume origin is busy" in result.outcome.report_text


def test_resume_retains_explicit_configured_skipped_test(tmp_path, monkeypatch):
    reports, _request, _runs = _interrupt_coverage_publication(
        tmp_path, monkeypatch, selected=("wrap",), skipped=("wrap",)
    )
    manifest = reports / "sim/1/targets/sim_0/campaign/manifest.json"
    execution = NativeExecution()

    result = SimulateFlow(
        coverage_execution=lambda _handle, _options, _commands, _access: execution
    ).execute(SimRequest(resume_from=manifest, work_dir=tmp_path, report_dir=tmp_path / "resumed"))

    assert result.exit_code == 0
    # The origin collected "wrap"; a publication-only resume never re-runs it.
    assert execution.runs == []
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
    assert result.outcome.report_text == (
        "sim_0: simulation PASS · coverage collection COMPLETE · evaluation NOT_REQUESTED "
        f"(Simulation Campaign {campaign['campaign_id']})\n"
        "RTL source discovery incomplete; sources without coverage points unavailable."
    )
    resolved = resolve_report_artifact_reference(
        report_path,
        campaign["artifacts"]["manifest"],
        expected_kind="simulation_campaign_manifest",
        expected_owner=campaign["campaign_id"],
        maximum=1024 * 1024,
        external_origin_target=manifest.parents[1],
    )
    assert resolved.path == manifest
    from booley.flows.sim.coverage_source_gaps import INCOMPLETE_CODE, source_gap_summary

    retained = resolve_coverage_campaign_reference(
        manifest.parents[1] / "coverage.json"
    ).loaded.campaign
    assert source_gap_summary(retained).status == "incomplete"
    assert sum(f.code == INCOMPLETE_CODE for f in retained.findings) == 1


def test_coverage_resume_uses_manifest_when_origin_progress_is_missing(tmp_path, monkeypatch):
    reports, _request, _runs = _interrupt_coverage_publication(tmp_path, monkeypatch)
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
    reports, _request, _runs = _interrupt_coverage_publication(tmp_path, monkeypatch)
    manifest = reports / "sim/1/targets/sim_0/campaign/manifest.json"
    resumed_reports = tmp_path / "resumed"

    def interrupt_again(boundary):
        if boundary == "before:summary_replace":
            raise KeyboardInterrupt("resume interrupted")

    with pytest.raises(KeyboardInterrupt, match="resume interrupted"):
        SimulateFlow(
            coverage_execution=lambda *_args: NativeExecution(),
            campaign_publication_checkpoint=interrupt_again,
        ).execute(SimRequest(resume_from=manifest, work_dir=tmp_path, report_dir=resumed_reports))

    progress = json.loads((resumed_reports / "sim/1/progress.json").read_text())
    assert progress["phase"] == "aborted"
    assert progress["completed_targets"] == []
    assert progress["pending_targets"] == ["sim_0"]


def _campaign_bytes(root):
    return {path: path.read_bytes() for path in sorted(root.rglob("*")) if path.is_file()}


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("state", ["interrupted", "pending"])
def test_resume_refuses_unfinished_coverage_collection(tmp_path, monkeypatch, state, dry_run):
    from booley.flows.sim.campaign.store import CampaignStore

    reports, _request = _interrupt_coverage_collection(
        tmp_path,
        monkeypatch,
        boundary="after:manifest_commit" if state == "pending" else None,
    )
    campaign = reports / "sim/1/targets/sim_0/campaign"
    manifest = campaign / "manifest.json"
    observed = CampaignStore(manifest.parent).scan()
    assert len(getattr(observed, state)) == 1
    tree_before = set(reports.rglob("*"))
    bytes_before = _campaign_bytes(campaign)
    factory_calls = []

    def factory(*_args):
        factory_calls.append(_args)
        return NativeExecution()

    result = SimulateFlow(coverage_execution=factory).execute(
        SimRequest(resume_from=manifest, work_dir=tmp_path, report_dir=reports, dry_run=dry_run)
    )

    assert result.exit_code == 2
    text = result.outcome.report_text
    assert "booley flow sim --target sim_0 --coverage" in text
    assert "no recorded result" in text
    assert factory_calls == []
    assert set(reports.rglob("*")) == tree_before
    assert _campaign_bytes(campaign) == bytes_before
    assert result.outcome.detail[state] == list(getattr(observed, state))


@pytest.mark.parametrize("no_waivers", [False, True])
def test_resume_refusal_hint_repeats_no_waivers(tmp_path, monkeypatch, no_waivers):
    reports, _request = _interrupt_coverage_collection(
        tmp_path, monkeypatch, no_waivers=no_waivers
    )
    result = SimulateFlow(coverage_execution=lambda *_args: NativeExecution()).execute(
        SimRequest(
            resume_from=reports / "sim/1/targets/sim_0/campaign/manifest.json",
            work_dir=tmp_path,
            report_dir=reports,
        )
    )

    assert result.exit_code == 2
    hint = next(
        line for line in result.outcome.report_text.splitlines() if "booley flow sim" in line
    )
    assert hint.endswith("--coverage --no-waivers --diagnostic" if no_waivers else "--coverage")


def test_resume_refusal_preserves_corrupt_result_integrity_error(tmp_path, monkeypatch):
    reports, _request, _runs = _interrupt_coverage_publication(tmp_path, monkeypatch)
    campaign = reports / "sim/1/targets/sim_0/campaign"
    result_path = next(campaign.glob("work-items/*/result.json"))
    raw = bytearray(result_path.read_bytes())
    raw[len(raw) // 2] ^= 0x01
    result_path.write_bytes(bytes(raw))

    result = SimulateFlow(coverage_execution=lambda *_args: NativeExecution()).execute(
        SimRequest(resume_from=campaign / "manifest.json", work_dir=tmp_path, report_dir=reports)
    )

    assert result.exit_code == 2
    assert result.outcome.report_text.startswith("Simulation Campaign integrity failure:")


def _crash_coverage_publication(tmp_path, monkeypatch, boundary, *, no_waivers=False):
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

    def execution_factory(_handle, _options, _commands, _access):
        return CountedExecution()

    armed = True

    def checkpoint(actual):
        nonlocal armed
        if armed and actual == boundary:
            armed = False
            raise OSError(f"injected crash at {boundary}")

    request = SimRequest(
        target="sim_0",
        work_dir=tmp_path,
        coverage=True,
        no_waivers=no_waivers,
        report_dir=reports,
    )
    interrupted = SimulateFlow(
        coverage_execution=execution_factory,
        campaign_publication_checkpoint=checkpoint,
    ).execute(request)
    assert interrupted.exit_code == 2
    public = reports / "sim/1/targets/sim_0/coverage.json"
    return reports, runs, public, execution_factory, interrupted


@pytest.mark.parametrize(
    ("boundary", "published"),
    [
        ("after:simulation_result", False),
        ("before:summary_replace", False),
        ("after:summary_replace", False),
        ("before:coverage_reference", False),
        ("after:coverage_reference", True),
    ],
)
def test_coverage_publication_crash_resumes_at_the_aggregate_boundary(
    tmp_path, monkeypatch, boundary, published
):
    """A finished collection with unfinished publication resumes without re-running."""
    from booley.flows.sim.coverage_campaign_store import load_coverage_campaign

    reports, runs, public, execution_factory, interrupted = _crash_coverage_publication(
        tmp_path, monkeypatch, boundary
    )
    assert public.exists() is published
    failed = interrupted.outcome.detail["targets"]["sim_0"]
    assert failed["passed"] is True
    assert failed["simulation"] == "pass"
    assert failed["collection"] == "complete"
    assert failed["evaluation"] == "not_requested"
    assert ("coverage_campaign" in failed) is (boundary == "after:coverage_reference")
    completed_runs = tuple(runs)
    manifest = public.parent / "campaign/manifest.json"

    resumed = SimulateFlow(coverage_execution=execution_factory).execute(
        SimRequest(resume_from=manifest, work_dir=tmp_path, report_dir=reports)
    )

    assert resumed.exit_code == 0
    assert tuple(runs) == completed_runs
    assert resolve_coverage_campaign_reference(public).campaign_path.is_file()
    nested_campaigns = list(
        public.parent.glob("campaign/work-items/*/attempts/*/coverage-campaign/coverage.json")
    )
    assert len(nested_campaigns) == 1
    assert all(load_coverage_campaign(path).campaign.campaign_id for path in nested_campaigns)


@pytest.mark.parametrize(
    "boundary", ["before:coverage_campaign", "after:coverage_campaign", "before:simulation_result"]
)
def test_coverage_collection_crash_refuses_resume(tmp_path, monkeypatch, boundary):
    """A collection without a recorded result is refused, never silently re-run."""
    reports, runs, public, execution_factory, _interrupted = _crash_coverage_publication(
        tmp_path, monkeypatch, boundary
    )
    manifest = public.parent / "campaign/manifest.json"
    completed_runs = tuple(runs)
    nested_before = list(public.parent.glob("campaign/work-items/*/attempts/*/coverage-campaign"))

    resumed = SimulateFlow(coverage_execution=execution_factory).execute(
        SimRequest(resume_from=manifest, work_dir=tmp_path, report_dir=reports)
    )

    assert resumed.exit_code == 2
    assert "booley flow sim --target sim_0 --coverage" in resumed.outcome.report_text
    assert "no recorded result" in resumed.outcome.report_text
    assert tuple(runs) == completed_runs
    nested_after = list(public.parent.glob("campaign/work-items/*/attempts/*/coverage-campaign"))
    assert nested_after == nested_before


def test_resumed_coverage_publication_failure_preserves_current_attempt_truth(
    tmp_path, monkeypatch
):
    reports, _runs, public, execution_factory, _interrupted = _crash_coverage_publication(
        tmp_path, monkeypatch, "after:simulation_result"
    )
    armed = True

    def checkpoint(boundary):
        nonlocal armed
        if armed and boundary == "before:summary_replace":
            armed = False
            raise OSError("injected resumed summary failure")

    resumed = SimulateFlow(
        coverage_execution=execution_factory,
        campaign_publication_checkpoint=checkpoint,
    ).execute(
        SimRequest(
            resume_from=public.parent / "campaign/manifest.json",
            work_dir=tmp_path,
            report_dir=reports,
        )
    )

    assert resumed.exit_code == 2
    detail = resumed.outcome.detail["targets"]["sim_0"]
    assert detail["passed"] is True
    assert detail["simulation"] == "pass"
    assert detail["collection"] == "complete"
    assert detail["evaluation"] == "not_requested"


def test_reference_authentication_failure_drops_only_reference(tmp_path, monkeypatch):
    def reject_owner(_resolved):
        raise ValueError("injected owner authentication failure")

    monkeypatch.setattr(
        "booley.flows.sim.campaign.coverage_execution.authenticate_coverage_campaign_owner",
        reject_owner,
    )
    _reports, _runs, _public, _factory, interrupted = _crash_coverage_publication(
        tmp_path, monkeypatch, "after:coverage_reference"
    )

    detail = interrupted.outcome.detail["targets"]["sim_0"]
    assert detail["passed"] is True
    assert detail["simulation"] == "pass"
    assert detail["collection"] == "complete"
    assert detail["evaluation"] == "not_requested"
    assert "coverage_campaign" not in detail


def test_failure_reference_encoding_cannot_mask_original_error(tmp_path, monkeypatch):
    reports, _runs, public, execution_factory, _interrupted = _crash_coverage_publication(
        tmp_path, monkeypatch, "after:simulation_result"
    )
    armed = True

    def checkpoint(boundary):
        nonlocal armed
        if armed and boundary == "after:coverage_reference":
            armed = False
            raise OSError("injected resumed reference failure")

    def fail_reference(*_args, **_kwargs):
        raise OSError("injected report reference failure")

    monkeypatch.setattr("booley.flows.sim.flow._report_artifact_reference", fail_reference)
    resumed = SimulateFlow(
        coverage_execution=execution_factory,
        campaign_publication_checkpoint=checkpoint,
    ).execute(
        SimRequest(
            resume_from=public.parent / "campaign/manifest.json",
            work_dir=tmp_path,
            report_dir=reports,
        )
    )

    assert resumed.exit_code == 2
    detail = resumed.outcome.detail["targets"]["sim_0"]
    assert detail["passed"] is True
    assert detail["simulation"] == "pass"
    assert detail["error"] == "injected resumed reference failure"
    assert "coverage_campaign" not in detail


def test_retention_failure_drops_reference_when_owner_authentication_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    project(tmp_path)
    data = tmp_path / ".booley_project"
    data.mkdir()
    (data / "tests.toml").write_text('[sim_0]\ntests = ["reset"]\n')

    def fail_retention(_progress, _outcome):
        raise OSError("injected retention failure")

    def reject_owner(_resolved):
        raise ValueError("injected owner authentication failure")

    monkeypatch.setattr("booley.flows.sim.flow._retain_coverage_campaign", fail_retention)
    monkeypatch.setattr("booley.flows.sim.flow.authenticate_coverage_campaign_owner", reject_owner)
    result = SimulateFlow(
        coverage_execution=lambda _handle, _options, _commands, _access: NativeExecution()
    ).execute(
        SimRequest(
            target="sim_0",
            work_dir=tmp_path,
            coverage=True,
            report_dir=tmp_path / "reports",
        )
    )

    assert result.exit_code == 2
    detail = result.outcome.detail["targets"]["sim_0"]
    assert detail["passed"] is True
    assert detail["simulation"] == "pass"
    assert detail["collection"] == "infrastructure_error"
    assert detail["evaluation"] == "not_requested"
    assert "coverage_campaign" not in detail


def test_retention_failure_preserves_in_memory_campaign_truth(tmp_path, monkeypatch):
    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    project(tmp_path)
    data = tmp_path / ".booley_project"
    data.mkdir()
    (data / "tests.toml").write_text('[sim_0]\ntests = ["reset"]\n')

    def fail_retention(_progress, _outcome):
        raise OSError("injected retention failure")

    monkeypatch.setattr("booley.flows.sim.flow._retain_coverage_campaign", fail_retention)
    result = SimulateFlow(
        coverage_execution=lambda _handle, _options, _commands, _access: NativeExecution()
    ).execute(
        SimRequest(
            target="sim_0",
            work_dir=tmp_path,
            coverage=True,
            report_dir=tmp_path / "reports",
        )
    )

    assert result.exit_code == 2
    detail = result.outcome.detail["targets"]["sim_0"]
    assert detail["passed"] is True
    assert detail["simulation"] == "pass"
    assert detail["collection"] == "complete"
    assert detail["evaluation"] == "not_requested"


def test_coverage_lock_covers_final_flow_report_publication(tmp_path, monkeypatch):
    from pathlib import Path

    from booley.flows.sim.campaign_retention import CampaignRetentionError, prune_invocation

    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    project(tmp_path)
    data = tmp_path / ".booley_project"
    data.mkdir()
    (data / "tests.toml").write_text('[sim_0]\ntests = ["reset"]\n')
    replace = Path.replace
    checked = []

    def check_lock(path, destination):
        if destination.name == "report.json":
            with pytest.raises(CampaignRetentionError, match="still being produced"):
                prune_invocation(tmp_path / "reports", 1)
            checked.append(True)
        return replace(path, destination)

    monkeypatch.setattr(Path, "replace", check_lock)
    result = SimulateFlow(
        coverage_execution=lambda handle, options, _commands, _access: NativeExecution()
    ).execute(
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
    result = SimulateFlow(
        coverage_execution=lambda handle, options, _commands, _access: NativeExecution()
    ).execute(SimRequest(target="sim_0", work_dir=tmp_path, coverage=True, report_dir=reports))
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
    result = SimulateFlow(
        coverage_execution=lambda handle, options, _commands, _access: NativeExecution()
    ).execute(
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
    result = SimulateFlow(
        coverage_execution=lambda handle, options, _commands, _access: NativeExecution()
    ).execute(SimRequest(target="sim_0", work_dir=tmp_path, coverage=True))
    assert result.exit_code == 0
    campaign = next(tmp_path.rglob("targets/sim_0/coverage.json"))
    model = Model()
    analyst = CoverageAnalystSpecialist(model=model)
    analyst.parse_args(["--work-dir", str(tmp_path), "--campaign", str(campaign)])
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    report = analyst.coverage_analyst(campaign).to_dict()
    assert report["$schema"] == "booley.coverage-analysis/v2"
    assert report["observed_evidence"]["campaign_manifest"]["$schema"] == (
        "booley.coverage-campaign/v4"
    )
    assert "points" not in report["observed_evidence"]
    assert report["observed_evidence"]["point_store_sha256"].startswith("sha256:")
    assert report["eligibility"] == "eligible"
    assert len(model.calls) == 1
    prompt = json.loads(model.calls[0].prompt)
    assert "campaign" not in prompt
    assert prompt["campaign_reference"]["storage_schema"] == "booley.coverage-campaign/v4"
    assert prompt["campaign_reference"]["point_count"] == 1
    assert "points" not in prompt
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before


@pytest.mark.parametrize(
    "verdict,hits,missing,evaluation,exit_code",
    [
        ("pass", 2, False, "pass", 0),
        ("fail", 2, False, "pass", 1),
        ("pass", 0, False, "fail", 0),
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
        coverage_execution=lambda handle, options, _commands, _access: NativeExecution(
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


def _run_gap_ticket_coverage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    registered: tuple[str, ...],
    skip: tuple[str, ...] = (),
    criterion_tests: tuple[str, ...] = ("gap",),
    tests: tuple[str, ...] = (),
    ticket: bool = True,
):
    """Run sim_custom coverage against a Criterion listing *criterion_tests*."""
    state_path, _initial = _prepare_atomic_coverage_ticket(tmp_path, monkeypatch)
    listing = ", ".join(f'"{name}"' for name in registered)
    skipped = ", ".join(f'"{name}"' for name in skip)
    skipping = f"skip = [{skipped}]\n" if skip else ""
    (tmp_path / ".booley_project/tests.toml").write_text(
        f"[sim_custom]\ntests = [{listing}]\n{skipping}"
    )
    state = DevelopmentState.load(state_path)
    for entry in state.criteria.values():
        if "tests" in entry.params:
            entry.params["tests"] = list(criterion_tests)
    state.save()
    if not ticket:
        monkeypatch.delenv("BOOLEY_STATE_FILE")
    execution = NativeExecution()
    result = SimulateFlow(
        coverage_execution=lambda _handle, _options, _commands, _access: execution
    ).execute(
        SimRequest(
            target="sim_custom",
            work_dir=tmp_path,
            coverage=True,
            test=tests or None,
            report_dir=tmp_path / "reports",
        )
    )
    return result, [run.test.name for run in execution.runs]


def _coverage_evaluation(tmp_path: Path) -> dict[str, object]:
    campaign = next(tmp_path.rglob("targets/sim_custom/coverage.json"))
    return resolve_coverage_campaign_reference(campaign).loaded.campaign.evaluation


def test_criterion_test_list_does_not_select_coverage_tests(tmp_path, monkeypatch):
    result, ran = _run_gap_ticket_coverage(tmp_path, monkeypatch, registered=("gap", "other"))

    assert sorted(ran) == ["gap", "other"]
    assert result.exit_code == 2
    evaluation = _coverage_evaluation(tmp_path)
    assert evaluation["status"] == "blocked"
    assert evaluation["suite"]["status"] == "mismatch"
    assert list(evaluation["suite"]["required"]) == ["gap"]


def test_criterion_test_list_is_satisfied_by_explicit_tests(tmp_path, monkeypatch):
    result, ran = _run_gap_ticket_coverage(
        tmp_path, monkeypatch, registered=("gap", "other"), tests=("gap",)
    )

    assert ran == ["gap"]
    evaluation = _coverage_evaluation(tmp_path)
    assert evaluation["suite"]["status"] == "match"
    # Blocked only by the plain execution's missing branch/expression support.
    assert "COV_EVAL_SUITE_MISMATCH" not in {item["code"] for item in evaluation["diagnostics"]}
    assert result.exit_code == 2


def test_default_coverage_selection_applies_skips_with_a_criterion(tmp_path, monkeypatch):
    result, ran = _run_gap_ticket_coverage(
        tmp_path,
        monkeypatch,
        registered=("gap", "other"),
        skip=("other",),
        criterion_tests=("gap", "other"),
    )

    assert ran == ["gap"]
    assert result.exit_code == 2
    evaluation = _coverage_evaluation(tmp_path)
    assert evaluation["status"] == "blocked"
    assert evaluation["suite"]["status"] == "mismatch"


def test_ticket_and_interactive_coverage_select_the_same_tests(tmp_path_factory, monkeypatch):
    ticket = tmp_path_factory.mktemp("ticket")
    interactive = tmp_path_factory.mktemp("interactive")

    _result, ticket_ran = _run_gap_ticket_coverage(
        ticket, monkeypatch, registered=("gap", "other")
    )
    _result, interactive_ran = _run_gap_ticket_coverage(
        interactive, monkeypatch, registered=("gap", "other"), ticket=False
    )

    assert sorted(ticket_ran) == sorted(interactive_ran) == ["gap", "other"]


def test_failed_test_exit_code_is_recorded_per_target_in_progress(tmp_path, monkeypatch):
    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    project(tmp_path)
    data = tmp_path / ".booley_project"
    data.mkdir()
    (data / "tests.toml").write_text('[sim_0]\ntests = ["reset"]\n')

    result = SimulateFlow(
        coverage_execution=lambda _handle, _options, _commands, _access: NativeExecution(
            verdict="fail"
        )
    ).execute(
        SimRequest(
            target="sim_0", work_dir=tmp_path, coverage=True, report_dir=tmp_path / "reports"
        )
    )

    assert result.exit_code == 1
    progress = json.loads((tmp_path / "reports/sim/1/progress.json").read_text())
    assert progress["detail"]["sim_0"]["exit_code"] == 1


# --- #992: --no-waivers -----------------------------------------------------------


def _configure_missing_waiver_directory(root: Path) -> None:
    """Configure waivers whose directory is absent: a load-time error for gated runs."""
    (root / ".booley_project" / "booley.toml").write_text(
        '[coverage.waivers]\nanchor = "project_data_repository"\ndirectory = "missing-waivers"\n'
    )


def _coverage_evaluation_status(reports: Path, selector: str) -> str:
    campaign = next(reports.rglob(f"targets/{selector}/coverage.json"))
    return resolve_coverage_campaign_reference(campaign).loaded.campaign.evaluation["status"]


def _finding_codes(outcome) -> list[str]:
    return [item["code"] for item in outcome.detail["findings"]]


def _workload(reports: Path, selector: str) -> dict[str, object]:
    manifest = next(reports.rglob(f"targets/{selector}/campaign/manifest.json"))
    return json.loads(manifest.read_text())["workload"]


@pytest.mark.parametrize("dry_run", [False, True])
def test_gated_no_waivers_is_refused_before_any_build(tmp_path, monkeypatch, dry_run):
    state_path, initial_state = _prepare_atomic_coverage_ticket(tmp_path, monkeypatch)
    _configure_missing_waiver_directory(tmp_path)
    factory_calls = []

    def execution_factory(*_args):
        factory_calls.append(_args)
        return _PassingSplitMetricsExecution()

    result = SimulateFlow(coverage_execution=execution_factory).execute(
        SimRequest(
            target="sim_custom",
            work_dir=tmp_path,
            coverage=True,
            no_waivers=True,
            dry_run=dry_run,
            report_dir=tmp_path / "reports",
        )
    )

    assert result.exit_code == 2
    assert _finding_codes(result.outcome) == ["COV_NO_WAIVERS_CRITERION"]
    assert "--diagnostic" in result.outcome.report_text
    assert factory_calls == []
    assert not (tmp_path / "reports/sim/1").exists()
    assert state_path.read_bytes() == initial_state


def test_gated_no_waivers_diagnostic_reports_raw_and_records_nothing(tmp_path, monkeypatch):
    state_path, _initial_state = _prepare_atomic_coverage_ticket(tmp_path, monkeypatch)
    _configure_missing_waiver_directory(tmp_path)
    identity = {"generation": "d" * 32, "authored_sha256": "e" * 64}
    adapter = _AcceptanceAdapter(log_dir=tmp_path / "logs", ticket_identity=identity)

    def run(name: str, **options):
        SimulateFlow(coverage_execution=lambda *_args: _PassingSplitMetricsExecution()).execute(
            SimRequest(
                target="sim_custom",
                work_dir=tmp_path,
                coverage=True,
                diagnostic=True,
                report_dir=tmp_path / name,
                **options,
            ),
            adapter=adapter,
        )

    # Control: the same run with waivers applied is blocked by the missing directory.
    run("control")
    assert _coverage_evaluation_status(tmp_path / "control", "sim_custom") == "blocked"
    run("raw", no_waivers=True)
    assert _coverage_evaluation_status(tmp_path / "raw", "sim_custom") != "blocked"
    assert _workload(tmp_path / "raw", "sim_custom")["no_waivers"] is True
    assert "no_waivers" not in _workload(tmp_path / "control", "sim_custom")
    # Run history (timeline) may change; Criteria and acceptance must not.
    recorded = DevelopmentState.load(state_path)
    assert all(entry.met is False for entry in recorded.criteria.values())
    assert recorded.acceptance_transactions == []
    assert _ledger_json(tmp_path / "logs") == {}


def test_default_coverage_manifest_omits_no_waivers(tmp_path, monkeypatch):
    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    project(tmp_path)
    data = tmp_path / ".booley_project"
    data.mkdir()
    (data / "tests.toml").write_text('[sim_0]\ntests = ["reset"]\n')

    def run(name: str, **options):
        result = SimulateFlow(coverage_execution=lambda *_args: NativeExecution()).execute(
            SimRequest(
                target="sim_0",
                work_dir=tmp_path,
                coverage=True,
                report_dir=tmp_path / name,
                **options,
            )
        )
        assert result.exit_code == 0, result.outcome
        return _workload(tmp_path / name, "sim_0")

    assert "no_waivers" not in run("default")
    assert run("raw", no_waivers=True)["no_waivers"] is True


def test_ungated_invalid_waivers_exit_2_with_no_waivers_hint(tmp_path, monkeypatch):
    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    project(tmp_path)
    data = tmp_path / ".booley_project"
    data.mkdir()
    (data / "tests.toml").write_text('[sim_0]\ntests = ["reset"]\n')
    _configure_missing_waiver_directory(tmp_path)

    def run(name: str, **options):
        return SimulateFlow(coverage_execution=lambda *_args: NativeExecution()).execute(
            SimRequest(
                target="sim_0",
                work_dir=tmp_path,
                coverage=True,
                report_dir=tmp_path / name,
                **options,
            )
        )

    blocked = run("default")
    assert blocked.exit_code == 2
    assert _coverage_evaluation_status(tmp_path / "default", "sim_0") == "blocked"
    assert "evaluation BLOCKED (COV_WAIVER_DIRECTORY_MISSING)" in blocked.outcome.report_text
    assert "--no-waivers" in blocked.outcome.report_text

    raw = run("raw", no_waivers=True)
    assert raw.exit_code == 0
    assert _coverage_evaluation_status(tmp_path / "raw", "sim_0") == "not_requested"


def _install_coverage_criterion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Install a ``coverage_sim_0`` Criterion whose suite (``all``) matches reset/wrap."""
    from booley.criteria.state import CriterionEntry

    state_path = tmp_path / "state.json"
    state = DevelopmentState.load(state_path)
    state.strict_criteria = True
    state.criteria = {
        "coverage_sim_0": CriterionEntry(
            params={"target": "sim_0", "tests": "all", "metrics": {"line": {"min_pct": 1}}}
        )
    }
    state.save()
    monkeypatch.setenv("BOOLEY_STATE_FILE", str(state_path))
    monkeypatch.setenv("BOOLEY_LOGS_DIR", str(tmp_path / "logs"))
    return state_path


def test_no_waivers_resume_of_ungated_manifest_does_not_rerun_tests(tmp_path, monkeypatch):
    reports, runs, public, execution_factory, _interrupted = _crash_coverage_publication(
        tmp_path, monkeypatch, "after:simulation_result", no_waivers=True
    )
    completed_runs = tuple(runs)

    resumed = SimulateFlow(coverage_execution=execution_factory).execute(
        SimRequest(
            resume_from=public.parent / "campaign/manifest.json",
            work_dir=tmp_path,
            report_dir=reports,
        )
    )

    assert resumed.exit_code == 0, resumed.outcome
    assert tuple(runs) == completed_runs


def test_no_waivers_resume_with_criterion_needs_diagnostic(tmp_path, monkeypatch):
    reports, _runs, public, execution_factory, _interrupted = _crash_coverage_publication(
        tmp_path, monkeypatch, "after:simulation_result", no_waivers=True
    )
    state_path = _install_coverage_criterion(tmp_path, monkeypatch)
    before = state_path.read_bytes()
    manifest = public.parent / "campaign/manifest.json"
    flow = SimulateFlow(coverage_execution=execution_factory)

    refused = flow.execute(SimRequest(resume_from=manifest, work_dir=tmp_path, report_dir=reports))

    assert refused.exit_code == 2
    assert _finding_codes(refused.outcome) == ["COV_NO_WAIVERS_CRITERION"]
    assert "--diagnostic" in refused.outcome.report_text
    assert sorted(path.name for path in (reports / "sim").iterdir() if path.is_dir()) == ["1"]
    assert state_path.read_bytes() == before

    diagnostic = SimulateFlow(coverage_execution=execution_factory).execute(
        SimRequest(resume_from=manifest, work_dir=tmp_path, report_dir=reports, diagnostic=True)
    )

    assert diagnostic.exit_code == 0, diagnostic.outcome
    assert state_path.read_bytes() == before


def _coverage_headline_state(tmp_path, monkeypatch, ticket):
    from booley.criteria.state import CriterionEntry

    state_path = tmp_path / "state.json"
    if ticket == "none":
        return state_path
    state = DevelopmentState.load(state_path)
    if ticket in {"coverage", "coverage_only"}:
        state.strict_criteria = True
        state.criteria = {
            "coverage_sim_0": CriterionEntry(
                params={
                    "target": "acme:demo:counter:1#sim_0",
                    "_target_selector": "sim_0",
                    "tests": "all",
                    "metrics": {"line": {"min_pct": 100}},
                }
            ),
            "sim_pass_sim_0": CriterionEntry(
                params={"target": "acme:demo:counter:1#sim_0", "_target_selector": "sim_0"}
            ),
        }
    if ticket == "coverage_only":
        del state.criteria["sim_pass_sim_0"]
    state.slug = "headline-ticket"
    state.save()
    monkeypatch.setenv("BOOLEY_STATE_FILE", str(state_path))
    monkeypatch.setenv("BOOLEY_LOGS_DIR", str(tmp_path / "logs"))
    return state_path


def _assert_coverage_headline_results(results, ticket, verdict, state_path):
    expected = (
        ("sim_pass_sim_0", verdict == "pass")
        if ticket == "none"
        else ("coverage_sim_0", False)
        if ticket == "coverage_only"
        else ("", None)
    )
    evaluation = "fail" if ticket in {"coverage", "coverage_only"} else "not_requested"
    for result in results:
        assert result.outcome.detail["targets"]["sim_0"]["evaluation"] == evaluation
        assert (result.outcome.criterion_key, result.outcome.criterion_met) == expected
        assert result.exit_code == (1 if verdict == "fail" else 0), result.outcome
    if ticket == "undeclared":
        assert DevelopmentState.load(state_path).criteria == {}
    if ticket in {"coverage", "coverage_only"}:
        saved = DevelopmentState.load(state_path)
        if ticket == "coverage":
            assert saved.criteria["sim_pass_sim_0"].met is True
        assert saved.criteria["coverage_sim_0"].met is False
    elif ticket == "none":
        assert not state_path.exists()


@pytest.mark.parametrize(
    "ticket,verdict",
    [
        ("none", "pass"),
        ("none", "fail"),
        ("undeclared", "pass"),
        ("coverage", "pass"),
        ("coverage_only", "pass"),
    ],
)
def test_public_coverage_campaign_headline_fresh_and_resume(
    tmp_path, monkeypatch, ticket, verdict
):
    monkeypatch.delenv("BOOLEY_STATE_FILE", raising=False)
    reports = _prepare_coverage_origin(tmp_path, monkeypatch)
    state_path = _coverage_headline_state(tmp_path, monkeypatch, ticket)
    runs = []

    class Counted(NativeExecution):
        def run(self, request):
            runs.append(request.test)
            return super().run(request)

    def invoke(request):
        adapter = (
            _AcceptanceAdapter(
                log_dir=tmp_path / "logs",
                ticket_identity={"generation": "d" * 32, "authored_sha256": "e" * 64},
            )
            if ticket in {"coverage", "coverage_only"}
            else None
        )
        return SimulateFlow(
            coverage_execution=lambda *_args: Counted(hits=0, verdict=verdict)
        ).execute(request, adapter=adapter)

    fresh = invoke(
        SimRequest(target="sim_0", work_dir=tmp_path, coverage=True, report_dir=reports)
    )
    completed = tuple(runs)
    assert tuple(item.name for item in completed) == ("reset", "wrap")
    manifest = reports / "sim/1/targets/sim_0/campaign/manifest.json"
    resumed = invoke(SimRequest(resume_from=manifest, work_dir=tmp_path, report_dir=reports))
    assert tuple(runs) == completed
    _assert_coverage_headline_results((fresh, resumed), ticket, verdict, state_path)


def _coverage_hook_flow_fixture(tmp_path, monkeypatch):
    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    project(tmp_path)
    data = tmp_path / ".booley_project"
    data.mkdir()
    (data / "tests.toml").write_text('[sim_0]\ntests = ["reset"]\n')
    (data / "booley.toml").write_text('[flows.sim]\npre_run_commands = ["echo frozen-hook"]\n')
    return SimulateFlow(coverage_execution=_frozen_hook_execution(tmp_path, data, []))


@pytest.mark.parametrize("side", ["before", "after"])
def test_actual_coverage_hook_publication_crash_keeps_only_durable_evidence(
    tmp_path, monkeypatch, side
):
    from booley.flows.sim.campaign.coverage_execution import CoverageAggregateExecutor
    from tests.flows.sim.test_campaign_crash_matrix import _CrashOnce, _InjectedProcessDeath

    original = CoverageAggregateExecutor.__init__
    crash = _CrashOnce(f"{side}:pre_sim_evidence")

    def with_checkpoint(executor, **kwargs):
        original(executor, **{**kwargs, "publication_checkpoint": crash})

    monkeypatch.setattr(CoverageAggregateExecutor, "__init__", with_checkpoint)
    flow = _coverage_hook_flow_fixture(tmp_path, monkeypatch)
    with pytest.raises(_InjectedProcessDeath):
        flow.execute(
            SimRequest(
                target="sim_0", work_dir=tmp_path, coverage=True, report_dir=tmp_path / "reports"
            )
        )
    assert crash.tripped
    manifest = tmp_path / "reports/sim/1/targets/sim_0/campaign/manifest.json"
    from booley.flows.sim.campaign import read_pre_sim_firings

    firings = read_pre_sim_firings(manifest)
    assert len(firings) == int(side == "after")
    assert all(f.document["stdout_tail"] == "frozen-hook\n" for f in firings)
    assert not list(manifest.parent.glob("work-items/*/result.json"))


def test_coverage_hook_publication_integrity_failure_keeps_error_category(tmp_path, monkeypatch):
    from booley.flows.sim import flow as flow_module
    from booley.flows.sim.campaign.codec import SimulationCampaignIntegrityError
    from booley.flows.sim.campaign.store import CampaignStore

    original = CampaignStore.publish_pre_sim_evidence
    failure_outcome = flow_module._coverage_failure_outcome
    categories = []

    def preserve_category(*args, **kwargs):
        categories.append(type(args[3]))
        return failure_outcome(*args, **kwargs)

    monkeypatch.setattr(flow_module, "_coverage_failure_outcome", preserve_category)

    def fail_after_publication(store, directory, ordinal, raw, **kwargs):
        original(store, directory, ordinal, raw, **kwargs)
        raise SimulationCampaignIntegrityError("hook publication integrity probe")

    monkeypatch.setattr(CampaignStore, "publish_pre_sim_evidence", fail_after_publication)
    flow = _coverage_hook_flow_fixture(tmp_path, monkeypatch)
    result = flow.execute(
        SimRequest(
            target="sim_0", work_dir=tmp_path, coverage=True, report_dir=tmp_path / "reports"
        )
    )
    assert result.exit_code == 2
    assert "hook publication integrity probe" in result.outcome.report_text
    assert "collector_error" not in result.outcome.report_text
    assert categories == [SimulationCampaignIntegrityError]
    from booley.flows.sim.campaign import read_pre_sim_firings

    firings = read_pre_sim_firings(tmp_path / "reports/sim/1/targets/sim_0/campaign/manifest.json")
    assert len(firings) == 1
    assert firings[0].document["stdout_tail"] == "frozen-hook\n"
    assert result.outcome.detail["pre_sim_current"] == 1


def _observe_pre_sim_projection(monkeypatch):
    from booley.flows.sim import flow as flow_module
    from booley.flows.sim.campaign import pre_sim_evidence

    reads = {"manifest": 0, "preview": 0, "decoder_manifest": 0}
    digest = pre_sim_evidence.manifest_digest
    artifact = flow_module._report_artifact_reference
    preview = flow_module._pre_sim_preview_record

    def reference(*args, **kwargs):
        reads["manifest"] += int(kwargs["kind"] == "simulation_campaign_manifest")
        return artifact(*args, **kwargs)

    def record(*args):
        reads["preview"] += 1
        return preview(*args)

    def manifest_digest(manifest):
        reads["decoder_manifest"] += 1
        return digest(manifest)

    monkeypatch.setattr(pre_sim_evidence, "manifest_digest", manifest_digest)
    monkeypatch.setattr(flow_module, "_report_artifact_reference", reference)
    monkeypatch.setattr(flow_module, "_pre_sim_preview_record", record)
    return reads


def _owned_coverage_hook_request(tmp_path):
    from booley.flows.sim.campaign.codec import canonical_json_bytes
    from booley.flows.sim.campaign.planning import finalize_manifest
    from booley.flows.sim.campaign.serial_execution import _attempt
    from booley.flows.sim.campaign.store import CampaignStore
    from tests.flows.sim.test_campaign_phase3_integrity import (
        _maximum_hook_selection_plan,
        _request,
    )
    from tests.flows.sim.test_endpoint_campaign_lifecycle import _refresh_hook_workload_identity

    plan, _ = _maximum_hook_selection_plan(("wrap",))
    document = json.loads(canonical_json_bytes(plan.manifest.document))
    document.pop("fingerprints")
    document["target"]["vlnv"] = "acme:demo:counter:1"
    document["workload"]["source_recipe"]["pre_sim_commands"] = ["echo owned-hook"]
    _refresh_hook_workload_identity(document)
    manifest = finalize_manifest(document)
    store = CampaignStore(tmp_path / "reports/1/targets/sim/campaign")
    store.publish_manifest(manifest)
    request = _request(store, manifest, manifest.document["work_items"][0], tmp_path, invocation=1)
    store.publish_attempt(request.attempt_directory, _attempt(request)[0])
    return request


@pytest.mark.parametrize("side", ["before", "after"])
def test_owned_coverage_sink_crash_preserves_actual_hook_before_adapter_launch(
    tmp_path, monkeypatch, side
):
    from booley.flows.sim.campaign.coverage_execution import _publish_hook
    from tests.flows.sim.test_campaign_crash_matrix import _CrashOnce, _InjectedProcessDeath
    from tests.flows.sim.test_verilator_coverage_execution import (
        _build_coverage,
        _execution_fixture,
        _run_request,
    )

    execution, target, raw_path, captured = _execution_fixture(tmp_path, monkeypatch)
    assert _build_coverage(execution, target).success
    request = _owned_coverage_hook_request(tmp_path)
    assert target.identity == "acme:demo:counter:1#sim"
    monkeypatch.setattr(
        "booley.flows.sim.verilator_coverage_execution.resolve_pre_sim_commands",
        lambda _root: ("echo owned-hook",),
    )
    crash = _CrashOnce(f"{side}:pre_sim_evidence")
    execution.set_pre_sim_evidence_sink(
        lambda evidence: _publish_hook(request, evidence, 1, crash)
    )
    with pytest.raises(_InjectedProcessDeath):
        execution.run(_run_request(target, raw_path))
    assert crash.tripped
    assert "run_script" not in captured
    firings = request.store.read_pre_sim_firings()
    assert len(firings) == int(side == "after")
    assert all(f.document["stdout_tail"] == "owned-hook\n" for f in firings)
    assert all(f.document["returncode"] == 0 for f in firings)
    assert not list(request.store.root.glob("work-items/*/result.json"))

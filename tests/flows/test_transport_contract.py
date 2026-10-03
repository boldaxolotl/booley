"""Compatibility evidence captured before separating Flow and MCP ownership."""

import json
import logging
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from booley.flows.execution_persistence import StandaloneFlowExecution
from booley.flows.flow_session import FlowSession
from booley.flows.fpga.flow import FpgaImplFlow
from booley.flows.lint.flow import LintFlow
from booley.flows.sim.flow import SimulateFlow
from booley.flows.synth.flow import AsicSynthesizeFlow
from booley.mcp.flow_adapter import flow_schema

FLOWS = (LintFlow, SimulateFlow, AsicSynthesizeFlow, FpgaImplFlow)


def test_standalone_cli_wires_utc_formatter(monkeypatch):
    from booley.flows import endpoint_cli

    configured = {}
    endpoint = MagicMock()
    endpoint.main.return_value = 0
    monkeypatch.setattr(
        endpoint_cli.logging, "basicConfig", lambda **kwargs: configured.update(kwargs)
    )

    with pytest.raises(SystemExit) as exc:
        endpoint_cli.cli(endpoint)

    assert exc.value.code == 0
    handler = configured["handlers"][0]
    record = logging.LogRecord("booley.flow", logging.INFO, "", 0, "event", (), None)
    record.created = 1_790_341_637.0
    assert handler.format(record) == "2026-09-25T13:07:17Z [booley.flow] INFO: event"


class RecordingExecution(StandaloneFlowExecution):
    def __init__(self, recorder):
        self.recorder = recorder

    def record_changes(self, *args, **kwargs):
        return self.recorder.record_changes(*args, **kwargs)


@pytest.mark.parametrize("flow_type", FLOWS)
def test_complete_builtin_schema_is_compatible(flow_type):
    expected = json.loads((Path(__file__).parent / "fixtures/builtin_schemas.json").read_text())
    flow = flow_type()
    assert flow_schema(flow) == expected[flow.name]


@pytest.fixture
def runtime(monkeypatch, tmp_path):
    from booley.runtime import runtime_context

    monkeypatch.setattr(runtime_context, "inside_session_runtime", lambda: True)
    for name in (
        "BOOLEY_STATE_FILE",
        "BOOLEY_TICKET_FILE",
        "BOOLEY_LOGS_DIR",
        "BOOLEY_RUNTIME_DIR",
    ):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


@pytest.mark.parametrize("flow_type", FLOWS)
@pytest.mark.parametrize("diagnostic", (False, True))
def test_typed_request_executes_without_constructing_a_parser(
    flow_type, diagnostic, runtime, monkeypatch
):
    import argparse

    from booley.runtime.endpoint_execution import EndpointOutcome

    flow = flow_type()
    request = flow.request_type(target="demo", work_dir=runtime, diagnostic=diagnostic)
    monkeypatch.setattr(
        argparse, "ArgumentParser", lambda **kwargs: pytest.fail("parser constructed")
    )
    monkeypatch.setattr(flow, "_run", lambda: EndpointOutcome(detail={"evidence": "same"}))
    result = flow.execute(request)
    assert result.exit_code == 0
    assert result.outcome.detail["evidence"] == "same"
    assert result.outcome.detail.get("acceptance_effect") == ("diagnostic" if diagnostic else None)
    assert not hasattr(flow.context, "_cli_parser")
    assert request.report_dir is None
    report_root = runtime / "flow-reports"
    assert flow.context.args.report_dir == report_root
    assert (report_root / f"{flow.name}.json").is_file()
    assert (report_root / flow.name / "1/report.json").is_file()


def test_flow_report_root_precedence(runtime, monkeypatch):
    from booley.runtime.endpoint_execution import EndpointOutcome

    flow = LintFlow()
    monkeypatch.setattr(flow, "_run", EndpointOutcome)
    explicit = runtime / "explicit"
    monkeypatch.setenv("BOOLEY_RUNTIME_DIR", str(runtime / "runtime"))

    flow.execute(flow.request_type(target="demo", work_dir=runtime, report_dir=explicit))
    assert flow.context.args.report_dir == explicit

    flow.execute(flow.request_type(target="demo", work_dir=runtime))
    assert flow.context.args.report_dir == runtime / "runtime/flow-reports"

    monkeypatch.delenv("BOOLEY_RUNTIME_DIR")
    flow.execute(flow.request_type(target="demo", work_dir=runtime))
    assert flow.context.args.report_dir == runtime / "flow-reports"


def test_adapter_report_root_wins_over_direct_default(runtime, monkeypatch):
    from booley.evidence.acceptance import ResolvedFlowAcceptance
    from booley.flows.execution_persistence import StandaloneFlowExecution
    from booley.runtime.endpoint_execution import EndpointOutcome

    selected = runtime / "logs/.runtime/flow-reports"

    class RuntimeSelectingExecution(StandaloneFlowExecution):
        def validate_and_resolve(self, request):
            request.report_dir = selected
            return ResolvedFlowAcceptance(ticket_backed=True)

    flow = LintFlow()
    monkeypatch.setattr(flow, "_run", EndpointOutcome)
    result = flow.execute(
        flow.request_type(target="demo", work_dir=runtime),
        adapter=RuntimeSelectingExecution(),
    )

    assert result.exit_code == 0
    assert flow.context.args.report_dir == selected


def test_direct_simulation_invocations_share_report_numbering(runtime, monkeypatch):
    from booley.runtime.endpoint_execution import EndpointOutcome

    reserved = []
    monkeypatch.setattr(SimulateFlow, "prepare_simulation_endpoint", lambda self: None)
    monkeypatch.setattr(SimulateFlow, "prepare_target_endpoint", lambda self: None)

    def run(session):
        reserved.append(session.reserve_invocation_dir())
        return EndpointOutcome()

    monkeypatch.setattr(FlowSession, "_run", run)
    for _ in range(3):
        flow = SimulateFlow()
        result = flow.execute(flow.request_type(target="demo", work_dir=runtime))
        assert result.exit_code == 0

    report_root = runtime / "flow-reports/sim"
    assert reserved == [report_root / "1", report_root / "2", report_root / "3"]


def test_uninitialized_flow_fails_before_state_or_execution(runtime, monkeypatch):
    from booley.runtime.project_dir import reset_cache

    uninitialized = runtime / "uninitialized"
    uninitialized.mkdir()
    monkeypatch.chdir(uninitialized)
    monkeypatch.delenv("BOOLEY_PROJECT_DIR")
    reset_cache()
    flow = LintFlow()
    monkeypatch.setattr(
        FlowSession, "read_state", lambda self: pytest.fail("loaded state before report root")
    )
    monkeypatch.setattr(flow, "_run", lambda: pytest.fail("executed uninitialized Flow"))

    result = flow.execute(flow.request_type(target="demo", work_dir=uninitialized))

    assert result.exit_code == 2
    assert "Run 'booley init'" in result.outcome.report_text
    assert not (uninitialized / "flow-reports").exists()


@pytest.mark.parametrize("flow_type", FLOWS)
def test_typed_request_runs_ticket_gate_before_state_or_admission(flow_type, runtime, monkeypatch):
    flow = flow_type()
    monkeypatch.setenv("BOOLEY_TICKET_FILE", str(runtime / "missing-ticket.md"))
    monkeypatch.setattr(
        FlowSession, "read_state", lambda self: pytest.fail("loaded mutable state")
    )
    monkeypatch.setattr(
        FlowSession, "_acquire_job_slot", lambda self: pytest.fail("admitted rejected request")
    )
    monkeypatch.setattr(flow, "_run", lambda: pytest.fail("ran rejected request"))
    result = flow.execute(flow.request_type(target="demo", work_dir=runtime))
    assert result.exit_code == 2
    assert "BLOCKED" in result.outcome.report_text


@pytest.mark.parametrize("flow_type", FLOWS)
def test_structured_and_cli_paths_preserve_acceptance_before_save(flow_type, runtime, monkeypatch):
    from booley.criteria.state import DevelopmentState
    from booley.runtime.endpoint_execution import EndpointOutcome
    from booley.ticket_board import acceptance_ledger
    from booley.ticket_board.flow_execution import TicketAcceptanceRecorder

    key = f"{flow_type.satisfies[0]}_demo"
    state_path = runtime / "state.json"
    state = DevelopmentState.load(state_path)
    state.init_criteria({key: True}, strict=True)
    state.save()
    monkeypatch.setenv("BOOLEY_STATE_FILE", str(state_path))
    monkeypatch.setenv("BOOLEY_LOGS_DIR", str(runtime / "logs"))
    events = []
    record = acceptance_ledger.record_changes
    save = DevelopmentState.save

    def record_changes(*args, **kwargs):
        result = record(*args, **kwargs)
        events.append("immutable")
        return result

    def save_state(self):
        events.append("mutable")
        return save(self)

    monkeypatch.setattr(acceptance_ledger, "record_changes", record_changes)
    monkeypatch.setattr(DevelopmentState, "save", save_state)
    outcomes = []
    recorder = TicketAcceptanceRecorder(log_dir=runtime / "logs")

    for direct in (False, True):
        # Reset the input before comparing the two paths.
        state.criteria[key].met = False
        save(state)
        events.clear()
        flow = flow_type()

        def run(flow=flow):
            flow.set_criterion(key, True, detail={"evidence": "identical"})
            return EndpointOutcome(
                criterion_key=key, criterion_met=True, detail={"evidence": "identical"}
            )

        monkeypatch.setattr(flow, "_run", run)
        if direct:
            result = flow.execute(
                flow.request_type(target="demo", work_dir=runtime),
                adapter=RecordingExecution(recorder),
            )
        else:
            result = flow.execute_cli(
                ["--target", "demo", "--work-dir", str(runtime)],
                adapter=RecordingExecution(recorder),
            )
        assert events[0:2] == ["immutable", "mutable"]
        assert result.exit_code == 0
        assert DevelopmentState.load(state_path).criteria[key].met
        outcomes.append(result.outcome)
    assert outcomes[0] == outcomes[1]
    assert list((runtime / "logs/acceptance/evidence").glob("*/record.json"))


@pytest.mark.parametrize("flow_type", FLOWS)
def test_unbound_typed_request_never_acquires_a_job(flow_type, runtime, monkeypatch):
    from booley.criteria.state import DevelopmentState

    path = runtime / "state.json"
    state = DevelopmentState.load(path)
    state.init_criteria({f"{flow_type.satisfies[0]}_other": True}, strict=True)
    state.save()
    monkeypatch.setenv("BOOLEY_STATE_FILE", str(path))
    flow = flow_type()
    monkeypatch.setattr(
        FlowSession, "_acquire_job_slot", lambda self: pytest.fail("admitted unbound request")
    )
    result = flow.execute(flow.request_type(target="demo", work_dir=runtime))
    assert result.exit_code == 2
    assert result.outcome.detail["acceptance_effect"] == "rejected_unbound"


def test_unbound_project_criterion_uses_active_endpoint_catalog(runtime, monkeypatch):
    from booley.criteria.state import DevelopmentState

    project_dir = runtime / ".booley_project"
    project_dir.mkdir()
    (project_dir / "criteria.toml").write_text(
        """[project_check]
description = "Run the Project check"
workflow_region = "pre_sim"
per_target = true
group = "verification"
""",
        encoding="utf-8",
    )
    path = runtime / "state.json"
    state = DevelopmentState.load(path)
    state.init_criteria({"project_check_other": True}, strict=True)
    state.save()
    monkeypatch.setenv("BOOLEY_STATE_FILE", str(path))
    flow = LintFlow()
    flow.satisfies = ["project_check"]
    monkeypatch.setattr(
        FlowSession, "_acquire_job_slot", lambda self: pytest.fail("admitted unbound request")
    )

    result = flow.execute(flow.request_type(target="demo", work_dir=runtime))

    assert result.exit_code == 2
    assert result.outcome.detail["acceptance_effect"] == "rejected_unbound"
    assert "project_check_other -> lint --target other" in result.outcome.report_text


@pytest.mark.parametrize("phase", ("update", "final"))
def test_acceptance_failures_keep_their_distinct_persistence_semantics(
    phase, runtime, monkeypatch
):
    from booley.criteria.state import DevelopmentState
    from booley.runtime.endpoint_execution import EndpointOutcome

    path = runtime / "state.json"
    state = DevelopmentState.load(path)
    state.init_criteria({"lint_clean_demo": True}, strict=True)
    state.save()
    monkeypatch.setenv("BOOLEY_STATE_FILE", str(path))
    flow = LintFlow()
    persisted = []
    save = DevelopmentState.save

    def save_state(self):
        persisted.append(True)
        return save(self)

    def fail(*args):
        raise RuntimeError("acceptance append failed")

    def run():
        flow.set_criterion("lint_clean_demo", True)
        return EndpointOutcome()

    monkeypatch.setattr(flow, "_run", run)
    monkeypatch.setattr(DevelopmentState, "save", save_state)
    monkeypatch.setattr(
        FlowSession, "_record_acceptance_changes" if phase == "update" else "_pre_save_hook", fail
    )
    request = flow.request_type(target="demo", work_dir=runtime)
    if phase == "update":
        result = flow.execute(request)
        assert result.exit_code == 2
        # The failed acceptance append never makes mutable state durable, and
        # standalone execution no longer persists a fallback error timeline.
        assert persisted == []
    else:
        result = flow.execute(request)
        assert result.exit_code == 2
        assert result.outcome.detail["completion_error"] == {
            "operation": "record acceptance and projections",
            "type": "RuntimeError",
            "message": "acceptance append failed",
        }
        assert len(persisted) == 1  # Only the successful in-run update; no final save.


def _assert_distinct_artifact_invocations(
    runtime: Path,
    cli_artifacts: dict[str, str],
    mcp_artifacts: dict[str, str],
) -> None:
    assert mcp_artifacts.keys() == cli_artifacts.keys()
    for name, cli_path in cli_artifacts.items():
        mcp_path = mcp_artifacts[name]
        assert mcp_path != cli_path
        assert (runtime / cli_path).is_file()
        assert (runtime / mcp_path).is_file()


def test_real_lint_child_through_mcp_matches_cli(runtime, monkeypatch):
    import asyncio
    import os
    import shutil
    import sys
    from unittest.mock import Mock

    from booley.mcp import server
    from tests.flows.lint.test_flow import _LINT_CORE_TEXT

    if os.name != "posix" or shutil.which("make") is None:
        pytest.skip("fake Verilator executable requires POSIX and make")
    (runtime / "demo.core").write_text(_LINT_CORE_TEXT)
    (runtime / "rtl").mkdir()
    (runtime / "rtl/top.sv").write_text("module top; endmodule\n")
    binaries = runtime / "bin"
    binaries.mkdir()
    verilator = binaries / "verilator"
    verilator.write_text(
        '#!/bin/sh\nif [ "$1" = "--version" ]; then echo "Verilator 5.052"; fi\nexit 0\n'
    )
    verilator.chmod(0o755)
    monkeypatch.setenv(
        "PATH",
        os.pathsep.join((str(binaries), str(Path(sys.executable).parent), os.environ["PATH"])),
    )
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).parents[2] / "src"))
    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    monkeypatch.setenv("BOOLEY_RUNTIME_DIR", str(runtime / "runtime"))
    monkeypatch.setenv("BOOLEY_LOGS_DIR", str(runtime / "logs"))
    monkeypatch.setattr(server, "_job_inline_wait_seconds", lambda: 10.0)
    cli = LintFlow().execute_cli(["--target", "lite"])
    assert cli.exit_code == 0
    cli_report = json.loads((runtime / "runtime/flow-reports/lint.json").read_text())
    assert (cli_report["criterion_key"], cli_report["criterion_met"]) == ("lint_clean_lite", True)

    async def dispatch():
        jobs = server._JobManager(Mock())
        definition = server._mcp_tool_def_from_class(
            LintFlow, flow_schema(LintFlow()), "lint", module_path="booley.flows.lint"
        )
        content = await server._dispatch_booley_mcp_tool(
            "lint", {"target": "lite"}, definition, {}, jobs
        )
        assert isinstance(content, tuple), content
        return content[1]["reports"][0]

    report = asyncio.run(asyncio.wait_for(dispatch(), timeout=20))
    assert report["exit_code"] == 0
    for key in ("flow", "target", "criterion_key", "criterion_met", "passed", "eda_tool"):
        assert report[key] == cli_report[key]
    _assert_distinct_artifact_invocations(
        runtime,
        cli_report["detail"]["artifacts"],
        report["detail"]["artifacts"],
    )


@pytest.mark.parametrize("base", ("BooleyFlow", "McpTool"))
def test_project_endpoint_constructor_schema_loader_and_execution(base, runtime):
    import sys

    from booley.harness.booley import _load_mcp_tool_class
    from booley.mcp.registry import extract_mcp_tool_info
    from booley.mcp.server import _find_mcp_tool_class_in_module

    extension = runtime / "project_endpoint.py"
    source = """from booley.flows.base import BooleyFlow
from booley.mcp.base import McpTool
from booley.runtime.endpoint_execution import EndpointOutcome
from booley.mcp.schema_extractor import extract_schema

class ProjectEndpoint(BASE):
    name = "project_probe"
    description = "Project-local extension"

    def __init__(self):
        super().__init__()
        assert self.configured == "initialized by the argument hook"

    def _add_args(self, parser):
        self.configured = "initialized by the argument hook"
        parser.add_argument("--timeout", choices=("short", "long"))

    def mcp_schema(self):
        schema = extract_schema(self._parser)
        schema["x-project-schema"] = True
        return schema

    def _run(self):
        return EndpointOutcome(detail={"configured": self.configured, "timeout": self.args.timeout, "report_dir": str(self.args.report_dir) if self.args.report_dir else None})
""".replace("BASE", base)
    extension.write_text(source)
    metadata = extract_mcp_tool_info(extension, builtin=False)
    assert metadata is not None
    cls = _load_mcp_tool_class(metadata)
    assert cls is not None
    loaded = _find_mcp_tool_class_in_module(sys.modules[cls.__module__], str(extension))
    assert loaded is not None
    _, endpoint, schema = loaded
    assert schema["x-project-schema"] is True
    assert "timeout_ms" not in schema["properties"]
    result = endpoint.execute_cli(["--target", "demo", "--timeout", "long"])
    assert result.exit_code == 0
    expected = {
        "configured": "initialized by the argument hook",
        "timeout": "long",
    }
    if base == "BooleyFlow":
        expected["report_dir"] = str(runtime / "flow-reports")
    else:
        expected["report_dir"] = None
    assert result.outcome.detail == expected


def test_ticket_runner_executes_project_local_flow(runtime, monkeypatch):
    from booley.evidence.acceptance import ResolvedFlowAcceptance
    from booley.flows.execution_persistence import NoAcceptanceRecorder
    from booley.runtime import runtime_context
    from booley.ticket_board import flow_runner

    class AllowedTicketExecution(NoAcceptanceRecorder):
        def validate_and_resolve(self, request):
            return ResolvedFlowAcceptance(ticket_backed=True)

    extension = runtime / "project_flow.py"
    extension.write_text(
        """from booley.flows.base import BooleyFlow
from booley.runtime.endpoint_execution import EndpointOutcome

class ProjectFlow(BooleyFlow):
    name = "project_probe"
    description = "Project-local Flow"

    def _add_args(self, parser):
        pass

    def _run(self):
        return EndpointOutcome(report_text="project Flow ran")
""",
        encoding="utf-8",
    )
    ticket = runtime / "ticket.md"
    ticket.write_text("ticket\n", encoding="utf-8")
    monkeypatch.setenv("BOOLEY_TICKET_FILE", str(ticket))
    monkeypatch.setattr(runtime_context, "inside_session_runtime", lambda: True)
    monkeypatch.setattr(flow_runner, "TicketBoardFlowExecution", AllowedTicketExecution)

    assert (
        flow_runner.main(
            [
                "--custom-path",
                str(extension),
                "project_probe",
                "--target",
                "demo",
                "--work-dir",
                str(runtime),
            ]
        )
        == 0
    )


def test_ticket_mcp_routes_project_local_flow_through_ticket_composition(
    runtime,
    monkeypatch,
):
    import asyncio
    from unittest.mock import Mock

    from booley.flows.base import BooleyFlow
    from booley.mcp import server

    class ProjectFlow(BooleyFlow):
        name = "project_probe"

    custom_path = runtime / "project_flow.py"
    definition = server._mcp_tool_def_from_class(
        ProjectFlow,
        {"type": "object", "properties": {}},
        "project_probe",
        is_custom=True,
        custom_path=str(custom_path),
    )
    commands = []

    async def run_subprocess(command, **_kwargs):
        commands.append(command)
        return 0, "", "", False

    monkeypatch.setenv("BOOLEY_TICKET_FILE", str(runtime / "ticket.md"))
    monkeypatch.setattr(server, "_run_subprocess", run_subprocess)
    monkeypatch.setattr(server, "_try_read_report", lambda: None)

    asyncio.run(
        server._dispatch_booley_mcp_tool(
            "project_probe",
            {},
            definition,
            {},
            server._JobManager(Mock()),
        )
    )

    assert commands == [
        [
            "python",
            "-m",
            "booley.ticket_board.flow_runner",
            "--custom-path",
            str(custom_path),
            "project_probe",
        ]
    ]


def test_repeated_typed_calls_get_fresh_invocation_metadata(runtime, monkeypatch):
    from booley.runtime.endpoint_execution import EndpointOutcome

    flow = LintFlow()
    monkeypatch.setattr(flow, "_run", EndpointOutcome)
    flow.execute_cli(["--target", "demo"])
    first_id = flow.context._invocation_id
    flow.context._eda_tool = "previous tool"
    flow.context._reserved_invocation_dir = runtime / "old-invocation"
    request = flow.request_type(target="demo", work_dir=runtime)
    flow.execute(request)
    assert flow.context._invocation_id != first_id
    assert flow.context._raw_argv is None
    assert flow.context._eda_tool is None
    assert flow.context._reserved_invocation_dir is None
    assert request.report_dir is None


@pytest.mark.parametrize("failure", (KeyboardInterrupt, RuntimeError))
def test_typed_failure_releases_admission_and_restores_stdout(failure, runtime, monkeypatch):
    import sys
    from unittest.mock import Mock

    flow = LintFlow()
    claim = object()
    store = Mock()
    monkeypatch.setattr(FlowSession, "_acquire_job_slot", lambda self: (store, claim))

    def run():
        raise failure("interrupted execution")

    monkeypatch.setattr(flow, "_run", run)
    stdout = sys.stdout
    request = flow.request_type(target="demo", work_dir=runtime)
    if failure is KeyboardInterrupt:
        with pytest.raises(KeyboardInterrupt):
            flow.execute(request)
    else:
        assert flow.execute(request).exit_code == 2
    assert sys.stdout is stdout
    store.release.assert_called_once_with(claim)


def test_flow_exception_produces_actionable_report_without_console_traceback(
    runtime, monkeypatch, capsys
):
    flow = LintFlow()

    def fail():
        raise ValueError("boom")

    monkeypatch.setattr(flow, "_run", fail)
    result = flow.execute(flow.request_type(target="demo", work_dir=runtime))

    assert result.exit_code == 2
    assert "lint failed: ValueError: boom" in result.outcome.report_text
    report_root = runtime / "flow-reports"
    report = json.loads((report_root / "lint.json").read_text(encoding="utf-8"))
    assert report["flow"] == "lint"
    assert report["report_text"] == result.outcome.report_text
    diagnostic = Path(report["report_text"].rsplit(". Diagnostic: ", 1)[1])
    assert diagnostic.is_file()
    assert report_root / "lint/1" not in diagnostic.parents
    output = capsys.readouterr()
    assert "Traceback" not in output.out + output.err


@pytest.mark.parametrize(
    "flow_type,family",
    [
        (LintFlow, "lint_clean"),
        (AsicSynthesizeFlow, "synthesis_ok"),
        (FpgaImplFlow, "fpga_impl_ok"),
        (SimulateFlow, "sim_pass"),
    ],
)
@pytest.mark.parametrize("target", ["first", "first,second"])
@pytest.mark.parametrize("met", [True, False])
def test_partial_evaluation_final_report_and_completion(
    flow_type, family, target, met, runtime, monkeypatch
):
    from booley.flows import endpoint_reporting

    events = []
    monkeypatch.setattr(endpoint_reporting, "_write_display_event", events.append)
    flow = flow_type()

    def run():
        flow.set_criterion(f"{family}_first", met)
        raise RuntimeError("second workload infrastructure failed")

    monkeypatch.setattr(flow, "_run", run)
    result = flow.execute(flow.request_type(target=target, work_dir=runtime))
    report = json.loads((runtime / "flow-reports" / f"{flow.name}.json").read_text())
    expected = (f"{family}_first", met) if target == "first" else ("", None)
    assert (report["criterion_key"], report["criterion_met"]) == expected
    assert (result.outcome.criterion_key, result.outcome.criterion_met) == expected
    assert report["passed"] is False
    assert report["exit_code"] == 2
    end = events[-1]
    assert (end["criterion_key"], end["criterion_met"]) == expected
    assert flow.state.criteria[f"{family}_first"].met is met


def test_reused_endpoint_resets_evaluation_before_early_gate(runtime, monkeypatch):
    from booley.runtime.endpoint_execution import EndpointOutcome

    flow = LintFlow()

    def run():
        flow.set_criterion("lint_clean_first", True)
        return EndpointOutcome()

    monkeypatch.setattr(flow, "_run", run)
    request = flow.request_type(target="first", work_dir=runtime)
    assert flow.execute(request).outcome.criterion_met is True
    monkeypatch.setattr(flow, "_pre_state_gate", lambda: EndpointOutcome(exit_code=2))
    result = flow.execute(request)
    assert result.outcome.criterion_key == ""
    assert result.outcome.criterion_met is None


@pytest.mark.parametrize("resume", [False, True])
def test_candidate_mapping_excludes_baseline_prerequisite(runtime, monkeypatch, resume):
    from contextlib import nullcontext
    from types import SimpleNamespace

    from booley.criteria.state import DevelopmentState
    from booley.flows.sim.flow import PreparedSimulationEndpoint
    from booley.runtime.endpoint_execution import EndpointOutcome
    from booley.targets.catalog import TargetCatalog

    (runtime / "design.core").write_text(
        "CAPI=2:\nname: acme:lib:dut:1\ntargets:\n  candidate:\n    flow: sim\n    flow_options: {tool: verilator}\n    toplevel: tb\n  baseline:\n    flow: sim\n    flow_options: {tool: verilator}\n    toplevel: tb\n"
    )
    candidate, baseline = TargetCatalog.build(runtime).select_many(
        "candidate,baseline", for_flow="sim"
    )
    state_path = runtime / "state.json"
    state = DevelopmentState.load(state_path)
    state.init_criteria({"sim_pass_candidate": True}, strict=True)
    state.save()
    monkeypatch.setenv("BOOLEY_STATE_FILE", str(state_path))
    manifest = SimpleNamespace(
        document={
            "target": {"vlnv": "acme:lib:dut:1", "name": "candidate", "selector": "candidate"},
            "required_suite": {"names": ["smoke"], "default_invocation": False},
            "work_items": [
                {"role": "candidate", "selection": {"kind": "named", "names": ["smoke"]}}
            ],
        }
    )
    validated = SimpleNamespace(
        candidate=SimpleNamespace(manifest=manifest),
        target_handles=(candidate, baseline),
        binding_for=lambda _manifest: SimpleNamespace(handle=candidate),
    )
    prepared = PreparedSimulationEndpoint(
        (candidate, baseline),
        validated if resume else None,
        () if resume else ("candidate",),
        {"candidate": ["smoke"]},
    )
    flow = SimulateFlow()
    monkeypatch.setattr(flow, "prepare_simulation_endpoint", lambda: prepared)
    monkeypatch.setattr(FlowSession, "admission", lambda *_args: nullcontext())

    def run(*_args):
        flow.set_criterion("sim_pass_candidate", True)
        return EndpointOutcome()

    monkeypatch.setattr(flow, "run_prepared_simulation", run)
    result = flow.execute(flow.request_type(target="candidate", work_dir=runtime))
    report = json.loads((runtime / "flow-reports/sim.json").read_text())
    assert result.exit_code == 0
    assert (report["criterion_key"], report["criterion_met"]) == ("sim_pass_candidate", True)


@pytest.mark.parametrize(
    "flow_type,family", [(AsicSynthesizeFlow, "synthesis_ok"), (FpgaImplFlow, "fpga_impl_ok")]
)
def test_frozen_candidate_bindings_do_not_shrink_after_partial_evaluation(
    flow_type, family, runtime, monkeypatch
):
    from booley.criteria.state import DevelopmentState
    from booley.runtime.endpoint_execution import EndpointOutcome

    state_path = runtime / "state.json"
    state = DevelopmentState.load(state_path)
    state.init_criteria(
        {f"{family}_first": True, f"{family}_second": True},
        flow_key_aliases={f"{family}_demo": [f"{family}_first", f"{family}_second"]},
        strict=True,
    )
    state.save()
    monkeypatch.setenv("BOOLEY_STATE_FILE", str(state_path))
    flow = flow_type()

    def run():
        flow.set_criterion(f"{family}_first", False)
        return EndpointOutcome(exit_code=2)

    monkeypatch.setattr(flow, "_run", run)
    result = flow.execute(flow.request_type(target="demo", work_dir=runtime))
    report = json.loads((runtime / "flow-reports" / f"{flow.name}.json").read_text())
    assert result.exit_code == 2
    assert (report["criterion_key"], report["criterion_met"]) == ("", None)


def test_override_freezes_parameter_bound_candidate_criteria(runtime, monkeypatch):
    from booley.criteria.state import DevelopmentState
    from booley.runtime.endpoint_execution import EndpointOutcome

    state_path = runtime / "state.json"
    state = DevelopmentState.load(state_path)
    state.init_criteria(
        {"sim_pass_candidate": True, "cycle_count_policy": True},
        criterion_params={
            "cycle_count_policy": {
                "target": "acme:lib:dut:1#candidate",
                "_target_selector": "candidate",
                "test": "smoke",
                "cycle_count_max": 100,
            }
        },
        strict=True,
    )
    state.save()
    monkeypatch.setenv("BOOLEY_STATE_FILE", str(state_path))
    flow = SimulateFlow()

    def run():
        flow.set_criterion("sim_pass_candidate", True)
        return EndpointOutcome(exit_code=2)

    monkeypatch.setattr(flow, "_run", run)
    result = flow.execute(flow.request_type(target="candidate", work_dir=runtime))
    report = json.loads((runtime / "flow-reports/sim.json").read_text())
    assert result.exit_code == 2
    assert (report["criterion_key"], report["criterion_met"]) == ("", None)
    assert flow.state.criteria["sim_pass_candidate"].met is True


@pytest.mark.parametrize(
    "flow_type,family,eda",
    [(AsicSynthesizeFlow, "synthesis_ok", "yosys"), (FpgaImplFlow, "fpga_impl_ok", "vivado")],
)
def test_single_basis_binding_uses_effective_key_and_ignores_baseline_only_binding(
    flow_type, family, eda, runtime, monkeypatch
):
    from booley.criteria.state import DevelopmentState
    from booley.evidence.acceptance import AcceptanceTargetBinding, ResolvedFlowAcceptance
    from booley.runtime.endpoint_execution import EndpointOutcome

    (runtime / "design.core").write_text(
        f"CAPI=2:\nname: acme:lib:dut:1\ntargets:\n  demo:\n    default_tool: {eda}\n    toplevel: dut\n"
    )
    key = f"{family}_demo"
    other = f"{family}_other"
    state_path = runtime / "state.json"
    state = DevelopmentState.load(state_path)
    state.init_criteria(
        {key: True, other: True},
        criterion_params={
            key: {"target": "acme:lib:dut:1#demo", "_target_selector": "demo"},
            other: {"target": "acme:lib:dut:1#other", "_target_selector": "other"},
        },
        strict=True,
    )
    state.save()
    monkeypatch.setenv("BOOLEY_STATE_FILE", str(state_path))
    flow = flow_type()
    bindings = (
        AcceptanceTargetBinding(
            flow.name,
            f"criteria.mandatory.{key}",
            "acme:lib:dut:1#base",
            "acme:lib:dut:1#demo",
            "base",
            "demo",
        ),
        AcceptanceTargetBinding(
            flow.name,
            f"criteria.mandatory.{other}",
            "acme:lib:dut:1#demo",
            "acme:lib:dut:1#other",
            "demo",
            "other",
        ),
    )

    class BoundExecution(StandaloneFlowExecution):
        def validate_and_resolve(self, _request):
            return ResolvedFlowAcceptance(bindings)

    def run(self):
        self.set_criterion(key, True)
        return EndpointOutcome()

    monkeypatch.setattr(flow_type, "_run", run)
    result = flow.execute(
        flow.request_type(target="demo", work_dir=runtime), adapter=BoundExecution()
    )
    report = json.loads((runtime / "flow-reports" / f"{flow.name}.json").read_text())
    assert result.exit_code == 0
    assert (report["criterion_key"], report["criterion_met"]) == (key, True)


def _report_targets(root, names=("first", "second"), flow="sim"):
    tools = {"lint": "verible", "synth": "yosys", "fpga": "vivado"}
    declaration = (
        "flow: sim\n    flow_options: {tool: verilator}"
        if flow == "sim"
        else f"default_tool: {tools[flow]}"
    )
    entries = "".join(f"  {name}:\n    {declaration}\n    toplevel: dut\n" for name in names)
    (root / "design.core").write_text("CAPI=2:\nname: acme:lib:dut:1\ntargets:\n" + entries)


@pytest.mark.parametrize(
    "flow_type,family",
    [
        (LintFlow, "lint_clean"),
        (AsicSynthesizeFlow, "synthesis_ok"),
        (FpgaImplFlow, "fpga_impl_ok"),
        (SimulateFlow, "sim_pass"),
    ],
)
@pytest.mark.parametrize("met", [True, False])
def test_prepared_identity_partial_report_is_null(flow_type, family, met, runtime, monkeypatch):
    from booley.runtime.endpoint_execution import EndpointOutcome

    _report_targets(runtime, flow=flow_type.name)
    flow = flow_type()
    if flow_type is SimulateFlow:
        monkeypatch.setattr(flow_type, "prepare_simulation_endpoint", lambda self: None)

    def run(self):
        self.set_criterion(f"{family}_first", met)
        return EndpointOutcome(exit_code=2, report_text="second workload unavailable")

    monkeypatch.setattr(flow_type, "_run", run)
    result = flow.execute(flow.request_type(target="first,second", work_dir=runtime))
    report = json.loads((runtime / "flow-reports" / f"{flow.name}.json").read_text())
    assert result.exit_code == 2
    assert (report["criterion_key"], report["criterion_met"]) == ("", None)
    identities = frozenset({"acme:lib:dut:1#first", "acme:lib:dut:1#second"})
    assert self_scope(flow) == identities
    assert flow.context._report_criteria.targets == identities
    assert flow.state.criteria[f"{family}_first"].met is met


def self_scope(flow):
    return {handle.identity for handle in flow._selected_target_handles()}


def _report_campaign_document(handle, inspection, preview, mode, selected):
    from dataclasses import replace

    from tests.flows.sim.test_campaign_flow_planning import _plan

    if mode in {"unfiltered", "coverage"}:
        from booley.flows.sim.campaign.flow_planning import plan_coarse_simulation_campaign

        inspection = replace(
            inspection, flow_options={"cocotb_module": "tests"} if mode == "unfiltered" else {}
        )
        if mode == "coverage":
            preview = replace(preview, groups=(selected,), commands=(("run",),))
        document = plan_coarse_simulation_campaign(
            handle=handle,
            inspection=inspection,
            preview=preview,
            selected_tests=selected if mode == "coverage" else (),
            required_suite=selected,
            revision="abc123",
            invocation_id=1,
            execution_id="",
            trace=False,
            kind="coverage_aggregate" if mode == "coverage" else "cocotb_batch",
            required_suite_catalog_backed=True,
        ).manifest.document
    else:
        document = _plan(handle, inspection, preview, selected, catalog_backed=mode != "default")
    return document


def _prepared_report_campaign(runtime, mode, resume):
    from dataclasses import replace

    from booley.flows.sim.campaign.codec import (
        decode_simulation_campaign_manifest,
        encode_simulation_campaign_manifest,
    )
    from booley.flows.sim.campaign.model import SimulationCampaignManifest
    from booley.flows.sim.campaign.resume import ValidatedManifestNode, ValidatedResumeManifest
    from booley.flows.sim.flow import PreparedSimulationEndpoint
    from booley.targets.catalog import TargetCatalog
    from tests.flows.sim.test_campaign_flow_planning import _planning_fixture

    project, handle, inspection, preview = _planning_fixture(runtime)
    (runtime / "design.core").write_text(
        "CAPI=2:\nname: acme:lib:dut:1\ntargets:\n  sim:\n    flow: sim\n    flow_options: {tool: icarus}\n    toplevel: tb\n"
    )
    handle = TargetCatalog.build(runtime).select("sim", for_flow="sim")
    inspection = replace(inspection, handle=handle)
    selected = ("reset", "count") if mode != "default" else ()
    if mode in {"default", "unfiltered"}:
        preview = replace(preview, groups=((),), commands=(("run",),))
    document = _report_campaign_document(handle, inspection, preview, mode, selected)
    manifest = decode_simulation_campaign_manifest(
        encode_simulation_campaign_manifest(SimulationCampaignManifest(document))
    )
    # This is a codec-validated production manifest, not a stub with item.test.
    suite = ("reset", "count") if mode != "default" else ()
    cycle_test = "reset" if mode != "default" else "default"
    node = ValidatedManifestNode(project / "manifest.json", manifest, "unused")
    validated = ValidatedResumeManifest(node, (), (handle,))
    prepared = PreparedSimulationEndpoint(
        (handle,), validated if resume else None, () if resume else ("sim",), {"sim": list(suite)}
    )
    return prepared, handle, cycle_test


def _report_campaign_state(runtime, handle, cycle_test, mode, missing_cycle):
    from booley.criteria.state import DevelopmentState

    state_path = runtime / "state.json"
    state = DevelopmentState.load(state_path)
    state.init_criteria(
        {"sim_pass_sim": True, "cycle_count_policy": True},
        criterion_params={
            "cycle_count_policy": {
                "target": handle.identity,
                "_target_selector": "sim",
                "test": cycle_test,
                "cycle_count_max": 100,
            }
        },
        strict=True,
    )
    if mode == "coverage":
        state.init_criteria(
            {"sim_pass_sim": True, "cycle_count_policy": True, "coverage_sim": True},
            strict=True,
            criterion_params={
                "cycle_count_policy": {
                    "target": handle.identity,
                    "_target_selector": "sim",
                    "test": cycle_test,
                    "cycle_count_max": 100,
                }
            },
        )
    if missing_cycle:
        del state.criteria["cycle_count_policy"]
    state.save()
    return state_path


@pytest.mark.parametrize("mode", ["named", "default", "unfiltered", "coverage"])
@pytest.mark.parametrize("resume", [False, True])
@pytest.mark.parametrize("missing_cycle", [False, True])
def test_prepared_campaign_plural_mapping_survives_partial_evaluation(
    runtime, monkeypatch, mode, resume, missing_cycle
):
    from booley.runtime.endpoint_execution import EndpointOutcome

    prepared, handle, cycle_test = _prepared_report_campaign(runtime, mode, resume)
    state_path = _report_campaign_state(runtime, handle, cycle_test, mode, missing_cycle)
    monkeypatch.setenv("BOOLEY_STATE_FILE", str(state_path))
    monkeypatch.setattr(SimulateFlow, "prepare_simulation_endpoint", lambda self: prepared)

    def run(self, *_args):
        self.set_criterion("sim_pass_sim", True)
        return EndpointOutcome(exit_code=2, report_text="cycle workload unavailable")

    monkeypatch.setattr(SimulateFlow, "run_prepared_simulation", run)
    flow = SimulateFlow()
    from booley.evidence.acceptance import AcceptanceTargetBinding, ResolvedFlowAcceptance

    class BoundExecution(StandaloneFlowExecution):
        def validate_and_resolve(self, _request):
            if not missing_cycle:
                return super().validate_and_resolve(_request)
            return ResolvedFlowAcceptance(
                (
                    AcceptanceTargetBinding(
                        "sim",
                        "criteria.mandatory.cycle_count_policy",
                        handle.identity,
                        handle.identity,
                        "sim",
                        "sim",
                    ),
                )
            )

    result = flow.execute(
        flow.request_type(target="sim", work_dir=runtime, coverage=mode == "coverage"),
        adapter=BoundExecution(),
    )
    assert result.exit_code == 2, result.outcome.report_text
    report = json.loads((flow.context.args.report_dir / "sim.json").read_text())
    assert result.exit_code == 2
    assert (report["criterion_key"], report["criterion_met"]) == ("", None)


@pytest.mark.parametrize("endpoint_kind", ["flow", "specialist"])
@pytest.mark.parametrize("catalog", [True, False])
@pytest.mark.parametrize("target", ["first", "first,second"])
def test_declared_custom_flow_prepared_headline(
    runtime, monkeypatch, endpoint_kind, catalog, target
):
    from typing import ClassVar

    from booley.flows.base import BooleyFlow
    from booley.runtime.endpoint_execution import EndpointOutcome

    if catalog:
        _report_targets(runtime)

    from booley.specialists.specialist import Specialist

    base = BooleyFlow if endpoint_kind == "flow" else Specialist

    class DrcFlow(base):
        name = "drc_check"

        def _build_prompt(self):
            return "unused"

        def _interpret_output(self, output, structured):
            raise AssertionError("agent not invoked")

        satisfies: ClassVar[list[str]] = ["drc_clean"]

        def _add_args(self, parser):
            pass

        def _run(self):
            self.set_criterion("drc_clean_first", True)
            return EndpointOutcome()

    flow = DrcFlow()
    result = flow.execute_cli(["--target", target, "--work-dir", str(runtime)])
    expected = ("drc_clean_first", True) if target == "first" else ("", None)
    assert result.exit_code == 0
    assert (result.outcome.criterion_key, result.outcome.criterion_met) == expected


@pytest.mark.parametrize("ticket", ["none", "nonstrict", "strict"])
@pytest.mark.parametrize("calls", ["both", "aggregate", "elab"])
def test_combined_prepared_mode_freezes_both_eligible_families(
    runtime, monkeypatch, ticket, calls
):
    from booley.criteria.state import DevelopmentState

    _report_targets(runtime, ("first",))
    if ticket != "none":
        path = runtime / "state.json"
        state = DevelopmentState.load(path)
        state.init_criteria({"elaborate_standalone": True}, strict=ticket == "strict")
        state.save()
        monkeypatch.setenv("BOOLEY_STATE_FILE", str(path))

    from booley.flows.sim.build import BuildOutcome
    from booley.flows.sim.flow import ElabOnlyTargetResult
    from booley.flows.sim.standalone import _StandaloneOutcome

    def build(self, target):
        verdict = None if calls == "aggregate" else "pass"
        return ElabOnlyTargetResult(
            target,
            outcome=BuildOutcome(True, verdict, "infrastructure" if verdict is None else None),
        )

    def sweep(self, targets, **kwargs):
        if calls == "elab":
            raise OSError("module sweep infrastructure failed")
        self.set_criterion("elaborate_standalone", True)
        return _StandaloneOutcome(passed=True)

    monkeypatch.setattr(SimulateFlow, "_elab_only_preflight", lambda self: ["first"])
    monkeypatch.setattr(SimulateFlow, "_run_one_elab_only", build)
    monkeypatch.setattr(SimulateFlow, "_run_standalone_check", sweep)
    flow = SimulateFlow()
    result = flow.execute(
        flow.request_type(target="first", work_dir=runtime, mode="elab_only_standalone")
    )
    expected = (
        ("elaborate_standalone", True) if ticket == "strict" and calls != "elab" else ("", None)
    )
    assert (result.outcome.criterion_key, result.outcome.criterion_met) == expected


@pytest.mark.parametrize("name", ["mutation_tester", "coverage_analyst", "tb_coder"])
def test_specialist_public_completion_projects_actual_evaluations(runtime, monkeypatch, name):
    from booley.mcp.base import McpToolResult
    from booley.specialists.coverage_analyst import CoverageAnalystSpecialist
    from booley.specialists.mutation_tester import MutationTesterSpecialist
    from booley.specialists.tb_coder import TbCoderSpecialist

    _report_targets(runtime, ("first",))
    classes = {
        "mutation_tester": MutationTesterSpecialist,
        "coverage_analyst": CoverageAnalystSpecialist,
        "tb_coder": TbCoderSpecialist,
    }
    endpoint_type = classes[name]
    extras = {
        "mutation_tester": ["--target", "first", "--scope", "rtl"],
        "coverage_analyst": ["--campaign", str(runtime / "coverage.json")],
        "tb_coder": ["--instruction-file", str(runtime / "instruction.md"), "--scope", "tb"],
    }[name]

    def run(self):
        if name == "mutation_tester":
            self.set_criterion("mutation_score_first", False)
        return McpToolResult(exit_code=1, criterion_key="stale", criterion_met=True)

    monkeypatch.setattr(endpoint_type, "_run", run)
    result = endpoint_type().execute_cli(
        ["--work-dir", str(runtime), "--report-dir", str(runtime / "reports"), *extras]
    )
    expected = ("mutation_score_first", False) if name == "mutation_tester" else ("", None)
    report = json.loads((runtime / "reports" / f"{name}.json").read_text())
    assert (result.outcome.criterion_key, result.outcome.criterion_met) == expected
    assert (report["criterion_key"], report["criterion_met"]) == expected


@pytest.mark.parametrize("mode", ["dry_run", "diagnostic"])
def test_prepared_lint_without_evaluation_has_null_completion(runtime, monkeypatch, mode):
    from booley.flows import endpoint_reporting
    from booley.flows.base import SubprocessResult

    _report_targets(runtime, ("first",), flow="lint")
    events = []
    monkeypatch.setattr(endpoint_reporting, "_write_display_event", events.append)
    monkeypatch.setattr(
        LintFlow, "_execute", lambda *_args, **_kwargs: SubprocessResult(returncode=0)
    )
    flow = LintFlow()
    result = flow.execute(flow.request_type(target="first", work_dir=runtime, **{mode: True}))
    assert (result.outcome.criterion_key, result.outcome.criterion_met) == ("", None)
    assert (events[-1]["criterion_key"], events[-1]["criterion_met"]) == ("", None)


def test_public_hooks_see_raw_then_projected_headline(runtime, monkeypatch):
    from booley.runtime.endpoint_execution import EndpointOutcome

    _report_targets(runtime, ("first",), flow="lint")
    observed = []

    def run(self):
        self.set_criterion("lint_clean_first", True)
        return EndpointOutcome(criterion_key="producer_raw", criterion_met=False)

    from booley.flows.endpoint_state import EndpointState

    original_record = EndpointState.record_acceptance
    original_post = EndpointState._post_run

    def record(self, prepared, outcome):
        observed.append(("acceptance", outcome.criterion_key, outcome.criterion_met))
        return original_record(self, prepared, outcome)

    def post(self, outcome, duration):
        observed.append(("post", outcome.criterion_key, outcome.criterion_met))
        return original_post(self, outcome, duration)

    monkeypatch.setattr(LintFlow, "_run", run)
    monkeypatch.setattr(EndpointState, "record_acceptance", record)
    monkeypatch.setattr(EndpointState, "_post_run", post)
    result = LintFlow().execute(LintFlow.request_type(target="first", work_dir=runtime))
    assert observed == [("acceptance", "producer_raw", False), ("post", "lint_clean_first", True)]
    assert (result.outcome.criterion_key, result.outcome.criterion_met) == (
        "lint_clean_first",
        True,
    )

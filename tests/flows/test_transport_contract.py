"""Compatibility evidence captured before separating Flow and MCP ownership."""

import json
from pathlib import Path

import pytest

from booley.flows.execution_persistence import StandaloneFlowExecution
from booley.flows.flow_session import FlowSession
from booley.flows.fpga.flow import FpgaImplFlow
from booley.flows.lint.flow import LintFlow
from booley.flows.sim.flow import SimulateFlow
from booley.flows.synth.flow import AsicSynthesizeFlow
from booley.mcp.flow_adapter import flow_schema

FLOWS = (LintFlow, SimulateFlow, AsicSynthesizeFlow, FpgaImplFlow)


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
        assert len(persisted) == 1  # Error completion still persists, after the failed update.
    else:
        with pytest.raises(RuntimeError, match="acceptance append failed"):
            flow.execute(request)
        assert len(persisted) == 1  # Only the successful in-run update; no final save.


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
    assert report["detail"]["artifacts"] == cli_report["detail"]["artifacts"]


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
        return EndpointOutcome(detail={"configured": self.configured, "timeout": self.args.timeout})
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
    assert result.outcome.detail == {
        "configured": "initialized by the argument hook",
        "timeout": "long",
    }


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

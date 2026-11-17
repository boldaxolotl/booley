"""Compatibility evidence captured before separating Flow and MCP ownership."""

import json
import logging
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from booley.flows.base import BuiltinFlow
from booley.flows.fpga.flow import FpgaImplFlow
from booley.flows.lint.flow import LintFlow
from booley.flows.mutation.flow import MutationFlow
from booley.flows.sim.flow import SimulateFlow
from booley.flows.sim.verilator_identity import PINNED_VERILATOR
from booley.flows.synth.flow import AsicSynthesizeFlow
from booley.mcp.flow_adapter import flow_schema
from tests.flows.invocation_doubles import replace_run
from tests.flows.report_headline import assert_paired_headline

FLOWS = (LintFlow, SimulateFlow, AsicSynthesizeFlow, FpgaImplFlow)
# The Flows that record Criteria; the Simulation Flow records none.
CRITERION_FLOWS = (LintFlow, AsicSynthesizeFlow, FpgaImplFlow)


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


def test_complete_builtin_schema_is_compatible():
    expected = json.loads((Path(__file__).parent / "fixtures/builtin_schemas.json").read_text())
    for flow_type in FLOWS:
        flow = flow_type()
        assert flow_schema(flow) == expected[flow.name], flow.name


@pytest.fixture
def runtime(monkeypatch, tmp_path):
    from booley.flows import endpoint_admission
    from booley.runtime import runtime_context

    monkeypatch.setattr(runtime_context, "inside_session_runtime", lambda: True)
    # Seat Caps are covered by the admission tests; these tests are about transport.
    monkeypatch.setattr(endpoint_admission, "required_seats", lambda _tools, _config: ())
    for name in (
        "BOOLEY_STATE_FILE",
        "BOOLEY_TICKET_FILE",
        "BOOLEY_LOGS_DIR",
        "BOOLEY_RUNTIME_DIR",
    ):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def test_cli_rejects_the_retired_diagnostic_option(capsys):
    from booley.flows.builtin_cli import build_parser

    for flow_type in (*FLOWS, MutationFlow):
        parser = build_parser(flow_type(), human=True)
        with pytest.raises(SystemExit) as error:
            parser.parse_args(["--target", "demo", "--diagnostic"])
        assert error.value.code == 2
        assert "error: unrecognized arguments: --diagnostic" in capsys.readouterr().err


@pytest.mark.parametrize("flow_type", FLOWS)
def test_typed_request_executes_without_constructing_a_parser(flow_type, runtime, monkeypatch):
    import argparse

    from booley.runtime.endpoint_execution import EndpointOutcome

    flow = flow_type()
    request = flow.request_type(target="demo", work_dir=runtime)
    monkeypatch.setattr(
        argparse, "ArgumentParser", lambda **kwargs: pytest.fail("parser constructed")
    )
    replace_run(monkeypatch, flow, lambda: EndpointOutcome(detail={"evidence": "same"}))
    result = flow.execute(request)
    assert result.exit_code == 0
    assert result.outcome.detail["evidence"] == "same"
    assert "acceptance_effect" not in result.outcome.detail
    assert not hasattr(flow, "_cli_parser")
    assert request.report_dir is None
    report_root = runtime / "flow-reports"
    assert flow.args.report_dir == report_root
    assert (report_root / f"{flow.name}.json").is_file()
    assert (report_root / flow.name / "1/report.json").is_file()


def test_flow_report_root_precedence(runtime, monkeypatch):
    from booley.runtime.endpoint_execution import EndpointOutcome

    flow = LintFlow()
    replace_run(monkeypatch, flow, EndpointOutcome)
    explicit = runtime / "explicit"
    monkeypatch.setenv("BOOLEY_RUNTIME_DIR", str(runtime / "runtime"))

    flow.execute(flow.request_type(target="demo", work_dir=runtime, report_dir=explicit))
    assert flow.args.report_dir == explicit

    flow.execute(flow.request_type(target="demo", work_dir=runtime))
    assert flow.args.report_dir == runtime / "runtime/flow-reports"

    monkeypatch.delenv("BOOLEY_RUNTIME_DIR")
    flow.execute(flow.request_type(target="demo", work_dir=runtime))
    assert flow.args.report_dir == runtime / "flow-reports"


def test_direct_simulation_invocations_share_report_numbering(runtime, monkeypatch):
    from booley.runtime.endpoint_execution import EndpointOutcome

    reserved = []
    monkeypatch.setattr(
        SimulateFlow, "prepare_endpoint_invocation", BuiltinFlow.prepare_endpoint_invocation
    )
    monkeypatch.setattr(SimulateFlow, "prepare_target_endpoint", lambda self: None)

    def run(session):
        reserved.append(session.reserve_invocation_dir())
        return EndpointOutcome()

    monkeypatch.setattr(SimulateFlow, "_run", run)
    for _ in range(3):
        flow = SimulateFlow()
        result = flow.execute(flow.request_type(target="demo", work_dir=runtime))
        assert result.exit_code == 0

    report_root = runtime / "flow-reports/sim"
    assert reserved == [report_root / "1", report_root / "2", report_root / "3"]


def test_uninitialized_flow_fails_before_execution(runtime, monkeypatch):
    from booley.runtime.booley_dir import reset_cache

    uninitialized = runtime / "uninitialized"
    uninitialized.mkdir()
    monkeypatch.chdir(uninitialized)
    monkeypatch.delenv("BOOLEY_DIR")
    reset_cache()
    flow = LintFlow()
    replace_run(monkeypatch, flow, lambda: pytest.fail("executed uninitialized Flow"))

    result = flow.execute(flow.request_type(target="demo", work_dir=uninitialized))

    assert result.exit_code == 2
    assert "Run 'booley init'" in result.outcome.report_text
    assert not (uninitialized / "flow-reports").exists()


def test_hand_set_state_file_neither_refuses_nor_records(runtime, monkeypatch):
    """A state file naming only another Target is neither read, refused on, nor written."""
    from booley.runtime.endpoint_execution import EndpointOutcome

    path = runtime / "state.json"
    path.write_bytes(b'{"opaque": true}\n')
    before = path.read_bytes()
    monkeypatch.setenv("BOOLEY_STATE_FILE", str(path))
    monkeypatch.setenv("BOOLEY_LOGS_DIR", str(runtime / "logs"))

    for flow_type in (*CRITERION_FLOWS, MutationFlow):
        for exit_code in (0, 1, 2):
            flow = flow_type()
            replace_run(
                monkeypatch,
                flow,
                lambda exit_code=exit_code: EndpointOutcome(
                    exit_code=exit_code, report_text="ran"
                ),
            )

            result = flow.execute(flow.request_type(target="demo", work_dir=runtime))

            assert result.outcome.report_text == "ran", (flow.name, exit_code)
            assert result.exit_code == exit_code, (flow.name, exit_code)
            assert path.read_bytes() == before
            assert not (runtime / "logs").exists()


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
    from unittest.mock import Mock

    from booley.mcp import catalog, server
    from tests.flows.lint.test_flow import _LINT_CORE_TEXT
    from tests.python_command_support import python_path_entry

    if os.name != "posix" or shutil.which("make") is None:
        pytest.skip("fake Verilator executable requires POSIX and make")
    (runtime / "demo.core").write_text(_LINT_CORE_TEXT)
    (runtime / "rtl").mkdir()
    (runtime / "rtl/top.sv").write_text("module top; endmodule\n")
    binaries = runtime / "bin"
    binaries.mkdir()
    verilator = binaries / "verilator"
    pinned_number = PINNED_VERILATOR.tag.removeprefix("v")
    verilator.write_text(
        f'#!/bin/sh\nif [ "$1" = "--version" ]; then echo "Verilator {pinned_number}"; fi\nexit 0\n'
    )
    verilator.chmod(0o755)
    monkeypatch.setenv(
        "PATH",
        os.pathsep.join(
            (str(binaries), python_path_entry(runtime / "python-shim"), os.environ["PATH"])
        ),
    )
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).parents[2] / "src"))
    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    monkeypatch.setenv("BOOLEY_RUNTIME_DIR", str(runtime / "runtime"))
    monkeypatch.setenv("BOOLEY_LOGS_DIR", str(runtime / "logs"))
    monkeypatch.setattr(server, "_job_inline_wait_seconds", lambda: 600.0)
    cli = LintFlow().execute_cli(["--target", "lite"])
    assert cli.exit_code == 0
    cli_report = json.loads((runtime / "runtime/flow-reports/lint.json").read_text())
    assert "criterion_key" not in cli_report and "criterion_met" not in cli_report

    async def dispatch():
        jobs = server._JobManager(Mock())
        definition = catalog._mcp_tool_def_from_endpoint(
            LintFlow(), flow_schema(LintFlow()), "lint", module_path="booley.flows.lint"
        )
        content = await server._dispatch_booley_mcp_tool(
            "lint", {"target": "lite"}, definition, {}, jobs
        )
        assert isinstance(content, tuple), content
        return content[1]["reports"][0]

    # No wall-clock budget here: the child is a real Python process whose start-up
    # is CPU-bound, so a fixed cap fails on a loaded host (a busy xdist run took
    # more than 20 s for a ~3 s child). A hang is still bounded by the pytest
    # timeout and the job supervisor's own timeout.
    report = asyncio.run(dispatch())
    assert report["exit_code"] == 0
    assert "criterion_key" not in report and "criterion_met" not in report
    for key in ("flow", "target", "passed", "eda_tool"):
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
    from booley.mcp.catalog import _find_mcp_tool_class_in_module
    from booley.mcp.registry import extract_mcp_tool_info

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


def test_repeated_typed_calls_get_fresh_invocation_metadata(runtime, monkeypatch):
    from booley.runtime.endpoint_execution import EndpointOutcome

    flow = LintFlow()
    replace_run(monkeypatch, flow, EndpointOutcome)
    flow.execute_cli(["--target", "demo"])
    first_id = flow._invocation_id
    flow._eda_tool = "previous tool"
    flow._reserved_invocation_dir = runtime / "old-invocation"
    request = flow.request_type(target="demo", work_dir=runtime)
    flow.execute(request)
    assert flow._invocation_id != first_id
    assert flow._raw_argv is None
    assert flow._eda_tool is None
    assert flow._reserved_invocation_dir is None
    assert request.report_dir is None


@pytest.mark.parametrize("failure", (KeyboardInterrupt, RuntimeError))
def test_typed_failure_releases_admission_and_restores_stdout(failure, runtime, monkeypatch):
    import sys
    from unittest.mock import Mock

    from booley.flows import endpoint_admission

    flow = LintFlow()
    claim = object()
    store = Mock()
    monkeypatch.setattr(
        endpoint_admission,
        "_acquire_memory_reservation",
        lambda _endpoint, _prepared, *, requeued=False: endpoint_admission._Claim(store, claim),
    )

    def run():
        raise failure("interrupted execution")

    replace_run(monkeypatch, flow, run)
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

    replace_run(monkeypatch, flow, fail)
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


def _display_events(monkeypatch):
    """Capture every event the endpoint emits; return the live list."""
    events = []
    monkeypatch.setattr("booley.flows.endpoint_session._write_display_event", events.append)
    monkeypatch.setattr("booley.flows.endpoint_reporting._write_display_event", events.append)
    return events


def _report_targets(root, names=("first", "second"), flow="sim"):
    tools = {"lint": "verible", "synth": "yosys", "fpga": "vivado"}
    declaration = (
        "flow: sim\n    flow_options: {tool: verilator}"
        if flow == "sim"
        else f"default_tool: {tools[flow]}"
    )
    entries = "".join(f"  {name}:\n    {declaration}\n    toplevel: dut\n" for name in names)
    (root / "design.core").write_text("CAPI=2:\nname: acme:lib:dut:1\ntargets:\n" + entries)


@pytest.mark.parametrize("name", ["mutation", "coverage_analyst"])
def test_mutation_flow_and_specialist_reports_carry_no_criterion(runtime, monkeypatch, name):
    from booley.flows.mutation.flow import MutationFlow
    from booley.mcp.base import McpToolResult
    from booley.specialists.coverage_analyst import CoverageAnalystSpecialist

    _report_targets(runtime, ("first",))
    classes = {
        "mutation": MutationFlow,
        "coverage_analyst": CoverageAnalystSpecialist,
    }
    endpoint_type = classes[name]
    extras = {
        "mutation": ["--target", "first", "--scope", "rtl"],
        "coverage_analyst": ["--record", str(runtime / "coverage.json")],
    }[name]

    def run(self):
        if name == "mutation":
            self.set_criterion("mutation_score_first", False)
        return McpToolResult(exit_code=1)

    monkeypatch.setattr(endpoint_type, "_run", run)
    events = _display_events(monkeypatch)
    result = endpoint_type().execute_cli(
        ["--work-dir", str(runtime), "--report-dir", str(runtime / "reports"), *extras]
    )
    assert not hasattr(result.outcome, "criterion_key")
    assert_paired_headline(runtime / "reports", name)
    (end,) = (event for event in events if event["type"] == "endpoint_end")
    assert "criterion_key" not in end and "criterion_met" not in end


def test_sim_verbose_mcp_schema_deliberately_exposes_optional_boolean():
    schema = flow_schema(SimulateFlow())
    assert schema["properties"]["verbose"] == {
        "type": "boolean",
        "default": False,
        "description": "Include full Simulation Record resume mismatch pointers and values",
    }
    assert "verbose" not in schema.get("required", [])
    assert "verbose" not in flow_schema(LintFlow())["properties"]


def test_completion_event_carries_no_criterion_fields():
    from booley.flows.endpoint_events import _endpoint_end_event
    from booley.runtime.endpoint_execution import EndpointOutcome

    event = _endpoint_end_event("generic", None, EndpointOutcome(), 0.0)
    assert "criterion_key" not in event
    assert "criterion_met" not in event

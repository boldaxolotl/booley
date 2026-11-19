"""Compatibility evidence captured before separating Flow and MCP ownership."""

import json
import logging
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from booley.flows.fpga.flow import FpgaImplFlow
from booley.flows.lint.flow import LintFlow
from booley.flows.sim.flow import SimulateFlow
from booley.flows.sim.verilator_identity import PINNED_VERILATOR
from booley.flows.synth.flow import AsicSynthesizeFlow
from booley.mcp.flow_adapter import flow_schema
from tests.flows.invocation_doubles import replace_run
from tests.flows.report_headline import assert_paired_headline
from tests.time_budget import hang_guard_s

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
        "BOOLEY_TICKET_FILE",
        "BOOLEY_LOGS_DIR",
        "BOOLEY_RUNTIME_DIR",
    ):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


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
    assert request.report_dir is None
    report_root = runtime / "flow-reports"
    assert flow.args.report_dir == report_root
    assert (report_root / f"{flow.name}.json").is_file()
    assert (report_root / flow.name / "1/report.json").is_file()


@pytest.mark.parametrize(
    ("case", "publishes", "dry_run"),
    [
        ("checkout-publishes", True, False),
        ("checkout-returns", False, False),
        ("checkout-dry-run", False, True),
        ("booley-dir-publishes", True, False),
    ],
)
def test_flow_fails_naming_its_removed_root(
    case, publishes, dry_run, runtime, monkeypatch, capsys
):
    from booley.core.booley_dir import reset_cache
    from booley.runtime.endpoint_execution import EndpointOutcome
    from booley.runtime.filesystem_utils import safe_rmtree
    from booley.targets.selection import UnknownTargetError

    if case == "booley-dir-publishes":
        removed = runtime / "bd"
        removed.mkdir()
        monkeypatch.setenv("BOOLEY_DIR", str(removed))
        reset_cache()
        work_dir, report_dir = runtime, None
        expected = f"lint failed: Booley Directory {removed} was removed during the run"
    else:
        removed = work_dir = runtime / "checkout"
        (work_dir / ".booley").mkdir(parents=True)
        report_dir = work_dir / ".booley" / "flow-reports"
        expected = f"lint failed: checkout {removed} was removed during the run"
    flow = LintFlow()

    def run():
        safe_rmtree(removed)
        if publishes:
            flow.reserve_invocation_dir()
            raise UnknownTargetError("Unknown target 'demo'; selectable Targets: (none authored)")
        return EndpointOutcome()

    replace_run(monkeypatch, flow, run)
    request = flow.request_type(
        target="demo", work_dir=work_dir, report_dir=report_dir, dry_run=dry_run
    )
    result = flow.execute(request)

    assert result.outcome.report_text == expected
    assert result.exit_code == 2
    assert not removed.exists()
    if publishes:
        err = capsys.readouterr().err
        assert "Traceback" in err
        assert "RemovedDirectoryError" in err


def _flow_removing(monkeypatch, removed, *, raises):
    """A lint Flow whose run deletes *removed* and then raises or returns."""
    from booley.runtime.endpoint_execution import EndpointOutcome
    from booley.runtime.filesystem_utils import safe_rmtree
    from booley.targets.selection import UnknownTargetError

    flow = LintFlow()

    def run():
        safe_rmtree(removed)
        if raises:
            raise UnknownTargetError("Unknown target 'demo'")
        return EndpointOutcome()

    replace_run(monkeypatch, flow, run)
    return flow


def test_booley_directory_removed_before_the_display_event_stays_removed(runtime, monkeypatch):
    checkout = runtime / "checkout"
    booley_dir = checkout / ".booley"
    booley_dir.mkdir(parents=True)
    monkeypatch.setenv("BOOLEY_RUNTIME_DIR", str(booley_dir / ".runtime"))
    flow = _flow_removing(monkeypatch, booley_dir, raises=False)

    result = flow.execute(
        flow.request_type(target="demo", work_dir=checkout, report_dir=booley_dir / "flow-reports")
    )

    assert result.outcome.report_text == (
        f"lint failed: Booley Directory {booley_dir} was removed during the run"
    )
    assert checkout.is_dir()
    assert not booley_dir.exists()


def test_dry_run_failure_in_a_removed_checkout_prints_its_traceback(runtime, monkeypatch, capsys):
    checkout = runtime / "checkout"
    checkout.mkdir()
    flow = _flow_removing(monkeypatch, checkout, raises=True)

    result = flow.execute(flow.request_type(target="demo", work_dir=checkout, dry_run=True))

    assert result.exit_code == 2
    assert "UnknownTargetError" in capsys.readouterr().err
    assert not checkout.exists()


def test_report_outside_a_removed_checkout_is_still_written(runtime, monkeypatch, capsys):
    checkout = runtime / "checkout"
    checkout.mkdir()
    outside = runtime / "outside"
    flow = _flow_removing(monkeypatch, checkout, raises=True)

    result = flow.execute(flow.request_type(target="demo", work_dir=checkout, report_dir=outside))

    expected = f"lint failed: checkout {checkout} was removed during the run"
    assert result.outcome.report_text == expected
    assert json.loads((outside / "lint.json").read_text())["report_text"] == expected
    assert "UnknownTargetError" in capsys.readouterr().err
    assert not checkout.exists()


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
    for _ in range(3):
        flow = SimulateFlow()

        def run(flow=flow):
            reserved.append(flow.reserve_invocation_dir())
            return EndpointOutcome()

        replace_run(monkeypatch, flow, run)
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
    monkeypatch.setenv("BOOLEY_MCP_JOB_INLINE_WAIT_SECONDS", str(hang_guard_s()))
    cli = LintFlow().execute_cli(["--target", "lite"])
    assert cli.exit_code == 0
    cli_report = json.loads((runtime / "runtime/flow-reports/lint.json").read_text())
    assert "criterion_key" not in cli_report and "criterion_met" not in cli_report

    # Private seam: the server dispatches a tool only from inside a live MCP session.
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

    # The inline wait above is the only budget: a hang guard on the scaled policy,
    # since the child is a real Python process whose CPU-bound start-up slows on a
    # loaded host. A hang is also bounded by the job supervisor's own timeout.
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

    # Private seam: the CLI and MCP loaders run only inside `booley tool` dispatch and the
    # catalog worker process.
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
    calls = []

    def run():
        calls.append(None)
        if len(calls) == 1:
            flow.record_eda_tool("demo", "verible")
        return EndpointOutcome()

    replace_run(monkeypatch, flow, run)
    monkeypatch.setenv("BOOLEY_RUNTIME_DIR", str(runtime / "runtime"))
    flow.execute_cli(["--target", "demo"])
    request = flow.request_type(target="demo", work_dir=runtime)
    flow.execute(request)

    reports = runtime / "runtime/flow-reports/lint"
    first, second = (
        json.loads((reports / f"{n}/report.json").read_text(encoding="utf-8")) for n in (1, 2)
    )
    assert first["argv"] == ["--target", "demo"]
    assert first["eda_tool"] == "verible"
    assert "argv" not in second
    assert "eda_tool" not in second
    starts = [event for event in _display_events(runtime) if event["type"] == "endpoint_start"]
    assert len({event["invocation_id"] for event in starts}) == 2
    assert request.report_dir is None


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


def _display_events(runtime):
    """Every event the endpoints appended to the runtime's display.jsonl."""
    path = runtime / "runtime/display.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _report_targets(root):
    (root / "design.core").write_text(
        "CAPI=2:\nname: acme:lib:dut:1\ntargets:\n"
        "  first:\n    flow: sim\n    flow_options: {tool: verilator}\n    toplevel: dut\n"
    )


@pytest.mark.parametrize("name", ["mutation", "coverage_analyst"])
def test_mutation_flow_and_specialist_reports_carry_no_criterion(runtime, monkeypatch, name):
    from booley.flows.mutation.flow import MutationFlow
    from booley.mcp.base import McpToolResult
    from booley.specialists.coverage_analyst import CoverageAnalystSpecialist

    _report_targets(runtime)
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

    # Private seam: CoverageAnalystSpecialist is not a BuiltinFlow, so replace_run cannot double it.
    monkeypatch.setattr(endpoint_type, "_run", run)
    monkeypatch.setenv("BOOLEY_RUNTIME_DIR", str(runtime / "runtime"))
    result = endpoint_type().execute_cli(
        ["--work-dir", str(runtime), "--report-dir", str(runtime / "reports"), *extras]
    )
    assert not hasattr(result.outcome, "criterion_key")
    assert_paired_headline(runtime / "reports", name)
    (end,) = (event for event in _display_events(runtime) if event["type"] == "endpoint_end")
    assert "criterion_key" not in end and "criterion_met" not in end

"""Compatibility evidence captured before separating Flow and MCP ownership."""

import json
import logging
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from booley.flows.endpoint_state import EndpointState
from booley.flows.execution_persistence import StandaloneFlowExecution
from booley.flows.fpga.flow import FpgaImplFlow
from booley.flows.lint.flow import LintFlow
from booley.flows.sim.flow import SimulateFlow
from booley.flows.synth.flow import AsicSynthesizeFlow
from booley.mcp.flow_adapter import flow_schema
from tests.flows.report_headline import assert_paired_headline

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
    assert not hasattr(flow, "_cli_parser")
    assert request.report_dir is None
    report_root = runtime / "flow-reports"
    assert flow.args.report_dir == report_root
    assert (report_root / f"{flow.name}.json").is_file()
    assert (report_root / flow.name / "1/report.json").is_file()


def test_flow_report_root_precedence(runtime, monkeypatch):
    from booley.runtime.endpoint_execution import EndpointOutcome

    flow = LintFlow()
    monkeypatch.setattr(flow, "_run", EndpointOutcome)
    explicit = runtime / "explicit"
    monkeypatch.setenv("BOOLEY_RUNTIME_DIR", str(runtime / "runtime"))

    flow.execute(flow.request_type(target="demo", work_dir=runtime, report_dir=explicit))
    assert flow.args.report_dir == explicit

    flow.execute(flow.request_type(target="demo", work_dir=runtime))
    assert flow.args.report_dir == runtime / "runtime/flow-reports"

    monkeypatch.delenv("BOOLEY_RUNTIME_DIR")
    flow.execute(flow.request_type(target="demo", work_dir=runtime))
    assert flow.args.report_dir == runtime / "flow-reports"


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
    assert flow.args.report_dir == selected


def test_direct_simulation_invocations_share_report_numbering(runtime, monkeypatch):
    from booley.runtime.endpoint_execution import EndpointOutcome

    reserved = []
    monkeypatch.setattr(SimulateFlow, "prepare_simulation_endpoint", lambda self: None)
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


def test_uninitialized_flow_fails_before_state_or_execution(runtime, monkeypatch):
    from booley.runtime.booley_dir import reset_cache

    uninitialized = runtime / "uninitialized"
    uninitialized.mkdir()
    monkeypatch.chdir(uninitialized)
    monkeypatch.delenv("BOOLEY_DIR")
    reset_cache()
    flow = LintFlow()
    monkeypatch.setattr(
        EndpointState, "read_state", lambda self: pytest.fail("loaded state before report root")
    )
    monkeypatch.setattr(flow, "_run", lambda: pytest.fail("executed uninitialized Flow"))

    result = flow.execute(flow.request_type(target="demo", work_dir=uninitialized))

    assert result.exit_code == 2
    assert "Run 'booley init'" in result.outcome.report_text
    assert not (uninitialized / "flow-reports").exists()


@pytest.mark.parametrize("flow_type", FLOWS)
def test_structured_and_cli_paths_preserve_acceptance_before_save(flow_type, runtime, monkeypatch):
    from booley.criteria.state import DevelopmentState
    from booley.runtime.endpoint_execution import EndpointOutcome
    from tests.criteria import ledger_support as acceptance_ledger
    from tests.criteria.ledger_support import FixtureRecorder

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
    recorder = FixtureRecorder(log_dir=runtime / "logs")

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
        EndpointState, "_acquire_job_slot", lambda self: pytest.fail("admitted unbound request")
    )
    result = flow.execute(flow.request_type(target="demo", work_dir=runtime))
    assert result.exit_code == 2
    assert result.outcome.detail["acceptance_effect"] == "rejected_unbound"


def test_unbound_project_criterion_uses_active_endpoint_catalog(runtime, monkeypatch):
    from booley.criteria.state import DevelopmentState

    booley_dir = runtime / ".booley"
    booley_dir.mkdir()
    (booley_dir / "criteria.toml").write_text(
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
        EndpointState, "_acquire_job_slot", lambda self: pytest.fail("admitted unbound request")
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
        EndpointState,
        "_record_acceptance_changes" if phase == "update" else "_pre_save_hook",
        fail,
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

    from booley.mcp import catalog, server
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
    assert "criterion_key" not in cli_report and "criterion_met" not in cli_report

    async def dispatch():
        jobs = server._JobManager(Mock())
        definition = catalog._mcp_tool_def_from_class(
            LintFlow, flow_schema(LintFlow()), "lint", module_path="booley.flows.lint"
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
    monkeypatch.setattr(flow, "_run", EndpointOutcome)
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

    flow = LintFlow()
    claim = object()
    store = Mock()
    monkeypatch.setattr(EndpointState, "_acquire_job_slot", lambda self: (store, claim))

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


def _headline_state(runtime, monkeypatch, criteria, **options):
    """Persist ticket Criteria for the endpoint under test; return the state path."""
    from booley.criteria.state import DevelopmentState

    path = runtime / "state.json"
    state = DevelopmentState.load(path)
    state.init_criteria(criteria, **options)
    state.save()
    monkeypatch.setenv("BOOLEY_STATE_FILE", str(path))
    return path


def _display_events(monkeypatch):
    """Capture every event the endpoint emits; return the live list."""
    events = []
    monkeypatch.setattr("booley.flows.endpoint_session._write_display_event", events.append)
    monkeypatch.setattr("booley.flows.endpoint_reporting._write_display_event", events.append)
    return events


def _assert_headline(result, report_dir, name, expected, events=None):
    """Outcome, persisted report, MCP projection and completion share one headline."""
    assert (result.outcome.criterion_key, result.outcome.criterion_met) == expected
    report = assert_paired_headline(report_dir, name, expected)
    if events is not None:
        end = next(event for event in events if event["type"] == "endpoint_end")
        assert (end.get("criterion_key", ""), end.get("criterion_met")) == expected
    return report


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
    external_acceptance_owner, flow_type, family, target, met, runtime, monkeypatch
):
    events = _display_events(monkeypatch)
    flow = flow_type()

    def run():
        flow.set_criterion(f"{family}_first", met)
        raise RuntimeError("second workload infrastructure failed")

    monkeypatch.setattr(flow, "_run", run)
    result = flow.execute(flow.request_type(target=target, work_dir=runtime))
    expected = (f"{family}_first", met) if target == "first" else ("", None)
    report = _assert_headline(result, runtime / "flow-reports", flow.name, expected, events)
    assert report["passed"] is False
    assert report["exit_code"] == 2
    assert flow.state.criteria[f"{family}_first"].met is met


def test_reused_endpoint_resets_evaluation_before_early_gate(
    external_acceptance_owner, runtime, monkeypatch
):
    from booley.runtime.endpoint_execution import EndpointOutcome

    flow = LintFlow()

    def run():
        flow.set_criterion("lint_clean_first", True)
        return EndpointOutcome()

    monkeypatch.setattr(flow, "_run", run)
    request = flow.request_type(target="first", work_dir=runtime)
    first = flow.execute(request)
    _assert_headline(first, runtime / "flow-reports", "lint", ("lint_clean_first", True))
    monkeypatch.setattr(flow, "_pre_state_gate", lambda: EndpointOutcome(exit_code=2))
    result = flow.execute(request)
    assert result.outcome.criterion_key == ""
    assert result.outcome.criterion_met is None
    # The early gate publishes nothing: run 1 stays the only persisted and MCP report.
    assert not (runtime / "flow-reports/lint/2").exists()
    assert_paired_headline(runtime / "flow-reports", "lint", ("lint_clean_first", True))


@pytest.mark.parametrize("resume", [False, True])
def test_candidate_mapping_excludes_baseline_prerequisite(
    external_acceptance_owner, runtime, monkeypatch, resume
):
    from contextlib import nullcontext
    from types import SimpleNamespace

    from booley.flows.sim.flow import PreparedSimulationEndpoint
    from booley.runtime.endpoint_execution import EndpointOutcome
    from booley.targets.catalog import TargetCatalog

    (runtime / "design.core").write_text(
        "CAPI=2:\nname: acme:lib:dut:1\ntargets:\n  candidate:\n    flow: sim\n    flow_options: {tool: verilator}\n    toplevel: tb\n  baseline:\n    flow: sim\n    flow_options: {tool: verilator}\n    toplevel: tb\n"
    )
    candidate, baseline = TargetCatalog.build(runtime).select_many(
        "candidate,baseline", for_flow="sim"
    )
    _headline_state(runtime, monkeypatch, {"sim_pass_candidate": True}, strict=True)
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
    monkeypatch.setattr(EndpointState, "admission", lambda *_args: nullcontext())

    def run(*_args):
        flow.set_criterion("sim_pass_candidate", True)
        return EndpointOutcome()

    monkeypatch.setattr(flow, "run_prepared_simulation", run)
    result = flow.execute(flow.request_type(target="candidate", work_dir=runtime))
    assert result.exit_code == 0
    _assert_headline(result, runtime / "flow-reports", "sim", ("sim_pass_candidate", True))


@pytest.mark.parametrize(
    "flow_type,family", [(AsicSynthesizeFlow, "synthesis_ok"), (FpgaImplFlow, "fpga_impl_ok")]
)
def test_frozen_candidate_bindings_do_not_shrink_after_partial_evaluation(
    external_acceptance_owner, flow_type, family, runtime, monkeypatch
):
    from booley.runtime.endpoint_execution import EndpointOutcome

    _headline_state(
        runtime,
        monkeypatch,
        {f"{family}_first": True, f"{family}_second": True},
        flow_key_aliases={f"{family}_demo": [f"{family}_first", f"{family}_second"]},
        strict=True,
    )
    flow = flow_type()

    def run():
        flow.set_criterion(f"{family}_first", False)
        return EndpointOutcome(exit_code=2)

    monkeypatch.setattr(flow, "_run", run)
    result = flow.execute(flow.request_type(target="demo", work_dir=runtime))
    assert result.exit_code == 2
    _assert_headline(result, runtime / "flow-reports", flow.name, ("", None))


def test_override_freezes_parameter_bound_candidate_criteria(
    external_acceptance_owner, runtime, monkeypatch
):
    from booley.runtime.endpoint_execution import EndpointOutcome

    _headline_state(
        runtime,
        monkeypatch,
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
    flow = SimulateFlow()

    def run():
        flow.set_criterion("sim_pass_candidate", True)
        return EndpointOutcome(exit_code=2)

    monkeypatch.setattr(flow, "_run", run)
    result = flow.execute(flow.request_type(target="candidate", work_dir=runtime))
    assert result.exit_code == 2
    _assert_headline(result, runtime / "flow-reports", "sim", ("", None))
    assert flow.state.criteria["sim_pass_candidate"].met is True


@pytest.mark.parametrize(
    "flow_type,family,eda",
    [(AsicSynthesizeFlow, "synthesis_ok", "yosys"), (FpgaImplFlow, "fpga_impl_ok", "vivado")],
)
def test_single_basis_binding_uses_effective_key_and_ignores_baseline_only_binding(
    external_acceptance_owner, flow_type, family, eda, runtime, monkeypatch
):
    from booley.evidence.acceptance import AcceptanceTargetBinding, ResolvedFlowAcceptance
    from booley.runtime.endpoint_execution import EndpointOutcome

    (runtime / "design.core").write_text(
        f"CAPI=2:\nname: acme:lib:dut:1\ntargets:\n  demo:\n    default_tool: {eda}\n    toplevel: dut\n"
    )
    key = f"{family}_demo"
    other = f"{family}_other"
    _headline_state(
        runtime,
        monkeypatch,
        {key: True, other: True},
        criterion_params={
            key: {"target": "acme:lib:dut:1#demo", "_target_selector": "demo"},
            other: {"target": "acme:lib:dut:1#other", "_target_selector": "other"},
        },
        strict=True,
    )
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
    assert result.exit_code == 0
    _assert_headline(result, runtime / "flow-reports", flow.name, (key, True))


@pytest.mark.parametrize("met", [True, False])
def test_alias_first_headline_ignores_family_key_acceptance_also_binds(
    external_acceptance_owner, runtime, monkeypatch, met
):
    from booley.flows import endpoint_acceptance
    from booley.runtime.endpoint_execution import EndpointOutcome

    _headline_state(
        runtime,
        monkeypatch,
        {"lint_clean_atomic": True, "lint_clean": True},
        flow_key_aliases={"lint_clean_first": ["lint_clean_atomic"]},
    )
    flow = LintFlow()

    def run():
        # Acceptance may update the alias and the bare family; the report takes the alias.
        bound = endpoint_acceptance._bound_criterion_keys(flow, "first")
        assert bound == ["lint_clean_atomic", "lint_clean"]
        flow.set_criterion("lint_clean_first", met)
        return EndpointOutcome()

    monkeypatch.setattr(flow, "_run", run)
    result = flow.execute(flow.request_type(target="first", work_dir=runtime))
    _assert_headline(result, runtime / "flow-reports", "lint", ("lint_clean_atomic", met))
    assert flow.state.criteria["lint_clean"].met is False


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
def test_prepared_identity_partial_report_is_null(
    external_acceptance_owner, flow_type, family, met, runtime, monkeypatch
):
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
    assert result.exit_code == 2
    _assert_headline(result, runtime / "flow-reports", flow.name, ("", None))
    identities = frozenset({"acme:lib:dut:1#first", "acme:lib:dut:1#second"})
    assert self_scope(flow) == identities
    assert flow._report_criteria.targets == identities
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
            preview = replace(preview, groups=(selected,), commands=(("run",),), run_groups=())
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
        preview = replace(preview, groups=((),), commands=(("run",),), run_groups=())
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


@pytest.mark.parametrize("mode", ["named", "default", "unfiltered", "coverage"])
@pytest.mark.parametrize("resume", [False, True])
def test_prepared_campaign_plural_mapping_survives_partial_evaluation(
    external_acceptance_owner, runtime, monkeypatch, mode, resume
):
    from booley.runtime.endpoint_execution import EndpointOutcome

    prepared, handle, cycle_test = _prepared_report_campaign(runtime, mode, resume)
    criteria = {"sim_pass_sim": True, "cycle_count_policy": True}
    if mode == "coverage":
        criteria["coverage_sim"] = True
    cycle = {"target": handle.identity, "_target_selector": "sim", "test": cycle_test}
    params = {"cycle_count_policy": {**cycle, "cycle_count_max": 100}}
    _headline_state(runtime, monkeypatch, criteria, criterion_params=params, strict=True)
    monkeypatch.setattr(SimulateFlow, "prepare_simulation_endpoint", lambda self: prepared)

    def run(self, *_args):
        self.set_criterion("sim_pass_sim", True)
        return EndpointOutcome(exit_code=2, report_text="cycle workload unavailable")

    monkeypatch.setattr(SimulateFlow, "run_prepared_simulation", run)
    flow = SimulateFlow()
    result = flow.execute(
        flow.request_type(target="sim", work_dir=runtime, coverage=mode == "coverage"),
        adapter=StandaloneFlowExecution(),
    )
    assert result.exit_code == 2, result.outcome.report_text
    _assert_headline(result, flow.args.report_dir, "sim", ("", None))


def _report_extension_type(endpoint_kind, key):
    from typing import ClassVar

    from booley.flows.base import BooleyFlow
    from booley.runtime.endpoint_execution import EndpointOutcome
    from booley.specialists.specialist import Specialist

    base = BooleyFlow if endpoint_kind == "flow" else Specialist

    class DrcFlow(base):
        name = "drc_check"

        def _build_prompt(self):
            return "unused"

        def _interpret_output(self, output, structured):
            raise AssertionError("agent not invoked")

        satisfies: ClassVar[list[str]] = ["drc_clean", "drc_rules", "cycle_count"]

        def _add_args(self, parser):
            parser.add_argument("--test")

        def _run(self):
            self.set_criterion(key, True)
            return EndpointOutcome()

    return DrcFlow


def _assert_custom_headline(runtime, monkeypatch, endpoint_type, argv, expected):
    """Run a declared custom endpoint and check every public headline surface."""
    events = _display_events(monkeypatch)
    endpoint = endpoint_type()
    result = endpoint.execute_cli([*argv, "--work-dir", str(runtime)])
    assert result.exit_code == 0
    _assert_headline(result, endpoint.args.report_dir, "drc_check", expected, events)


@pytest.mark.parametrize("binding", ["name", "identity", "named_identity"])
@pytest.mark.parametrize("ticket", ["none", "single", "plural"])
@pytest.mark.parametrize("endpoint_kind", ["flow", "specialist"])
@pytest.mark.parametrize("catalog", [True, False])
@pytest.mark.parametrize("target", ["first", "first,second"])
def test_declared_custom_flow_prepared_headline(
    external_acceptance_owner,
    runtime,
    monkeypatch,
    binding,
    ticket,
    endpoint_kind,
    catalog,
    target,
):
    key = "drc_clean_policy42" if binding == "identity" else "drc_clean_first"
    other = "drc_rules_policy73" if binding == "identity" else "drc_rules_first"
    if ticket != "none":
        criteria = {key: True, **({other: True} if ticket == "plural" else {})}
        params = {name: {"target": "acme:lib:dut:1#first"} for name in criteria}
        _headline_state(
            runtime,
            monkeypatch,
            criteria,
            strict=False,
            criterion_params=params if binding != "name" else None,
        )
    if catalog:
        _report_targets(runtime)
    expected = (key, True) if target == "first" and ticket != "plural" else ("", None)
    endpoint_type = _report_extension_type(endpoint_kind, key)
    _assert_custom_headline(runtime, monkeypatch, endpoint_type, ["--target", target], expected)


@pytest.mark.parametrize("endpoint_kind", ["flow", "specialist"])
@pytest.mark.parametrize("target", ["first", "acme:lib:dut:1#first", "other:lib:x#first"])
@pytest.mark.parametrize("other_target", ["first", "second"])
def test_custom_fallback_excludes_only_proven_other_target(
    external_acceptance_owner, runtime, monkeypatch, endpoint_kind, target, other_target
):
    _headline_state(
        runtime,
        monkeypatch,
        {"drc_clean_first": True, "drc_rules_policy73": True},
        strict=False,
        criterion_params={"drc_rules_policy73": {"target": f"acme:lib:dut:1#{other_target}"}},
    )
    # ``other:lib:x`` and ``acme:lib:dut:1`` cannot be one core, so only the
    # bare ``first`` selector (whose core is unknown) keeps the sibling uncertain.
    proven_other_core = target == "other:lib:x#first"
    expected = (
        ("drc_clean_first", True) if other_target == "second" or proven_other_core else ("", None)
    )
    endpoint_type = _report_extension_type(endpoint_kind, "drc_clean_first")
    _assert_custom_headline(runtime, monkeypatch, endpoint_type, ["--target", target], expected)


@pytest.mark.parametrize("endpoint_kind", ["flow", "specialist"])
@pytest.mark.parametrize("family", ["drc_rules", "cycle_count"])
@pytest.mark.parametrize("test", ["selected", "other"])
def test_custom_identity_uncertainty_respects_selected_workload(
    external_acceptance_owner, runtime, monkeypatch, endpoint_kind, family, test
):
    key = f"{family}_policy73"
    params = {"target": "acme:lib:dut:1#first", "test": "selected"}
    if family != "cycle_count":
        params["test_selector"] = "selected"
    _headline_state(
        runtime,
        monkeypatch,
        {"drc_clean_first": True, key: True},
        strict=False,
        criterion_params={key: params},
    )
    expected = ("drc_clean_first", True) if test == "other" else ("", None)
    endpoint_type = _report_extension_type(endpoint_kind, "drc_clean_first")
    argv = ["--target", "first", "--test", test]
    _assert_custom_headline(runtime, monkeypatch, endpoint_type, argv, expected)


@pytest.mark.parametrize("endpoint_kind", ["flow", "specialist"])
@pytest.mark.parametrize("binding", ["stored_selector", "legacy"])
@pytest.mark.parametrize("direction", ["bare_to_qualified", "qualified_to_bare"])
@pytest.mark.parametrize("name", ["first", "second"])
def test_custom_alternate_selector_keeps_unevaluated_potential(
    external_acceptance_owner, runtime, monkeypatch, endpoint_kind, binding, direction, name
):
    target = "dut#first" if direction == "bare_to_qualified" else "first"
    selector = name if direction == "bare_to_qualified" else f"dut#{name}"
    params = {"target": selector}
    if binding == "stored_selector":
        params = {"target": f"acme:lib:dut:1#{name}", "_target_selector": selector}
    _headline_state(
        runtime,
        monkeypatch,
        {"drc_clean_first": True, "drc_rules_policy73": True},
        strict=False,
        criterion_params={"drc_rules_policy73": params},
    )
    expected = ("", None) if name == "first" else ("drc_clean_first", True)
    endpoint_type = _report_extension_type(endpoint_kind, "drc_clean_first")
    _assert_custom_headline(runtime, monkeypatch, endpoint_type, ["--target", target], expected)


@pytest.mark.parametrize("endpoint_kind", ["flow", "specialist"])
def test_custom_evaluated_uncertain_false_is_a_verdict(
    external_acceptance_owner, runtime, monkeypatch, endpoint_kind
):
    from booley.runtime.endpoint_execution import EndpointOutcome

    key = "drc_clean_policy42"
    params = {key: {"target": "acme:lib:dut:1#first"}}
    _headline_state(runtime, monkeypatch, {key: True}, strict=False, criterion_params=params)
    endpoint_type = _report_extension_type(endpoint_kind, key)

    def run(self):
        self.set_criterion(key, False)
        return EndpointOutcome()

    monkeypatch.setattr(endpoint_type, "_run", run)
    _assert_custom_headline(
        runtime, monkeypatch, endpoint_type, ["--target", "first"], (key, False)
    )


@pytest.mark.parametrize("ticket", ["none", "nonstrict", "strict"])
@pytest.mark.parametrize("calls", ["both", "aggregate", "elab"])
def test_combined_prepared_mode_freezes_both_eligible_families(
    external_acceptance_owner, runtime, monkeypatch, ticket, calls
):
    _report_targets(runtime, ("first",))
    if ticket != "none":
        criteria = {"elaborate_standalone": True}
        _headline_state(runtime, monkeypatch, criteria, strict=ticket == "strict")

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
    _assert_headline(result, runtime / "flow-reports", "sim", expected)


@pytest.mark.parametrize("name", ["mutation_tester", "coverage_analyst"])
def test_specialist_public_completion_projects_actual_evaluations(
    external_acceptance_owner, runtime, monkeypatch, name
):
    from booley.mcp.base import McpToolResult
    from booley.specialists.coverage_analyst import CoverageAnalystSpecialist
    from booley.specialists.mutation_tester import MutationTesterSpecialist

    _report_targets(runtime, ("first",))
    classes = {
        "mutation_tester": MutationTesterSpecialist,
        "coverage_analyst": CoverageAnalystSpecialist,
    }
    endpoint_type = classes[name]
    extras = {
        "mutation_tester": ["--target", "first", "--scope", "rtl"],
        "coverage_analyst": ["--campaign", str(runtime / "coverage.json")],
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
    _assert_headline(result, runtime / "reports", name, expected)


@pytest.mark.parametrize(
    ("binding", "expected"),
    [
        pytest.param("vendor:b:y:1.0#first", ("mutation_score_first", False), id="other-core"),
        pytest.param("y#first", ("mutation_score_first", False), id="other-core-partial"),
        pytest.param("x#first", ("", None), id="same-core-partial"),
        pytest.param("vendor:a:x:2.0#first", ("", None), id="other-version"),
        pytest.param("first", ("", None), id="bare"),
    ],
)
def test_mutation_tester_qualified_target_keeps_headline(
    external_acceptance_owner, runtime, monkeypatch, binding, expected
):
    """A qualified ``--target`` excludes a same-name Criterion bound to another core."""
    from booley.mcp.base import EXIT_FAILURE, McpToolResult
    from booley.specialists.mutation_tester import MutationTesterSpecialist
    from tests.target_test_support import write_two_core_sim_project

    write_two_core_sim_project(
        runtime,
        '["vendor:a:x:1.0#first"]\ntests = ["one"]\n["vendor:b:y:1.0#first"]\ntests = ["one"]\n',
        target="first",
    )
    _headline_state(
        runtime,
        monkeypatch,
        {"mutation_score_first": False, "mutation_score_otherfirst": False},
        strict=False,
        criterion_params={"mutation_score_otherfirst": {"target": binding}},
    )
    target = "vendor:a:x:1.0#first"

    def run_cold(self, _plan):
        self.set_criterion(f"mutation_score_{target}", False, source_target=target)
        return McpToolResult(exit_code=EXIT_FAILURE)

    monkeypatch.setattr(MutationTesterSpecialist, "_prepare_run_plan", lambda _self: object())
    monkeypatch.setattr(MutationTesterSpecialist, "_load_reusable_lock", lambda _self, _plan: None)
    monkeypatch.setattr(MutationTesterSpecialist, "_run_cold", run_cold)
    result = MutationTesterSpecialist().execute_cli(
        [
            "--work-dir",
            str(runtime),
            "--report-dir",
            str(runtime / "reports"),
            "--target",
            target,
            "--scope",
            "x.v",
        ]
    )
    _assert_headline(result, runtime / "reports", "mutation_tester", expected)


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
    assert (events[-1].get("criterion_key", ""), events[-1].get("criterion_met")) == ("", None)


def test_public_hooks_see_raw_then_projected_headline(
    external_acceptance_owner, runtime, monkeypatch
):
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
    _assert_headline(result, runtime / "flow-reports", "lint", ("lint_clean_first", True))


def test_public_ambiguous_reviewer_fallback_remains_unknown(runtime, monkeypatch):
    from booley.runtime.endpoint_execution import EndpointOutcome
    from booley.specialists.reviewer import ReviewerSpecialist

    _headline_state(
        runtime, monkeypatch, {"review_rtl_bugs_done": True, "review_rtl_bugs_clean": True}
    )

    def run(self):
        self.set_criterion("review_rtl_bugs_done", True)
        return EndpointOutcome()

    class StableDisplayReviewer(ReviewerSpecialist):
        display_tag = "review"

    monkeypatch.setattr(StableDisplayReviewer, "_run", run)
    events = _display_events(monkeypatch)
    reviewer = StableDisplayReviewer()
    result = reviewer.execute_cli(
        ["--scope", ".", "--category", "rtl", "--focus", "bugs", "--work-dir", str(runtime)]
    )
    assert result.exit_code == 0, result.outcome
    assert reviewer._report_criteria.known is False
    assert (result.outcome.criterion_key, result.outcome.criterion_met) == ("", None)
    assert_paired_headline(reviewer.args.report_dir, "reviewer", None)
    end = next(event for event in events if event["type"] == "endpoint_end")
    assert (end.get("criterion_key", ""), end.get("criterion_met")) == ("", None)


def test_sim_verbose_mcp_schema_deliberately_exposes_optional_boolean():
    schema = flow_schema(SimulateFlow())
    assert schema["properties"]["verbose"] == {
        "type": "boolean",
        "default": False,
        "description": "Include full Simulation Campaign resume mismatch pointers and values",
    }
    assert "verbose" not in schema.get("required", [])
    assert "verbose" not in flow_schema(LintFlow())["properties"]


@pytest.fixture
def external_acceptance_owner(monkeypatch):
    """Retain projection coverage with the existing external ledger composition.

    Unbound execution has separate absence tests. This owner supplies the real
    legacy identity/recording contract, independently of Goal Mode.
    """
    from booley.flows.endpoint_state import EndpointState
    from booley.mcp import flow_execution_selection
    from tests.criteria.ledger_support import FixtureRecorder

    recorder = FixtureRecorder(identity={"generation": "d" * 32, "authored_sha256": "e" * 64})
    configure = EndpointState.configure_flow_execution

    def configure_external(endpoint, adapter):
        configure(endpoint, adapter)
        endpoint._acceptance_recorder = recorder

    monkeypatch.setattr(EndpointState, "configure_flow_execution", configure_external)
    monkeypatch.setattr(
        flow_execution_selection,
        "_STANDALONE_SELECTION",
        flow_execution_selection.FlowExecutionSelection(None, lambda: recorder),
    )


def test_direct_generic_completion_preserves_empty_criterion_fields():
    from booley.flows.endpoint_events import _endpoint_end_event
    from booley.runtime.endpoint_execution import EndpointOutcome

    event = _endpoint_end_event("generic", None, EndpointOutcome(), 0.0)
    assert event["criterion_key"] == ""
    assert event["criterion_met"] is False


@pytest.mark.parametrize("strict", [False, True])
@pytest.mark.parametrize("endpoint_kind", ["flow", "specialist"])
def test_custom_headline_keeps_same_name_selector_uncertain(
    external_acceptance_owner, runtime, monkeypatch, endpoint_kind, strict
):
    # A custom endpoint prepares no Target selection, so reporting does not resolve
    # one either: a same-name selector on another core stays an unevaluated potential.
    _report_targets(runtime)
    _headline_state(
        runtime,
        monkeypatch,
        {"drc_clean_first": True, "drc_rules_policy73": True},
        strict=strict,
        criterion_params={"drc_rules_policy73": {"target": "other:lib:x#first"}},
    )
    endpoint_type = _report_extension_type(endpoint_kind, "drc_clean_first")
    _assert_custom_headline(runtime, monkeypatch, endpoint_type, ["--target", "first"], ("", None))


@pytest.mark.parametrize("endpoint_kind", ["flow", "specialist"])
def test_custom_headline_scope_resolves_no_target(
    external_acceptance_owner, runtime, monkeypatch, endpoint_kind
):
    # Non-strict, so admission's own binding gate resolves nothing either.
    from booley.targets.catalog import TargetCatalog

    _report_targets(runtime)
    _headline_state(runtime, monkeypatch, {"drc_clean_first": True}, strict=False)

    def refuse(*_args, **_kwargs):
        raise AssertionError("report headline resolved a Target catalog")

    monkeypatch.setattr(TargetCatalog, "build", refuse)
    endpoint_type = _report_extension_type(endpoint_kind, "drc_clean_first")
    argv = ["--target", "first"]
    _assert_custom_headline(runtime, monkeypatch, endpoint_type, argv, ("drc_clean_first", True))


def test_prepared_workload_scope_follows_resolved_test_runs(
    external_acceptance_owner, runtime, monkeypatch
):
    # A named Variant runs Test "count": the cycle Criterion on it is in scope, so a
    # run that judged only sim_pass reports no headline instead of a partial one.
    from booley.runtime.endpoint_execution import EndpointOutcome

    prepared, handle, _ = _prepared_report_campaign(runtime, "named", False)
    tests_toml = runtime / ".booley/tests.toml"
    tests_toml.write_text(
        '[sim]\nselect = "+test={name}"\ntests = ["reset", "count"]\n\n'
        '[sim.variants.count]\ndefault = "short"\nshort = []\nsoak = ["+iters=9"]\n'
    )
    scope = {"target": handle.identity, "_target_selector": "sim"}
    params = {
        "sim_pass_count": {**scope, "test_selector": "count"},
        "cycle_count_policy": {**scope, "test": "count", "cycle_count_max": 100},
    }
    criteria = dict.fromkeys(params, True)
    _headline_state(runtime, monkeypatch, criteria, criterion_params=params, strict=True)
    monkeypatch.setattr(SimulateFlow, "prepare_simulation_endpoint", lambda self: prepared)

    def run(self, *_args):
        self.set_criterion("sim_pass_count", True)
        return EndpointOutcome(exit_code=2, report_text="cycle workload unavailable")

    monkeypatch.setattr(SimulateFlow, "run_prepared_simulation", run)
    flow = SimulateFlow()
    result = flow.execute(flow.request_type(target="sim", work_dir=runtime, test=["count+soak"]))
    assert result.exit_code == 2, result.outcome.report_text
    _assert_headline(result, flow.args.report_dir, "sim", ("", None))


def test_prepared_workload_scope_keeps_whole_suite_criterion_with_skipped_test(
    external_acceptance_owner, runtime, monkeypatch
):
    # A configured skip leaves "count" out of the run and of the required suite, so the
    # whole-suite sim_pass Criterion stays planned beside the judged cycle Criterion.
    from booley.runtime.endpoint_execution import EndpointOutcome

    prepared, handle, _ = _prepared_report_campaign(runtime, "named", False)
    tests_toml = runtime / ".booley/tests.toml"
    tests_toml.write_text('[sim]\ntests = ["reset", "count"]\nskip = ["count"]\n')
    scope = {"target": handle.identity, "_target_selector": "sim"}
    params = {"cycle_count_policy": {**scope, "test": "reset", "cycle_count_max": 100}}
    criteria = {"sim_pass_sim": True, "cycle_count_policy": True}
    _headline_state(runtime, monkeypatch, criteria, criterion_params=params, strict=True)

    def prepare(self):
        assert self._validate_test_plans(["sim"], prepared.test_names_map) is None
        return prepared

    def run(self, *_args):
        self.set_criterion("cycle_count_policy", True)
        return EndpointOutcome(exit_code=2, report_text="reset lost to infrastructure")

    monkeypatch.setattr(SimulateFlow, "prepare_simulation_endpoint", prepare)
    monkeypatch.setattr(SimulateFlow, "run_prepared_simulation", run)
    flow = SimulateFlow()
    result = flow.execute(flow.request_type(target="sim", work_dir=runtime))
    assert result.exit_code == 2, result.outcome.report_text
    _assert_headline(result, flow.args.report_dir, "sim", ("", None))


def test_prepared_coverage_workload_keeps_declared_suite_with_skipped_test(
    external_acceptance_owner, runtime, monkeypatch
):
    # A coverage manifest records the declared suite, skips included, so a partial
    # selection leaves the whole-suite sim_pass Criterion out of the planned scope.
    from booley.runtime.endpoint_execution import EndpointOutcome

    prepared, _, _ = _prepared_report_campaign(runtime, "named", False)
    tests_toml = runtime / ".booley/tests.toml"
    tests_toml.write_text('[sim]\ntests = ["reset", "count"]\nskip = ["count"]\n')
    criteria = {"sim_pass_sim": True, "coverage_sim": True}
    _headline_state(runtime, monkeypatch, criteria, strict=True)

    def prepare(self):
        assert self._validate_test_plans(["sim"], prepared.test_names_map) is None
        return prepared

    def run(self, *_args):
        self.set_criterion("coverage_sim", True)
        return EndpointOutcome(exit_code=0, report_text="coverage met")

    monkeypatch.setattr(SimulateFlow, "prepare_simulation_endpoint", prepare)
    monkeypatch.setattr(SimulateFlow, "run_prepared_simulation", run)
    flow = SimulateFlow()
    result = flow.execute(
        flow.request_type(target="sim", work_dir=runtime, coverage=True, test=["reset"])
    )
    _assert_headline(result, flow.args.report_dir, "sim", ("coverage_sim", True))

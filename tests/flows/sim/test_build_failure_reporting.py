"""Build timeout/OOM evidence must reach the agent-visible report (issue #882).

Every consumer of an infrastructure build outcome (record Build Result,
typed record error, endpoint report, coverage build) must lead
with the actionable reason and a bounded, marker-free output tail.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import booley.flows.sim.build as build_module
from booley.flows.base import SubprocessResult
from booley.flows.sim.build import classify_build_outcome
from booley.flows.sim.execution import SimulationOptions
from booley.flows.sim.execution.contract import SimulationTestOutcome
from booley.flows.sim.execution.engine import SimulationExecution
from booley.flows.sim.record.codec import (
    MAX_DETAIL_BYTES,
    canonical_json_bytes,
)
from booley.flows.sim.record.coordinator import (
    NewSimulationRecordRunRequest,
    SimulationRecord,
    SimulationRecordPolicy,
)
from booley.flows.sim.record.model import create_simulation_record_plan
from booley.flows.sim.record.serial_execution import OrdinaryHdlSerialExecutor
from booley.flows.sim.record.store import SimulationRecordStore
from booley.flows.sim.verilator_coverage import SimulationBuildResult
from booley.flows.source_paths import ProjectSourcePaths
from booley.fusesoc.fusesoc_registry import ResolvedFile
from booley.mcp.base import EXIT_ERROR
from booley.targets.catalog import TargetCatalog
from tests.flows.sim.prepared_group import run_prepared_group
from tests.flows.sim.simulation_record_boundary import PASS_PROCESS, run_simulation_record_sim
from tests.flows.sim.simulation_record_test_support import (
    _admission,
    _FailedCoverageBuild,
    _named_manifest,
    _plan,
    _run_coverage_simulation_record,
)
from tests.flows.sim.simulation_record_test_support import _handle as _shared_handle
from tests.flows.sim.test_execution_engine import (
    _handle,
    _prepared,
    _real_fusesoc,
    _run_execution,
    _subprocess_invoker,
    _write_runtime_input_project,
)
from tests.flows.sim.test_verilator_coverage_execution import (
    _build_coverage,
    _execution_fixture,
)
from tests.time_budget import hang_guard_s

TIMEOUT_REASON = "build timed out after 600 s (raise [flows.sim].build_timeout_ms)"
OOM_REMEDY = (
    "for Verilator, lower build parallelism with -j<N> in the Target's "
    "flow_options.make_options, or raise [sandbox].memory and recreate the Sandbox"
)
HEAD_LINE = "make: Entering directory '/work/build' HEADMARKER"
LAST_LINE = "last line before kill"
HOSTILE_BUILD_OUTPUT = (
    '\\"\t漢字' * 200
    + "\x1b[31mred\x1b[0m\x00\n"
    + "y" * 10_000
    + "\nBOOLEY_BUILD_STAGE token=abc123 rc=137\n"
)


def _long_output() -> str:
    """Build output whose head must be dropped and whose tail must survive."""
    lines = [HEAD_LINE, "g++ " + "-I/very/long/include/path " * 80]
    lines += [f"compiling unit {index}" for index in range(40)]
    lines.append(LAST_LINE)
    return "\n".join(lines) + "\n"


def _timed_out_result(stdout: str | None = None) -> SubprocessResult:
    return SubprocessResult(
        returncode=-9,
        stdout=_long_output() if stdout is None else stdout,
        timed_out=True,
    )


@pytest.mark.parametrize(
    ("case", "stdout", "peak", "expected_peak"),
    [
        (
            "with_record",
            "gcc\nmake: *** Error 137\nBOOLEY_BUILD_STAGE token=abc123 rc=137\n",
            31744.4,
            "31744 MB",
        ),
        ("no_record", "gcc\nmake: *** Error 137\n", 31744.4, "31744 MB"),
        ("unknown_peak", "gcc\nmake: *** Error 137\n", None, "unknown"),
        (
            "design_diagnostic_present",
            "%Error: rtl/top.sv:4:3: syntax error\nmake: *** Error 137\n"
            "BOOLEY_BUILD_STAGE token=abc123 rc=1\n",
            31744.4,
            "31744 MB",
        ),
    ],
)
def test_oom_build_names_oom_evidence_and_remedy(
    case: str, stdout: str, peak: float | None, expected_peak: str
) -> None:
    del case
    outcome = classify_build_outcome(
        SubprocessResult(
            returncode=137, stdout=stdout, oom_kill_delta=1, oom_killed=True, peak_rss_mb=peak
        ),
        "abc123",
        timeout_s=600,
    )

    peak_text = "build peak RSS unknown" if peak is None else f"build peak RSS {expected_peak}"
    assert outcome.reason == (
        f"build failed while the Sandbox recorded an OOM kill ({peak_text}; {OOM_REMEDY})"
    )
    assert outcome.failure_kind == "infrastructure"
    report = build_module.build_failure_report(outcome, ProjectSourcePaths())
    assert report.splitlines()[0] == outcome.reason
    assert "Error 137" in report
    assert "BOOLEY_BUILD_STAGE" not in report


def test_host_oom_build_names_host_memory_not_a_sandbox_remedy() -> None:
    outcome = classify_build_outcome(
        SubprocessResult(
            returncode=137,
            stdout="make: *** Error 137\nBOOLEY_BUILD_STAGE token=abc123 rc=1\n",
            oom_kill_delta=1,
            oom_killed=True,
            host_oom_kill=True,
            peak_rss_mb=2048.0,
        ),
        "abc123",
        timeout_s=600,
    )

    assert (
        outcome.reason
        == "build failed: the host ran out of memory and killed it (build peak RSS 2048 MB)"
    )
    assert "raise [sandbox].memory" not in (outcome.reason or "")
    assert outcome.failure_kind == "infrastructure"


def test_ordinary_build_failure_during_another_jobs_oom_kill_is_not_oom(monkeypatch) -> None:
    diagnostic = "%Error: rtl/top.sv:4:3: syntax error"
    outcome = classify_build_outcome(
        SubprocessResult(
            returncode=1,
            stdout=f"{diagnostic}\nBOOLEY_BUILD_STAGE token=abc123 rc=1\n",
            oom_kill_delta=1,
        ),
        "abc123",
        timeout_s=600,
    )

    assert outcome.design_failed
    assert "OOM" not in (outcome.reason or "")
    assert diagnostic in outcome.output


_CORE = "src/acme_lib_runtime_input_1/tb.sv"
_OVERLAY_CORE = "src/acme_lib_runtime_input-booleytrace_1/tb.sv"
_DESIGN_LINES = (
    "{core}:4: syntax error",
    "{core}:9: error: Unknown module type: missing",
    "gen/other.sv:2: error: Unknown module type: other",
)
_SORRY = "{core}:4: sorry: Constant user functions are not supported."


@pytest.mark.parametrize(
    ("trace", "core", "lines"),
    [
        (False, _CORE, _DESIGN_LINES),
        (True, _OVERLAY_CORE, _DESIGN_LINES),
        (False, _CORE, (_SORRY,)),
    ],
    ids=["design", "trace-overlay", "infrastructure"],
)
def test_build_failure_names_project_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    trace: bool,
    core: str,
    lines: tuple[str, ...],
) -> None:
    pytest.importorskip("fusesoc")
    pytest.importorskip("edalize")
    project = tmp_path / "project"
    state = _write_runtime_input_project(project)
    (state / "tests.toml").write_text('[sim]\ntests = ["t"]\n', encoding="utf-8")
    printed = [line.format(core=core) for line in lines]
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    iverilog = fake_bin / "iverilog"
    iverilog.write_bytes(
        ("#!/bin/sh\n" + "".join(f"echo '{x}'\n" for x in printed) + "exit 1\n").encode()
    )
    iverilog.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")

    handle = TargetCatalog.build(project).select("sim", for_flow="sim")
    execution = SimulationExecution(
        invoke=_subprocess_invoker(project),
        options=SimulationOptions(trace=trace, timeout_ms=hang_guard_s() * 1000),
    )
    with _real_fusesoc():
        outcome = run_prepared_group(execution, handle, ())

    expected = [line.format(core="tb.sv") for line in lines]
    if lines == (_SORRY,):
        assert outcome.infrastructure_failure is not None
        shown = outcome.infrastructure_failure.detail.splitlines()
    else:
        assert outcome.infrastructure_failure is None
        shown = outcome.tests[0].error_tail.splitlines()
        # The persisted build output keeps the raw build-tree spelling.
        assert printed[0] in outcome.builds[0].output
    for line in expected:
        assert line in shown


def _engine_timeout_outcome(tmp_path: Path):
    handle = _handle(tmp_path)
    prepared = _prepared(handle, cocotb=False)
    return _run_execution(
        handle,
        prepared,
        lambda _command, timeout: _timed_out_result(),
        ("smoke",),
        cocotb=False,
        canned_at="build",
        options=SimulationOptions(build_timeout_ms=600_000),
    )


class _InfrastructureGroup:
    """Fake build group whose build ended as the real engine reported it."""

    artifact_paths: tuple[Path, ...] = ()

    def __init__(self, root: Path, outcome) -> None:
        self.build_root = root
        self._outcome = outcome

    def planning_disclosure(self):
        return {}

    def compile(self):
        return SimpleNamespace(passed=False)

    def finish_build_failure(self):
        return self._outcome


def _first_build_result(store: SimulationRecordStore) -> dict[str, object]:
    (path,) = store.root.glob("**/build-result.json")
    return json.loads(path.read_text())


def _shared_executor(root: Path, outcome) -> OrdinaryHdlSerialExecutor:
    class Execution:
        @contextmanager
        def ordinary_group(self, _handle, _names, _test_runs=None):
            yield _InfrastructureGroup(root, outcome)

    return OrdinaryHdlSerialExecutor(
        invoke=lambda *_args, **_kwargs: None,  # type: ignore[arg-type]
        execution_factory=lambda _options: Execution(),  # type: ignore[arg-type,return-value]
    )


def _run_shared_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome):
    plan = create_simulation_record_plan(_named_manifest(("tail", "quick")))
    project = tmp_path / "project"
    project.mkdir()
    (project / ".booley").mkdir()
    monkeypatch.setenv("BOOLEY_DIR", str(project / ".booley"))
    handle = _shared_handle(project)
    monkeypatch.setattr(
        "booley.flows.sim.record.serial_execution.TargetCatalog.build",
        lambda _root: SimpleNamespace(select=lambda *_args, **_kwargs: handle),
    )
    root = tmp_path / "engine-build"
    root.mkdir()
    executor = _shared_executor(root, outcome)
    invocation = tmp_path / "reports" / "000001"
    invocation.mkdir(parents=True)
    request = NewSimulationRecordRunRequest(
        plan, project, invocation.parent, SimulationRecordPolicy(), invocation, _admission()
    )
    return executor, request, invocation


def _published_build_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome):
    """Run the shared executor to a terminal error record; return its build result."""
    executor, request, invocation = _run_shared_timeout(tmp_path, monkeypatch, outcome)
    simulation_record = SimulationRecord(executor).run(request)
    assert simulation_record.complete is True
    assert simulation_record.aggregate_grade == "error"
    store = SimulationRecordStore(invocation / "targets" / "sim" / "record")
    assert store.scan().interrupted == ()
    result = _first_build_result(store)
    assert result["state"] == "infrastructure_error"
    return simulation_record, result


def test_simulation_record_build_timeout_publishes_terminal_infrastructure_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outcome = _engine_timeout_outcome(tmp_path / "engine")
    failure = outcome.infrastructure_failure
    assert failure is not None
    assert failure.detail.splitlines()[0] == TIMEOUT_REASON
    assert HEAD_LINE not in failure.detail
    simulation_record, result = _published_build_result(tmp_path, monkeypatch, outcome)

    assert simulation_record.observations[0]["detail"]["reason"].splitlines()[0] == TIMEOUT_REASON
    observation = result["observation"]
    assert observation["message"] == TIMEOUT_REASON
    assert observation["detail"]["text"].splitlines()[0] == TIMEOUT_REASON


def test_flow_sim_build_timeout_report_leads_with_the_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = run_simulation_record_sim(
        tmp_path, monkeypatch, {"sim": ["t1"]}, PASS_PROCESS, build=_timed_out_result()
    )

    outcome = run.result.outcome
    assert run.result.exit_code == EXIT_ERROR
    first_line = outcome.report_text.splitlines()[0]
    assert "build timed out" in first_line
    assert "(raise [flows.sim].build_timeout_ms)" in first_line
    assert outcome.detail["eda_tool_error"] == "build_infrastructure"
    assert outcome.detail["build_stage"]["timed_out"] is True
    assert LAST_LINE in outcome.report_text
    assert HEAD_LINE not in outcome.report_text
    assert run.launched == []


def _failed_coverage_build(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    build_stage: Callable[[str], SubprocessResult],
    files: tuple[ResolvedFile, ...] = (),
):
    execution, target, _raw, _captured = _execution_fixture(tmp_path, monkeypatch, files=files)
    execution._options = SimulationOptions(build_timeout_ms=600_000)
    original = execution._invoke

    def invoke(command, *, timeout):
        if "BOOLEY_BUILD_STAGE" in command[-1]:
            return build_stage(command[-1])
        return original(command, timeout=timeout)

    execution._invoke = invoke
    return _build_coverage(execution, target)


def _timed_out_coverage_build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stdout: str):
    return _failed_coverage_build(tmp_path, monkeypatch, lambda _script: _timed_out_result(stdout))


def test_coverage_build_failure_names_project_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prefix = "src/acme_demo_counter-booleycoverage_1"
    files = (
        ResolvedFile(
            f"{prefix}/rtl/counter.sv",
            "systemVerilogSource",
            core="acme:demo:counter-booleycoverage:1",
        ),
    )

    def build_stage(script: str) -> SubprocessResult:
        token = re.search(r"token=([0-9a-f]+)", script)
        assert token is not None
        return SubprocessResult(
            returncode=1,
            stdout=(
                f"%Error: {prefix}/rtl/counter.sv:1:8: syntax error\n"
                "%Error: gen/other.sv:2:3: syntax error\n"
                f"BOOLEY_BUILD_STAGE token={token.group(1)} rc=1\n"
            ),
        )

    build = _failed_coverage_build(tmp_path, monkeypatch, build_stage, files)

    assert build.success is False
    assert build.infrastructure_error is False
    assert build.output.splitlines()[:2] == [
        "%Error: rtl/counter.sv:1:8: syntax error",
        "%Error: gen/other.sv:2:3: syntax error",
    ]


def test_coverage_build_timeout_is_infrastructure_with_reason_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    build = _timed_out_coverage_build(tmp_path, monkeypatch, "z" * 2_000 + f"\n{LAST_LINE}\n")

    assert build.success is False
    assert build.infrastructure_error is True
    assert build.reason == TIMEOUT_REASON
    assert build.output.splitlines()[0] == TIMEOUT_REASON
    assert len(build.output) < 1_000


@pytest.mark.parametrize(
    ("reason", "output", "message"),
    [
        (TIMEOUT_REASON, f"{TIMEOUT_REASON}\n--- output tail ---\n{LAST_LINE}", TIMEOUT_REASON),
        (None, "coverage Verilator identity mismatch", "coverage Verilator identity mismatch"),
    ],
    ids=["reason", "reasonless"],
)
def test_coverage_simulation_record_build_failure_message_is_reason(
    tmp_path: Path, reason: str | None, output: str, message: str
) -> None:
    failing = _FailedCoverageBuild(infrastructure=True)
    failing.build = lambda _request: SimulationBuildResult(  # type: ignore[method-assign]
        False, output, infrastructure_error=True, reason=reason
    )
    simulation_record, _invocation = _run_coverage_simulation_record(tmp_path, failing)
    assert simulation_record.complete is True
    assert simulation_record.aggregate_grade == "error"

    store = SimulationRecordStore(tmp_path / "reports/1/targets/sim_0/record")
    observation = _first_build_result(store)["observation"]
    assert observation["message"] == message
    assert output.splitlines()[-1] in observation["detail"]["text"]
    assert store.scan().interrupted == ()


def test_prepared_build_failure_reports_missing_executable(tmp_path: Path) -> None:
    for preamble in ("", "x" * 3000 + "\n"):
        outcome = _run_execution(
            _handle(tmp_path),
            _prepared(_handle(tmp_path), cocotb=False),
            lambda _command, timeout, preamble=preamble: SubprocessResult(
                returncode=127, stdout=f"{preamble}/bin/sh: 1: verilator: not found\n"
            ),
            ("smoke",),
            cocotb=False,
            canned_at="build",
        )
        assert outcome.infrastructure_failure is not None
        assert outcome.infrastructure_failure.missing_executable == "verilator"


def test_ansi_prefixed_marker_line_is_dropped_from_tail() -> None:
    output = "a\n\x1b[31mBOOLEY_BUILD_STAGE token=abc rc=1\x1b[0m\nb\n"

    assert build_module.build_output_tail(output) == "a\nb"


def test_hostile_oom_build_output_publishes_and_decodes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handle = _handle(tmp_path / "engine")
    outcome = _run_execution(
        handle,
        _prepared(handle, cocotb=False),
        lambda _command, timeout: SubprocessResult(
            returncode=137,
            stdout=HOSTILE_BUILD_OUTPUT,
            oom_kill_delta=1,
            oom_killed=True,
            peak_rss_mb=31744.4,
        ),
        ("smoke",),
        cocotb=False,
        canned_at="build",
    )
    _record, result = _published_build_result(tmp_path, monkeypatch, outcome)

    text = result["observation"]["detail"]["text"]
    assert "OOM kill" in text.splitlines()[0]
    assert "\x1b" not in text
    assert "\x00" not in text
    assert "BOOLEY_BUILD_STAGE" not in text
    assert len(canonical_json_bytes({"text": text})) <= MAX_DETAIL_BYTES


@pytest.mark.parametrize("executable", ["verilator_bin", "iverilog"])
def test_build_owned_loader_startup_is_infrastructure(executable):
    text = f"{executable}: error while loading shared libraries: libx.so: missing"
    outcome = classify_build_outcome(
        SubprocessResult(
            returncode=127, stdout=text + "\nBOOLEY_BUILD_STAGE token=abc123 rc=127\n"
        ),
        "abc123",
        timeout_s=600,
    )
    assert outcome.failure_kind == "infrastructure"
    assert "libx.so" in outcome.reason


@pytest.mark.parametrize("access", ["legacy-per-test", "immutable"])
def test_large_batch_build_failure_publishes_bounded_terminal_result(
    tmp_path, monkeypatch, access
) -> None:
    names = tuple(f"test_{index}" for index in range(1000))
    plan = _plan(tmp_path, kind="cocotb_batch", cocotb=True, names=names, access=access)
    outcome = _engine_timeout_outcome(tmp_path / "engine")
    assert outcome.infrastructure_failure is not None
    outcome = replace(
        outcome,
        infrastructure_failure=replace(outcome.infrastructure_failure, detail="界" * 4000),
        builds=(replace(outcome.builds[0], reason="compiler failed: " + "界" * 200),),
    )
    if access == "legacy-per-test":
        outcome = replace(
            outcome,
            tests=tuple(
                SimulationTestOutcome(
                    name, "elab_error", False, reason="x" * 500 + "tail sentinel"
                )
                for name in names
            ),
        )
    root = tmp_path / "failed-build"
    root.mkdir()

    executor = _shared_executor(root, outcome)
    invocation = tmp_path / "reports/1"
    invocation.mkdir(parents=True)
    simulation_record = SimulationRecord(executor).run(
        NewSimulationRecordRunRequest(
            plan, tmp_path, invocation.parent, SimulationRecordPolicy(), invocation, _admission()
        )
    )
    assert simulation_record.complete
    store = SimulationRecordStore(invocation / "targets/sim/record")
    result = store.scan().items[0].result
    assert result is not None
    assert result.document["grade"] == "error"
    assert [item["test"] for item in result.document["observations"]] == list(names)
    assert _first_build_result(store)["observation"]["detail"]["build_stage"]["timed_out"]
    assert "compiler failed:" in simulation_record.observations[0]["detail"]["reason"]
    if access == "legacy-per-test":
        assert "tail sentinel" in simulation_record.observations[0]["detail"]["reason"]


def test_escaped_build_metadata_stays_within_canonical_detail_budget(
    tmp_path, monkeypatch
) -> None:
    outcome = _engine_timeout_outcome(tmp_path / "engine")
    assert outcome.infrastructure_failure is not None
    outcome = replace(
        outcome,
        infrastructure_failure=replace(
            outcome.infrastructure_failure, missing_executable="\\" * 128, detail="界" * 4000
        ),
        builds=(
            replace(
                outcome.builds[0],
                reason="compiler failed: " + "\x01" * 200,
                cache_decision="\x02" * 400,
            ),
        ),
    )
    _record, result = _published_build_result(tmp_path, monkeypatch, outcome)
    detail = result["observation"]["detail"]

    assert len(canonical_json_bytes(detail)) <= MAX_DETAIL_BYTES
    assert detail["build_stage"]["timed_out"] is True

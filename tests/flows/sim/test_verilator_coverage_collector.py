from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from booley.flows.sim.verilator_coverage import (
    PINNED_VERILATOR,
    CoverageCollectionRequest,
    CoverageSource,
    CoverageTarget,
    SelectedCoverageTest,
    SimulationBuildResult,
    SimulationCommandResult,
    SimulationRunResult,
    VerilatorCollectorIdentity,
    collect,
)

_HEADER = "# SystemC::Coverage-3\n"
_LINE_POINT = (
    "C '\x01f\x02rtl/counter.sv\x01l\x0210\x01n\x023\x01h\x02TOP.counter"
    "\x01t\x02line\x01o\x02block' 2\n"
)


def _request(
    tmp_path: Path,
    test: str,
    *,
    harness: str = "generated_main",
    hooks: tuple[str, ...] = (),
    reset_included: bool = True,
) -> CoverageCollectionRequest:
    return CoverageCollectionRequest(
        target=CoverageTarget(
            identity="acme:demo:counter:1.0#sim_counter",
            selector="sim_counter",
            toplevel="counter",
            harness=harness,
            sources=(CoverageSource("rtl/counter.sv", "rtl/counter.sv", "rtl"),),
            custom_main_hooks=hooks,
        ),
        selected_tests=(SelectedCoverageTest(test),),
        artifact_root=tmp_path / "campaign",
        reset_included=reset_included,
    )


def _native_record(
    record_type: str,
    comment: str,
    *,
    hierarchy: str = "TOP.counter",
    line: int = 10,
    column: int = 3,
    hits: int = 1,
) -> str:
    identity = (
        f"\x01f\x02rtl/counter.sv\x01l\x02{line}\x01n\x02{column}"
        f"\x01h\x02{hierarchy}\x01t\x02{record_type}\x01o\x02{comment}"
    )
    return f"C '{identity}' {hits}\n"


class _GeneratedMainExecution:
    def build(self, request) -> SimulationBuildResult:
        return SimulationBuildResult(success=True, collector=PINNED_VERILATOR)

    def run(self, request) -> SimulationRunResult:
        request.raw_path.parent.mkdir(parents=True, exist_ok=True)
        request.raw_path.write_text(_HEADER + _LINE_POINT, encoding="utf-8")
        return SimulationRunResult(verdict="pass")

    def command(self, request) -> SimulationCommandResult:
        assert request.argv[:2] == ("verilator_coverage", "--write")
        request.output_path.parent.mkdir(parents=True, exist_ok=True)
        request.output_path.write_text(_HEADER + _LINE_POINT, encoding="utf-8")
        return SimulationCommandResult(returncode=0)


class _MissingRawExecution(_GeneratedMainExecution):
    def run(self, request) -> SimulationRunResult:
        return SimulationRunResult(verdict="pass")

    def command(self, request) -> SimulationCommandResult:
        raise AssertionError("merge must not run without every raw database")


class _StaleRawExecution(_MissingRawExecution):
    def run(self, request) -> SimulationRunResult:
        return SimulationRunResult(verdict="fail")


class _MalformedRawExecution(_MissingRawExecution):
    def __init__(self, payload: str | bytes = _HEADER + "not a coverage record\n") -> None:
        self.payload = payload

    def run(self, request) -> SimulationRunResult:
        request.raw_path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(self.payload, bytes):
            request.raw_path.write_bytes(self.payload)
        else:
            request.raw_path.write_text(self.payload, encoding="utf-8")
        return SimulationRunResult(verdict="timeout")


class _IncompatibleRawExecution(_MissingRawExecution):
    def run(self, request) -> SimulationRunResult:
        request.raw_path.parent.mkdir(parents=True, exist_ok=True)
        request.raw_path.write_text("# SystemC::Coverage-4\n" + _LINE_POINT, encoding="utf-8")
        return SimulationRunResult(verdict="pass")


class _RichNativeExecution(_GeneratedMainExecution):
    payload = _HEADER + "".join(
        (
            _native_record("line", "block", hierarchy="TOP.first"),
            _native_record("line", "block", hierarchy="TOP.second"),
            _native_record("branch", "if"),
            _native_record("expr", "(enable == 1) => 1"),
            _native_record("toggle", "count[0]:0->1"),
            _native_record("user", "wrap_seen"),
            _native_record("fsm", "RUN->WRAP"),
            _native_record("covergroup", "values.high"),
            _native_record("future_kind", "opaque"),
        )
    )

    def run(self, request) -> SimulationRunResult:
        request.raw_path.parent.mkdir(parents=True, exist_ok=True)
        request.raw_path.write_text(self.payload, encoding="utf-8")
        return SimulationRunResult(verdict="pass")

    def command(self, request) -> SimulationCommandResult:
        request.output_path.parent.mkdir(parents=True, exist_ok=True)
        request.output_path.write_bytes(Path(request.argv[-1]).read_bytes())
        return SimulationCommandResult(returncode=0)


class _StagedSourceExecution(_GeneratedMainExecution):
    payload = _HEADER + _LINE_POINT.replace(
        "\x01f\x02rtl/counter.sv",
        "\x01f\x02/build/src/acme_demo_counter_1/rtl/counter.sv",
    )

    def run(self, request) -> SimulationRunResult:
        request.raw_path.parent.mkdir(parents=True, exist_ok=True)
        request.raw_path.write_text(self.payload, encoding="utf-8")
        return SimulationRunResult(verdict="pass")

    def command(self, request) -> SimulationCommandResult:
        request.output_path.parent.mkdir(parents=True, exist_ok=True)
        request.output_path.write_text(self.payload, encoding="utf-8")
        return SimulationCommandResult(returncode=0)


class _PostResetExecution(_GeneratedMainExecution):
    def run(self, request) -> SimulationRunResult:
        assert request.hook_evidence_path is not None
        request.raw_path.parent.mkdir(parents=True, exist_ok=True)
        request.raw_path.write_text(_HEADER + _LINE_POINT, encoding="utf-8")
        request.hook_evidence_path.parent.mkdir(parents=True, exist_ok=True)
        request.hook_evidence_path.write_text(
            json.dumps(
                {
                    "$schema": "booley.coverage-hook/v1",
                    "run_id": request.run_id,
                    "events": [{"hook": "start", "sequence": 1, "success": True}],
                }
            ),
            encoding="utf-8",
        )
        return SimulationRunResult(verdict="pass")


class _HookFailureExecution(_GeneratedMainExecution):
    def __init__(self, events: list[dict[str, object]]) -> None:
        self.events = events

    def run(self, request) -> SimulationRunResult:
        assert request.hook_evidence_path is not None
        request.raw_path.parent.mkdir(parents=True, exist_ok=True)
        request.raw_path.write_text(_HEADER + _LINE_POINT, encoding="utf-8")
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
        return SimulationRunResult(verdict="pass")


class _NoExecution:
    def build(self, request) -> SimulationBuildResult:
        raise AssertionError("invalid custom-main declaration must fail before build")

    def run(self, request) -> SimulationRunResult:
        raise AssertionError("invalid custom-main declaration must fail before simulation")

    def command(self, request) -> SimulationCommandResult:
        raise AssertionError("invalid custom-main declaration must fail before native tools")


class _CocotbExecution(_GeneratedMainExecution):
    def __init__(self) -> None:
        self.run_requests = []

    def run(self, request) -> SimulationRunResult:
        self.run_requests.append(request)
        expected = f"+verilator+coverage+file+{request.raw_path}"
        assert request.argv_suffix == (expected,)
        hits = len(self.run_requests)
        request.raw_path.parent.mkdir(parents=True, exist_ok=True)
        request.raw_path.write_text(
            _HEADER + _native_record("line", "block", hits=hits),
            encoding="utf-8",
        )
        return SimulationRunResult(verdict="pass" if hits == 1 else "fail")

    def command(self, request) -> SimulationCommandResult:
        request.output_path.parent.mkdir(parents=True, exist_ok=True)
        request.output_path.write_text(
            _HEADER + _native_record("line", "block", hits=3),
            encoding="utf-8",
        )
        return SimulationCommandResult(returncode=0)


class _MergeFailureExecution(_GeneratedMainExecution):
    def command(self, request) -> SimulationCommandResult:
        return SimulationCommandResult(returncode=2, stderr="merge rejected an input")


class _BadMergeExecution(_GeneratedMainExecution):
    def __init__(self, mode: str) -> None:
        self.mode = mode

    def command(self, request) -> SimulationCommandResult:
        if self.mode == "missing":
            return SimulationCommandResult(returncode=0)
        request.output_path.parent.mkdir(parents=True, exist_ok=True)
        if self.mode == "malformed":
            payload = _HEADER + "garbage\n"
        elif self.mode == "mismatch":
            payload = _HEADER + _native_record("line", "block", hits=999)
        else:
            payload = _HEADER + _LINE_POINT
        request.output_path.write_text(payload, encoding="utf-8")
        if self.mode == "stale":
            os.utime(request.output_path, ns=(1, 1))
        return SimulationCommandResult(returncode=0)


class _BuildFailureExecution(_NoExecution):
    def build(self, request) -> SimulationBuildResult:
        return SimulationBuildResult(success=False, output="%Error: syntax error")


class _WrongVerilatorExecution(_NoExecution):
    def build(self, request) -> SimulationBuildResult:
        return SimulationBuildResult(
            success=True,
            collector=VerilatorCollectorIdentity("v5.050", "0" * 40),
        )


def test_generated_main_collects_one_native_database_and_normalizes_line_point(
    tmp_path: Path,
) -> None:
    result = collect(_request(tmp_path, "reset"), _GeneratedMainExecution())

    assert result.status == "complete"
    _assert_build_evidence(result)
    _assert_line_point(result.points[0])
    assert [(run.test, run.simulation_verdict, run.collection) for run in result.runs] == [
        ("reset", "pass", "included")
    ]
    assert [artifact.kind for artifact in result.artifacts] == [
        "raw_native",
        "merged_native",
    ]
    assert result.merge.status == "equivalent"


def _assert_build_evidence(result) -> None:
    assert result.collector == VerilatorCollectorIdentity(
        tag="v5.052",
        commit="ea338be98e1e838d3518809ce8899f85a009963c",
    )
    assert result.build.variant.trace is False
    assert result.build.variant.coverage is True
    assert result.build.instrumentation == (
        "--coverage-line",
        "--coverage-toggle",
        "--coverage-expr",
        "--coverage-user",
        "--coverage-per-instance",
    )


def _assert_line_point(point) -> None:
    assert point.identity.metric == "line"
    assert point.identity.location == {
        "source": "rtl/counter.sv",
        "start": {"line": 10, "column": 3},
        "end": {"line": 10, "column": 3},
    }
    assert point.identity.hierarchy == "TOP.counter"
    assert point.identity.subject == {"basic_block": "block"}
    assert dict(point.hits_by_run) == {"run:001:reset": 2}
    assert point.disposition == {"kind": "eligible"}


def test_normalization_maps_fusesoc_staged_source_suffix(tmp_path: Path) -> None:
    request = CoverageCollectionRequest(
        target=CoverageTarget(
            identity="acme:demo:counter:1.0#sim_counter",
            selector="sim_counter",
            toplevel="counter",
            harness="generated_main",
            sources=(CoverageSource("rtl/counter.sv", "rtl/counter.sv", "rtl"),),
        ),
        selected_tests=(SelectedCoverageTest("wrap"),),
        artifact_root=tmp_path,
    )

    result = collect(request, _StagedSourceExecution())

    assert result.status == "complete"
    assert result.points[0].identity.location["source"] == "rtl/counter.sv"
    assert result.points[0].disposition["kind"] == "eligible"


def test_missing_raw_database_is_a_collector_error_without_losing_simulation_truth(
    tmp_path: Path,
) -> None:
    request = CoverageCollectionRequest(
        target=CoverageTarget(
            identity="acme:demo:counter:1.0#sim_counter",
            selector="sim_counter",
            toplevel="counter",
            harness="generated_main",
            sources=(),
        ),
        selected_tests=(SelectedCoverageTest("reset"),),
        artifact_root=tmp_path / "campaign",
    )

    result = collect(request, _MissingRawExecution())

    assert result.status == "collector_error"
    assert [(run.simulation_verdict, run.collection) for run in result.runs] == [
        ("pass", "collector_error")
    ]
    assert result.artifacts == ()
    assert result.merge.status == "not_run"
    assert [finding.code for finding in result.findings] == ["COV_RAW_FILE_MISSING"]


def test_stale_raw_database_is_retained_but_excluded_from_collection(tmp_path: Path) -> None:
    request = CoverageCollectionRequest(
        target=CoverageTarget(
            identity="acme:demo:counter:1.0#sim_counter",
            selector="sim_counter",
            toplevel="counter",
            harness="generated_main",
            sources=(CoverageSource("rtl/counter.sv", "rtl/counter.sv", "rtl"),),
        ),
        selected_tests=(SelectedCoverageTest("wrap"),),
        artifact_root=tmp_path / "campaign",
    )

    raw = request.artifact_root / "native" / "raw" / "001-wrap.dat"
    raw.parent.mkdir(parents=True)
    raw.write_text(_HEADER + _LINE_POINT, encoding="utf-8")

    result = collect(request, _StaleRawExecution())

    assert result.status == "collector_error"
    assert result.runs[0].simulation_verdict == "fail"
    assert result.runs[0].collection == "collector_error"
    assert [artifact.state for artifact in result.artifacts] == ["stale"]
    assert result.points == ()
    assert [finding.code for finding in result.findings] == ["COV_RAW_FILE_STALE"]


@pytest.mark.parametrize(
    "payload",
    [
        _HEADER + "C '\x01f\x02rtl/counter.sv\x01f\x02duplicate\x01t\x02line' 1\n",
        _HEADER + "C '\x01f\x02rtl/counter.sv\x01l\x02ten\x01t\x02line' 1\n",
        _HEADER.encode() + b"C '\x01t\x02line\xff' 1\n",
    ],
)
def test_malformed_native_attributes_are_structured_errors(
    tmp_path: Path, payload: str | bytes
) -> None:
    request = _request(tmp_path, "malformed")

    result = collect(request, _MalformedRawExecution(payload))

    assert result.status == "collector_error"
    assert result.runs[0].simulation_verdict == "timeout"
    assert [finding.code for finding in result.findings] == ["COV_RAW_NOT_QUERYABLE"]


def test_malformed_raw_database_is_unqueryable_and_preserves_timeout(tmp_path: Path) -> None:
    request = CoverageCollectionRequest(
        target=CoverageTarget(
            identity="acme:demo:counter:1.0#sim_counter",
            selector="sim_counter",
            toplevel="counter",
            harness="generated_main",
            sources=(),
        ),
        selected_tests=(SelectedCoverageTest("hang"),),
        artifact_root=tmp_path / "campaign",
    )

    result = collect(request, _MalformedRawExecution())

    assert result.status == "collector_error"
    assert result.runs[0].simulation_verdict == "timeout"
    assert result.runs[0].collection == "collector_error"
    assert [artifact.state for artifact in result.artifacts] == ["unqueryable"]
    assert [finding.code for finding in result.findings] == ["COV_RAW_NOT_QUERYABLE"]


def test_incompatible_native_format_blocks_normalization_with_stable_evidence(
    tmp_path: Path,
) -> None:
    request = CoverageCollectionRequest(
        target=CoverageTarget(
            identity="acme:demo:counter:1.0#sim_counter",
            selector="sim_counter",
            toplevel="counter",
            harness="generated_main",
            sources=(),
        ),
        selected_tests=(SelectedCoverageTest("reset"),),
        artifact_root=tmp_path / "campaign",
    )

    result = collect(request, _IncompatibleRawExecution())

    assert result.status == "collector_error"
    assert result.native_format.name == "verilator-coverage"
    assert result.native_format.compatibility == "incompatible"
    assert result.points == ()
    assert [artifact.state for artifact in result.artifacts] == ["incompatible"]
    assert [finding.code for finding in result.findings] == ["COV_NATIVE_FORMAT_INCOMPATIBLE"]


def test_known_deferred_and_unknown_record_classes_are_retained_losslessly(
    tmp_path: Path,
) -> None:
    request = CoverageCollectionRequest(
        target=CoverageTarget(
            identity="acme:demo:counter:1.0#sim_counter",
            selector="sim_counter",
            toplevel="counter",
            harness="generated_main",
            sources=(CoverageSource("rtl/counter.sv", "rtl/counter.sv", "rtl"),),
        ),
        selected_tests=(SelectedCoverageTest("all_metrics"),),
        artifact_root=tmp_path / "campaign",
    )

    result = collect(request, _RichNativeExecution())

    assert result.status == "complete"
    _assert_rich_points(result)
    _assert_rich_capabilities(result)
    assert [(finding.severity, finding.code) for finding in result.findings] == [
        ("warning", "COV_NATIVE_RECORD_UNKNOWN")
    ]


def _assert_rich_points(result) -> None:
    assert [point.identity.metric for point in result.points] == [
        "branch",
        "cover_property",
        "covergroup",
        "expression",
        "fsm",
        "line",
        "line",
        "toggle",
    ]
    assert [
        point.identity.hierarchy for point in result.points if point.identity.metric == "line"
    ] == [
        "TOP.first",
        "TOP.second",
    ]
    dispositions = {point.identity.metric: point.disposition["kind"] for point in result.points}
    assert dispositions["fsm"] == "unscored"
    assert dispositions["covergroup"] == "unscored"
    assert dispositions["toggle"] == "eligible"


def _assert_rich_capabilities(result) -> None:
    assert {capability.record_class: capability.status for capability in result.capabilities} == {
        "branch": "reported",
        "cover_property": "reported",
        "covergroup": "reported",
        "expression": "reported",
        "fsm": "reported",
        "future_kind": "reported",
        "line": "reported",
        "toggle": "reported",
    }


def test_supported_but_unobserved_record_classes_are_explicitly_absent(
    tmp_path: Path,
) -> None:
    result = collect(_request(tmp_path, "line_only"), _GeneratedMainExecution())

    statuses = {capability.record_class: capability.status for capability in result.capabilities}
    assert statuses == {
        "branch": "absent",
        "cover_property": "absent",
        "covergroup": "absent",
        "expression": "absent",
        "fsm": "absent",
        "line": "reported",
        "toggle": "absent",
    }


def test_post_reset_window_requires_one_fresh_successful_start_hook(tmp_path: Path) -> None:
    request = CoverageCollectionRequest(
        target=CoverageTarget(
            identity="acme:demo:counter:1.0#sim_counter",
            selector="sim_counter",
            toplevel="counter",
            harness="generated_main",
            sources=(CoverageSource("rtl/counter.sv", "rtl/counter.sv", "rtl"),),
        ),
        selected_tests=(SelectedCoverageTest("post_reset"),),
        artifact_root=tmp_path / "campaign",
        reset_included=False,
    )

    result = collect(request, _PostResetExecution())

    assert result.status == "complete"
    assert result.coverage_window.mode == "post_reset"
    assert [artifact.kind for artifact in result.artifacts] == [
        "raw_native",
        "coverage_hook_evidence",
        "merged_native",
    ]
    assert result.coverage_window.hook_artifacts == ("artifact:hook:001",)


def test_missing_post_reset_hook_evidence_is_a_structured_collector_error(
    tmp_path: Path,
) -> None:
    request = CoverageCollectionRequest(
        target=CoverageTarget(
            identity="acme:demo:counter:1.0#sim_counter",
            selector="sim_counter",
            toplevel="counter",
            harness="generated_main",
            sources=(CoverageSource("rtl/counter.sv", "rtl/counter.sv", "rtl"),),
        ),
        selected_tests=(SelectedCoverageTest("forgot_hook"),),
        artifact_root=tmp_path / "campaign",
        reset_included=False,
    )

    result = collect(request, _GeneratedMainExecution())

    assert result.status == "collector_error"
    assert result.runs[0].simulation_verdict == "pass"
    assert result.runs[0].collection == "collector_error"
    assert [artifact.kind for artifact in result.artifacts] == ["raw_native"]
    assert [finding.code for finding in result.findings] == ["COV_WINDOW_HOOK_MISSING"]
    assert result.merge.status == "not_run"


def test_unchanged_prior_hook_evidence_is_rejected_as_stale(tmp_path: Path) -> None:
    request = _request(tmp_path, "post_reset", reset_included=False)
    hook = request.artifact_root / "hooks" / "001-post_reset.json"
    hook.parent.mkdir(parents=True)
    hook.write_text(
        json.dumps(
            {
                "$schema": "booley.coverage-hook/v1",
                "run_id": "run:001:post_reset",
                "events": [{"hook": "start", "sequence": 1, "success": True}],
            }
        ),
        encoding="utf-8",
    )

    result = collect(request, _GeneratedMainExecution())

    assert result.status == "collector_error"
    assert [finding.code for finding in result.findings] == ["COV_WINDOW_HOOK_MISSING"]


@pytest.mark.parametrize(
    ("events", "expected_code"),
    [
        (
            [
                {"hook": "start", "sequence": 1, "success": True},
                {"hook": "start", "sequence": 2, "success": True},
            ],
            "COV_WINDOW_HOOK_DUPLICATE",
        ),
        (
            [{"hook": "start", "sequence": 1, "success": False}],
            "COV_WINDOW_HOOK_FAILED",
        ),
        (
            [{"hook": "start", "sequence": "first", "success": True}],
            "COV_WINDOW_HOOK_INVALID",
        ),
    ],
)
def test_invalid_start_hook_is_a_collector_error(
    tmp_path: Path,
    events: list[dict[str, object]],
    expected_code: str,
) -> None:
    request = CoverageCollectionRequest(
        target=CoverageTarget(
            identity="acme:demo:counter:1.0#sim_counter",
            selector="sim_counter",
            toplevel="counter",
            harness="generated_main",
            sources=(CoverageSource("rtl/counter.sv", "rtl/counter.sv", "rtl"),),
        ),
        selected_tests=(SelectedCoverageTest("bad_hook"),),
        artifact_root=tmp_path / "campaign",
        reset_included=False,
    )

    result = collect(request, _HookFailureExecution(events))

    assert result.status == "collector_error"
    assert [finding.code for finding in result.findings] == [expected_code]
    assert result.runs[0].simulation_verdict == "pass"


@pytest.mark.parametrize(
    ("hooks", "reset_included", "expected_code"),
    [
        ((), True, "COV_CUSTOM_MAIN_WRITE_HOOK_REQUIRED"),
        (("write_hook",), False, "COV_CUSTOM_MAIN_START_HOOK_REQUIRED"),
        (("write_hook", "start_hook"), True, "COV_CUSTOM_MAIN_START_HOOK_UNNECESSARY"),
        (("write_hook", "write_hook"), True, "COV_CUSTOM_MAIN_HOOK_DUPLICATE"),
        (("write_hook", "mystery"), True, "COV_CUSTOM_MAIN_HOOK_UNKNOWN"),
    ],
)
def test_invalid_custom_main_hook_declarations_fail_before_execution(
    tmp_path: Path,
    hooks: tuple[str, ...],
    reset_included: bool,
    expected_code: str,
) -> None:
    request = CoverageCollectionRequest(
        target=CoverageTarget(
            identity="acme:demo:counter:1.0#sim_counter",
            selector="sim_counter",
            toplevel="counter",
            harness="custom_main",
            sources=(),
            custom_main_hooks=hooks,
        ),
        selected_tests=(SelectedCoverageTest("custom"),),
        artifact_root=tmp_path / "campaign",
        reset_included=reset_included,
    )

    result = collect(request, _NoExecution())

    assert result.status == "collector_error"
    assert result.runs == ()
    assert [finding.code for finding in result.findings] == [expected_code]


def test_non_custom_target_rejects_custom_main_hooks_before_execution(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path, "generated", hooks=("write_hook",))

    result = collect(request, _NoExecution())

    assert result.status == "collector_error"
    assert result.runs == ()
    assert [finding.code for finding in result.findings] == ["COV_CUSTOM_MAIN_HOOK_FORBIDDEN"]


def test_custom_main_requires_one_successful_runtime_write_hook(tmp_path: Path) -> None:
    request = CoverageCollectionRequest(
        target=CoverageTarget(
            identity="acme:demo:counter:1.0#sim_counter",
            selector="sim_counter",
            toplevel="counter",
            harness="custom_main",
            sources=(CoverageSource("rtl/counter.sv", "rtl/counter.sv", "rtl"),),
            custom_main_hooks=("write_hook",),
        ),
        selected_tests=(SelectedCoverageTest("custom"),),
        artifact_root=tmp_path / "campaign",
    )

    result = collect(
        request,
        _HookFailureExecution([{"hook": "write", "sequence": 1, "success": True}]),
    )

    assert result.status == "complete"
    assert result.runs[0].collection == "included"
    assert result.coverage_window.mode == "whole_run"
    assert result.coverage_window.hook_artifacts == ("artifact:hook:001",)


def test_cocotb_trace_coverage_runs_one_process_per_test_and_keeps_failed_run_data(
    tmp_path: Path,
) -> None:
    execution = _CocotbExecution()
    request = CoverageCollectionRequest(
        target=CoverageTarget(
            identity="acme:demo:counter:1.0#sim_counter",
            selector="sim_counter",
            toplevel="counter",
            harness="cocotb",
            sources=(CoverageSource("rtl/counter.sv", "rtl/counter.sv", "rtl"),),
        ),
        selected_tests=(SelectedCoverageTest("increments"), SelectedCoverageTest("wraps")),
        artifact_root=tmp_path / "campaign",
        trace=True,
    )

    result = collect(request, execution)

    assert result.status == "complete"
    assert result.build.variant.name == "trace-coverage"
    assert [run.simulation_verdict for run in result.runs] == ["pass", "fail"]
    assert len(execution.run_requests) == 2
    assert execution.run_requests[0].raw_path != execution.run_requests[1].raw_path
    assert dict(result.points[0].hits_by_run) == {
        "run:001:increments": 1,
        "run:002:wraps": 2,
    }


def test_native_merge_tool_failure_retains_normalized_per_run_evidence(tmp_path: Path) -> None:
    request = CoverageCollectionRequest(
        target=CoverageTarget(
            identity="acme:demo:counter:1.0#sim_counter",
            selector="sim_counter",
            toplevel="counter",
            harness="generated_main",
            sources=(CoverageSource("rtl/counter.sv", "rtl/counter.sv", "rtl"),),
        ),
        selected_tests=(SelectedCoverageTest("reset"),),
        artifact_root=tmp_path / "campaign",
    )

    result = collect(request, _MergeFailureExecution())

    assert result.status == "collector_error"
    assert result.runs[0].collection == "included"
    assert [artifact.kind for artifact in result.artifacts] == ["raw_native"]
    assert len(result.points) == 1
    assert result.merge.status == "failed"
    assert [finding.code for finding in result.findings] == ["COV_NATIVE_MERGE_FAILED"]


@pytest.mark.parametrize(
    ("mode", "expected_code", "merge_status", "merged_state"),
    [
        ("missing", "COV_NATIVE_MERGE_MISSING", "failed", None),
        ("stale", "COV_NATIVE_MERGE_STALE", "failed", "stale"),
        ("malformed", "COV_NATIVE_MERGE_NOT_QUERYABLE", "failed", "unqueryable"),
        ("mismatch", "COV_NATIVE_MERGE_MISMATCH", "mismatch", "fresh_queryable"),
    ],
)
def test_bad_native_merge_never_discards_per_run_evidence(
    tmp_path: Path,
    mode: str,
    expected_code: str,
    merge_status: str,
    merged_state: str | None,
) -> None:
    request = CoverageCollectionRequest(
        target=CoverageTarget(
            identity="acme:demo:counter:1.0#sim_counter",
            selector="sim_counter",
            toplevel="counter",
            harness="generated_main",
            sources=(CoverageSource("rtl/counter.sv", "rtl/counter.sv", "rtl"),),
        ),
        selected_tests=(SelectedCoverageTest("reset"),),
        artifact_root=tmp_path / "campaign",
    )
    if mode == "stale":
        merged = request.artifact_root / "native" / "merged" / "coverage.dat"
        merged.parent.mkdir(parents=True)
        merged.write_text(_HEADER + _LINE_POINT, encoding="utf-8")
        os.utime(merged, ns=(1, 1))

    result = collect(request, _BadMergeExecution(mode))

    assert result.status == "collector_error"
    assert len(result.points) == 1
    assert result.merge.status == merge_status
    assert [finding.code for finding in result.findings] == [expected_code]
    merged = [artifact for artifact in result.artifacts if artifact.kind == "merged_native"]
    assert [artifact.state for artifact in merged] == (
        [] if merged_state is None else [merged_state]
    )


def test_build_failure_reports_elaboration_truth_for_every_selected_test(tmp_path: Path) -> None:
    request = CoverageCollectionRequest(
        target=CoverageTarget(
            identity="acme:demo:counter:1.0#sim_counter",
            selector="sim_counter",
            toplevel="counter",
            harness="generated_main",
            sources=(),
        ),
        selected_tests=(SelectedCoverageTest("a"), SelectedCoverageTest("b")),
        artifact_root=tmp_path / "campaign",
    )

    result = collect(request, _BuildFailureExecution())

    assert result.status == "collector_error"
    assert [(run.test, run.simulation_verdict, run.collection) for run in result.runs] == [
        ("a", "elab_error", "collector_error"),
        ("b", "elab_error", "collector_error"),
    ]
    assert result.artifacts == ()
    assert [finding.code for finding in result.findings] == ["COV_COVERAGE_BUILD_FAILED"]


def test_collector_rejects_any_verilator_other_than_the_exact_safe_pin(tmp_path: Path) -> None:
    request = CoverageCollectionRequest(
        target=CoverageTarget(
            identity="acme:demo:counter:1.0#sim_counter",
            selector="sim_counter",
            toplevel="counter",
            harness="generated_main",
            sources=(),
        ),
        selected_tests=(SelectedCoverageTest("reset"),),
        artifact_root=tmp_path / "campaign",
    )

    result = collect(request, _WrongVerilatorExecution())

    assert result.status == "collector_error"
    assert result.runs == ()
    assert [finding.code for finding in result.findings] == ["COV_VERILATOR_IDENTITY_MISMATCH"]

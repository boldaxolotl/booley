from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from booley.flows import job_resources
from booley.flows.base import SubprocessResult
from booley.flows.sim.execution.contract import PreSimEvidence
from booley.flows.sim.verilator_coverage import (
    PINNED_VERILATOR,
    CoverageCollectionRequest,
    CoverageSource,
    CoverageTarget,
    SelectedCoverageRun,
    SimulationBuildResult,
    SimulationCommandResult,
    SimulationRunResult,
    VerilatorCollectorIdentity,
    collect,
)
from booley.flows.test_runs import build_test_run
from booley.runtime import resource_history
from tests.canned_process_support import canned_command, measured_flow
from tests.conftest import symlink_or_skip

_HEADER = "# SystemC::Coverage-3\n"
_LINE_POINT = (
    "C '\x01f\x02rtl/counter.sv\x01l\x0210\x01n\x023\x01h\x02TOP.counter"
    "\x01t\x02line\x01o\x02block' 2\n"
)


_START = {"hook": "start", "sequence": 1, "success": True}
_SOURCES = (CoverageSource("rtl/counter.sv", "rtl/counter.sv", "rtl"),)


def _request(
    tmp_path: Path,
    *tests: str | SelectedCoverageRun,
    harness: str = "generated_main",
    hooks: tuple[str, ...] = (),
    reset_included: bool = True,
    sources: tuple[CoverageSource, ...] = _SOURCES,
    trace: bool = False,
    artifact_root: Path | None = None,
) -> CoverageCollectionRequest:
    return CoverageCollectionRequest(
        target=CoverageTarget(
            identity="acme:demo:counter:1.0#sim_counter",
            selector="sim_counter",
            toplevel="counter",
            harness=harness,
            sources=sources,
            custom_main_hooks=hooks,
        ),
        selected_tests=tuple(
            SelectedCoverageRun(test) if isinstance(test, str) else test for test in tests
        ),
        artifact_root=artifact_root or tmp_path / "record",
        reset_included=reset_included,
        trace=trace,
    )


def _native_entry(
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


def _collection_detail(result) -> str:
    """Describe a collector result for a failed status assertion."""
    findings = [(finding.code, finding.message) for finding in result.findings]
    return f"findings={findings!r} infrastructure_error={result.infrastructure_error}"


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


class _PresenceExecution(_GeneratedMainExecution):
    """Writes one caller-chosen payload as both the raw and the merged database."""

    def __init__(self, payload: str) -> None:
        self.payload = payload

    def run(self, request) -> SimulationRunResult:
        request.raw_path.parent.mkdir(parents=True, exist_ok=True)
        request.raw_path.write_text(self.payload, encoding="utf-8")
        return SimulationRunResult("pass")

    def command(self, request) -> SimulationCommandResult:
        request.output_path.parent.mkdir(parents=True, exist_ok=True)
        request.output_path.write_text(self.payload, encoding="utf-8")
        return SimulationCommandResult(0)


class _MissingRawExecution(_GeneratedMainExecution):
    def run(self, request) -> SimulationRunResult:
        return SimulationRunResult(verdict="pass")

    def command(self, request) -> SimulationCommandResult:
        raise AssertionError("merge must not run without every raw database")


class _UnsafeRawExecution(_MissingRawExecution):
    """Leaves the raw database path as a symlink out of the root or as a directory."""

    def __init__(self, mode: str, outside: Path) -> None:
        self.mode = mode
        self.outside = outside

    def run(self, request) -> SimulationRunResult:
        request.raw_path.parent.mkdir(parents=True, exist_ok=True)
        _make_unsafe(self.mode, request.raw_path, self.outside)
        return SimulationRunResult(verdict="pass")


def _make_unsafe(mode: str, path: Path, outside: Path) -> None:
    """Make *path* a symlink to a valid file outside the root, or a directory."""
    if mode == "directory":
        path.mkdir()
        return
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_text(_HEADER + _LINE_POINT, encoding="utf-8")
    symlink_or_skip(path, outside)


class _StaleRawExecution(_MissingRawExecution):
    def run(self, request) -> SimulationRunResult:
        return SimulationRunResult(verdict="fail")


class _MalformedRawExecution(_MissingRawExecution):
    def __init__(self, payload: str | bytes = _HEADER + "not a native counter entry\n") -> None:
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
            _native_entry("line", "block", hierarchy="TOP.first"),
            _native_entry("line", "block", hierarchy="TOP.second"),
            _native_entry("branch", "if"),
            _native_entry("expr", "(enable == 1) => 1"),
            _native_entry("toggle", "count[0]:0->1"),
            _native_entry("user", "wrap_seen"),
            _native_entry("fsm", "RUN->WRAP"),
            _native_entry("covergroup", "values.high"),
            _native_entry("future_kind", "opaque"),
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


class _StagedSourceExecution(_PresenceExecution):
    def __init__(self) -> None:
        super().__init__(
            _HEADER
            + _LINE_POINT.replace(
                "\x01f\x02rtl/counter.sv", "\x01f\x02/build/src/acme_demo_counter_1/rtl/counter.sv"
            )
        )


class _HookFailureExecution(_GeneratedMainExecution):
    def __init__(self, events: list[dict[str, object]] | None) -> None:
        self.events = events

    def run(self, request) -> SimulationRunResult:
        assert request.hook_evidence_path is not None
        request.raw_path.parent.mkdir(parents=True, exist_ok=True)
        request.raw_path.write_text(_HEADER + _LINE_POINT, encoding="utf-8")
        if self.events is None:
            return SimulationRunResult(verdict="pass")
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


class _HookAndRawFailureExecution(_HookFailureExecution):
    def __init__(self, events: list[dict[str, object]] | None, raw_payload: str | None) -> None:
        super().__init__(events)
        self.raw_payload = raw_payload

    def run(self, request) -> SimulationRunResult:
        result = super().run(request)
        if self.raw_payload is None:
            request.raw_path.unlink()
        else:
            request.raw_path.write_text(self.raw_payload, encoding="utf-8")
        return result


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
            _HEADER + _native_entry("line", "block", hits=hits), encoding="utf-8"
        )
        return SimulationRunResult(verdict="pass" if hits == 1 else "fail")

    def command(self, request) -> SimulationCommandResult:
        request.output_path.parent.mkdir(parents=True, exist_ok=True)
        request.output_path.write_text(
            _HEADER + _native_entry("line", "block", hits=3), encoding="utf-8"
        )
        return SimulationCommandResult(returncode=0)


class _MergeFailureExecution(_GeneratedMainExecution):
    def command(self, request) -> SimulationCommandResult:
        return SimulationCommandResult(returncode=2, stderr="merge rejected an input")


class _BadMergeExecution(_GeneratedMainExecution):
    def __init__(self, mode: str, outside: Path) -> None:
        self.mode = mode
        self.outside = outside

    def command(self, request) -> SimulationCommandResult:
        if self.mode == "missing":
            return SimulationCommandResult(returncode=0)
        request.output_path.parent.mkdir(parents=True, exist_ok=True)
        if self.mode in {"outside", "directory"}:
            _make_unsafe(self.mode, request.output_path, self.outside)
            return SimulationCommandResult(returncode=0)
        if self.mode == "malformed":
            payload = _HEADER + "garbage\n"
        elif self.mode == "mismatch":
            payload = _HEADER + _native_entry("line", "block", hits=999)
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
            success=True, collector=VerilatorCollectorIdentity("v5.050", "0" * 40)
        )


class _PreSimExecution(_GeneratedMainExecution):
    def __init__(self, status: str) -> None:
        self.status = status
        self.tests = []

    def run(self, request) -> SimulationRunResult:
        self.tests.append(request.test.name)
        evidence = PreSimEvidence(
            ("prepare-vectors",),
            (request.test.name,),
            self.status if len(self.tests) == 1 else "passed",
            0.25,
            "missing-generator" if self.status == "spawn_error" else "bad vectors",
        )
        if evidence.status != "passed":
            return SimulationRunResult(
                "elab_error",
                evidence.detail,
                evidence,
                infrastructure_error=evidence.status == "spawn_error",
            )
        result = super().run(request)
        return replace(result, pre_sim=evidence)

    def command(self, request) -> SimulationCommandResult:
        if self.status == "passed":
            return super().command(request)
        raise AssertionError("merge must not run after a Pre-Sim failure")


def test_pre_sim_failure_is_attributed_and_later_tests_continue(tmp_path: Path) -> None:
    request = replace(
        _request(tmp_path, "first"),
        selected_tests=(SelectedCoverageRun("first"), SelectedCoverageRun("second")),
    )
    execution = _PreSimExecution("failed")

    result = collect(request, execution)

    assert execution.tests == ["first", "second"]
    assert result.status == "collector_error"
    assert result.infrastructure_error is False
    assert [run.simulation_verdict for run in result.runs] == ["elab_error", "pass"]
    assert result.runs[0].attributes["pre_sim"]["status"] == "failed"
    assert [finding.code for finding in result.findings] == ["COV_PRE_SIM_FAILED"]
    assert all(finding.code != "COV_RAW_FILE_MISSING" for finding in result.findings)


def test_pre_sim_spawn_error_aborts_later_tests(tmp_path: Path) -> None:
    request = replace(
        _request(tmp_path, "first"),
        selected_tests=(SelectedCoverageRun("first"), SelectedCoverageRun("second")),
    )
    execution = _PreSimExecution("spawn_error")

    result = collect(request, execution)

    assert execution.tests == ["first"]
    assert result.infrastructure_error is True
    assert [run.simulation_verdict for run in result.runs] == ["elab_error", "inconclusive"]
    assert result.runs[1].attributes == {
        "execution": "not_run",
        "failure_kind": "infrastructure",
        "error_tail": "not run after an earlier infrastructure abort",
    }
    assert "missing-generator" in result.findings[-1].message


def test_design_abort_retains_termination_without_reading_coverage(tmp_path: Path) -> None:
    class MissingInputExecution(_GeneratedMainExecution):
        def run(self, request) -> SimulationRunResult:
            return SimulationRunResult(
                "fail",
                "$readmemh could not open vectors.hex",
                termination="fatal_init",
                failure_kind="missing_input",
            )

        def command(self, request) -> SimulationCommandResult:
            raise AssertionError("merge must not run after a Simulation abort")

    result = collect(_request(tmp_path, "missing_vectors"), MissingInputExecution())

    assert result.infrastructure_error is False
    assert result.status == "collector_error"
    assert result.runs[0].attributes["termination"] == "fatal_init"
    assert result.runs[0].attributes["failure_kind"] == "missing_input"
    assert result.findings[0].code == "COV_SIMULATION_ABORTED"


def test_protected_surface_failure_takes_priority_over_failed_hook(tmp_path: Path) -> None:
    class ChangedImage(_GeneratedMainExecution):
        def run(self, request) -> SimulationRunResult:
            evidence = PreSimEvidence(
                ("mutate-and-fail",), (request.test.name,), "failed", 0.25, "hook exited 1"
            )
            return SimulationRunResult(
                "inconclusive",
                "coverage image verification failed: image changed",
                evidence,
                infrastructure_error=True,
            )

        def command(self, request) -> SimulationCommandResult:
            raise AssertionError("merge must not run after image verification failure")

    result = collect(_request(tmp_path, "first"), ChangedImage())

    assert result.infrastructure_error is True
    assert result.findings[0].code == "COV_INFRASTRUCTURE_ERROR"
    assert "image changed" in result.findings[0].message
    assert result.runs[0].attributes["pre_sim"]["status"] == "failed"


def test_successful_pre_sim_evidence_is_serialized_on_the_run(tmp_path: Path) -> None:
    execution = _PreSimExecution("passed")

    result = collect(_request(tmp_path, "reset"), execution)

    assert result.status == "complete", _collection_detail(result)
    assert result.runs[0].attributes["pre_sim"]["status"] == "passed"
    assert result.runs[0].attributes["pre_sim"]["test_names"] == ("reset",)


def test_generated_main_collects_one_native_database_and_normalizes_line_point(
    tmp_path: Path,
) -> None:
    result = collect(_request(tmp_path, "reset"), _GeneratedMainExecution())

    assert result.status == "complete", _collection_detail(result)
    _assert_build_evidence(result)
    _assert_line_point(result.points[0])
    assert [(run.test, run.simulation_verdict, run.collection) for run in result.runs] == [
        ("reset", "pass", "included")
    ]
    assert [artifact.kind for artifact in result.artifacts] == ["raw_native", "merged_native"]
    assert result.merge.status == "equivalent"


def _assert_build_evidence(result) -> None:
    assert result.collector == PINNED_VERILATOR
    assert result.build.variant.trace is False
    assert result.build.variant.coverage is True
    assert result.build.instrumentation == (
        "--coverage-line",
        "--coverage-branch",
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
    request = _request(tmp_path, "wrap", artifact_root=tmp_path)

    result = collect(request, _StagedSourceExecution())

    assert result.status == "complete", _collection_detail(result)
    assert result.points[0].identity.location["source"] == "rtl/counter.sv"
    assert result.points[0].disposition["kind"] == "eligible"


def test_missing_raw_database_is_a_collector_error_without_losing_simulation_truth(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path, "reset", sources=(), harness="custom_main", hooks=("write_hook",))
    events = [{"hook": "write", "sequence": 1, "success": True}]

    result = collect(request, _HookAndRawFailureExecution(events, None))

    assert result.status == "collector_error"
    assert [(run.simulation_verdict, run.collection) for run in result.runs] == [
        ("pass", "collector_error")
    ]
    assert [artifact.kind for artifact in result.artifacts] == ["coverage_hook_evidence"]
    assert result.merge.status == "not_run"
    assert [finding.code for finding in result.findings] == ["COV_RAW_FILE_MISSING"]


@pytest.mark.parametrize(
    ("mode", "message_fragment"), [("outside", "escapes"), ("directory", "not a regular file")]
)
def test_unsafe_raw_database_is_reported_as_unsafe(
    tmp_path: Path, mode: str, message_fragment: str
) -> None:
    request = _request(tmp_path, "reset", sources=())

    result = collect(request, _UnsafeRawExecution(mode, tmp_path / "outside" / "raw.dat"))

    assert result.status == "collector_error"
    assert [run.collection for run in result.runs] == ["collector_error"]
    assert [finding.code for finding in result.findings] == ["COV_RAW_FILE_UNSAFE"]
    assert message_fragment in result.findings[0].message
    # Nothing outside the artifact root is hashed or recorded.
    assert result.artifacts == ()


@pytest.mark.parametrize(
    ("expected_code", "reset_included"),
    [("COV_RAW_FILE_STALE", True), ("COV_WINDOW_HOOK_MISSING", False)],
)
def test_stale_raw_database_is_retained_but_excluded_from_collection(
    tmp_path: Path, expected_code: str, reset_included: bool
) -> None:
    request = _request(tmp_path, "wrap", reset_included=reset_included)

    raw = request.artifact_root / "native" / "raw" / "001-wrap.dat"
    raw.parent.mkdir(parents=True)
    raw.write_text(_HEADER + _LINE_POINT, encoding="utf-8")

    result = collect(request, _StaleRawExecution())

    assert result.status == "collector_error"
    assert result.runs[0].simulation_verdict == "fail"
    assert result.runs[0].collection == "collector_error"
    assert [artifact.state for artifact in result.artifacts] == ["stale"]
    assert result.points == ()
    assert [finding.code for finding in result.findings] == [expected_code]


@pytest.mark.parametrize(
    "payload",
    [
        _HEADER + "not a native counter entry\n",
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
    assert result.runs[0].collection == "collector_error"
    assert [artifact.state for artifact in result.artifacts] == ["unqueryable"]
    assert [finding.code for finding in result.findings] == ["COV_RAW_NOT_QUERYABLE"]


def test_incompatible_native_format_blocks_normalization_with_stable_evidence(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path, "reset", sources=())

    result = collect(request, _IncompatibleRawExecution())

    assert result.status == "collector_error"
    assert result.native_format.name == "verilator-coverage"
    assert result.native_format.compatibility == "incompatible"
    assert result.points == ()
    assert [artifact.state for artifact in result.artifacts] == ["incompatible"]
    assert [finding.code for finding in result.findings] == ["COV_NATIVE_FORMAT_INCOMPATIBLE"]


def test_known_deferred_and_unknown_record_classes_are_retained_losslessly(tmp_path: Path) -> None:
    request = _request(tmp_path, "all_metrics")

    result = collect(request, _RichNativeExecution())

    assert result.status == "complete", _collection_detail(result)
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
    ] == ["TOP.first", "TOP.second"]
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
        "native:future_kind": "reported",
        "line": "reported",
        "toggle": "reported",
    }


def test_supported_but_unobserved_record_classes_are_explicitly_absent(tmp_path: Path) -> None:
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
    request = _request(tmp_path, "post_reset", reset_included=False)

    result = collect(
        request, _HookFailureExecution([{"hook": "start", "sequence": 1, "success": True}])
    )

    assert result.status == "complete", _collection_detail(result)
    assert result.coverage_window.mode == "post_reset"
    assert [artifact.kind for artifact in result.artifacts] == [
        "raw_native",
        "coverage_hook_evidence",
        "merged_native",
    ]
    assert result.coverage_window.hook_artifacts == ("artifact:hook:001",)


def test_missing_post_reset_hook_evidence_is_a_structured_collector_error(tmp_path: Path) -> None:
    request = _request(tmp_path, "forgot_hook", reset_included=False)

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
        ([{"hook": "start", "sequence": 1, "success": False}], "COV_WINDOW_HOOK_FAILED"),
        ([{"hook": "start", "sequence": "first", "success": True}], "COV_WINDOW_HOOK_INVALID"),
    ],
)
def test_invalid_start_hook_is_a_collector_error(
    tmp_path: Path, events: list[dict[str, object]], expected_code: str
) -> None:
    request = _request(tmp_path, "bad_hook", reset_included=False)

    result = collect(request, _HookFailureExecution(events))

    assert result.status == "collector_error"
    assert [finding.code for finding in result.findings] == [expected_code]
    assert result.runs[0].simulation_verdict == "pass"


_INVALID_HOOK_CASES = [
    ("generated_main", ("write_hook",), True, "COV_CUSTOM_MAIN_HOOK_FORBIDDEN", "remove it"),
    (
        "custom_main",
        ("write_hook", "write_hook"),
        True,
        "COV_CUSTOM_MAIN_HOOK_DUPLICATE",
        "more than once",
    ),
    (
        "custom_main",
        ("write_hook", "mystery"),
        True,
        "COV_CUSTOM_MAIN_HOOK_UNKNOWN",
        "['mystery']",
    ),
    (
        "custom_main",
        (),
        True,
        "COV_CUSTOM_MAIN_WRITE_HOOK_REQUIRED",
        "booley_coverage_write() once on every normal exit path",
    ),
    (
        "custom_main",
        ("write_hook",),
        False,
        "COV_CUSTOM_MAIN_START_HOOK_REQUIRED",
        "booley_coverage_start() once after reset",
    ),
    (
        "custom_main",
        ("write_hook", "start_hook"),
        True,
        "COV_CUSTOM_MAIN_START_HOOK_UNNECESSARY",
        "reset_included: false",
    ),
]


def test_invalid_custom_main_hook_declarations_fail_before_execution(tmp_path: Path) -> None:
    for index, (harness, hooks, reset_included, expected_code, expected_fix) in enumerate(
        _INVALID_HOOK_CASES
    ):
        request = _request(
            tmp_path / str(index),
            "custom",
            harness=harness,
            hooks=hooks,
            reset_included=reset_included,
            sources=(),
        )

        result = collect(request, _NoExecution())

        assert result.status == "collector_error", expected_code
        assert result.runs == ()
        assert [finding.code for finding in result.findings] == [expected_code]
        message = result.findings[0].message
        assert message.startswith("sim_counter: "), expected_code
        assert "flow_options.booley.coverage.custom_main_hooks" in message, expected_code
        assert expected_fix in message, expected_code


def test_custom_main_requires_one_successful_runtime_write_hook(tmp_path: Path) -> None:
    request = _request(tmp_path, "custom", harness="custom_main", hooks=("write_hook",))

    result = collect(
        request, _HookFailureExecution([{"hook": "write", "sequence": 1, "success": True}])
    )

    assert result.status == "complete", _collection_detail(result)
    assert result.runs[0].collection == "included"
    assert result.coverage_window.mode == "whole_run"
    assert result.coverage_window.hook_artifacts == ("artifact:hook:001",)


@pytest.mark.parametrize("raw_payload", [None, "# SystemC::Coverage-4\n"])
def test_failed_custom_main_write_hook_precedes_raw_database_failure(
    tmp_path: Path, raw_payload: str | None
) -> None:
    request = _request(
        tmp_path,
        "failed_write",
        harness="custom_main",
        hooks=("start_hook", "write_hook"),
        reset_included=False,
    )
    events = [
        {"hook": "start", "sequence": 1, "success": True},
        {"hook": "write", "sequence": 2, "success": False},
    ]

    result = collect(request, _HookAndRawFailureExecution(events, raw_payload))

    assert result.status == "collector_error"
    assert result.runs[0].collection == "collector_error"
    assert [finding.code for finding in result.findings] == ["COV_WRITE_HOOK_FAILED"]
    assert result.native_format.compatibility == "unknown"
    assert result.merge.status == "not_run"
    assert result.coverage_window.hook_artifacts == ("artifact:hook:001",)
    raw_artifacts = [artifact for artifact in result.artifacts if artifact.kind == "raw_native"]
    assert [artifact.state for artifact in raw_artifacts] == (
        [] if raw_payload is None else ["write_failed"]
    )
    hook = next(
        artifact for artifact in result.artifacts if artifact.kind == "coverage_hook_evidence"
    )
    document = json.loads((request.artifact_root / hook.path).read_text(encoding="utf-8"))
    assert document["events"][-1] == {"hook": "write", "sequence": 2, "success": False}


@pytest.mark.parametrize(
    ("events", "expected_code", "reset_included"),
    [
        (
            [
                {"hook": "write", "sequence": 1, "success": True},
                {"hook": "start", "sequence": 2, "success": True},
                {"hook": "write", "sequence": 3, "success": True},
            ],
            "COV_CUSTOM_MAIN_HOOK_OUT_OF_ORDER",
            False,
        ),
        (
            [
                {"hook": "start", "sequence": 1, "success": True},
                {"hook": "write", "sequence": 2, "success": True},
                {"hook": "write", "sequence": 3, "success": True},
            ],
            "COV_WRITE_HOOK_DUPLICATE",
            False,
        ),
        (
            [
                {"hook": "write", "sequence": 1, "success": False},
                {"hook": "start", "sequence": 2, "success": True},
            ],
            "COV_WRITE_HOOK_FAILED",
            False,
        ),
        (
            [
                {"hook": "write", "sequence": 1, "success": True},
                {"hook": "start", "sequence": 2, "success": False},
            ],
            "COV_WINDOW_HOOK_FAILED",
            False,
        ),
        (
            [
                {"hook": "write", "sequence": 2, "success": True},
                {"hook": "start", "sequence": 2, "success": True},
            ],
            "COV_CUSTOM_MAIN_HOOK_OUT_OF_ORDER",
            False,
        ),
        (
            [
                {"hook": "start", "sequence": "first", "success": True},
                {"hook": "write", "sequence": 2, "success": True},
            ],
            "COV_WINDOW_HOOK_INVALID",
            False,
        ),
        (
            [
                {"hook": "start", "sequence": 1, "success": True},
                {"hook": "write", "sequence": "first", "success": True},
            ],
            "COV_WINDOW_HOOK_INVALID",
            False,
        ),
        (
            [
                {"hook": "start", "sequence": 1, "success": True},
                {"hook": "write", "sequence": 2, "success": True},
                {"hook": "write", "sequence": "later", "success": True},
            ],
            "COV_WRITE_HOOK_DUPLICATE",
            False,
        ),
        (
            [
                {"hook": "write", "sequence": 1, "success": True},
                {"hook": "start", "sequence": 2, "success": True},
                {"hook": "write", "sequence": 3, "success": True},
            ],
            "COV_WRITE_HOOK_DUPLICATE",
            True,
        ),
        ([_START], "COV_WRITE_HOOK_MISSING", False),
        ([_START, _START], "COV_WINDOW_HOOK_DUPLICATE", False),
        (None, "COV_WINDOW_HOOK_MISSING", False),
        (None, "COV_WRITE_HOOK_MISSING", True),
    ],
)
@pytest.mark.parametrize("raw_payload", [_HEADER + _LINE_POINT, None], ids=["raw", "no_raw"])
def test_post_reset_custom_main_hook_event_precedence(
    tmp_path: Path,
    events: list[dict[str, object]] | None,
    expected_code: str,
    reset_included: bool,
    raw_payload: str | None,
) -> None:
    request = _request(
        tmp_path,
        "ordered_hooks",
        harness="custom_main",
        hooks=("write_hook",) if reset_included else ("start_hook", "write_hook"),
        reset_included=reset_included,
    )

    result = collect(request, _HookAndRawFailureExecution(events, raw_payload))

    assert result.status == "collector_error"
    assert [finding.code for finding in result.findings] == [expected_code]


def test_cocotb_trace_coverage_runs_one_process_per_test_and_keeps_failed_run_data(
    tmp_path: Path,
) -> None:
    execution = _CocotbExecution()
    request = _request(tmp_path, "increments", "wraps", harness="cocotb", trace=True)

    result = collect(request, execution)

    assert result.status == "complete", _collection_detail(result)
    assert result.build.variant.name == "trace-coverage"
    assert [run.simulation_verdict for run in result.runs] == ["pass", "fail"]
    assert len(execution.run_requests) == 2
    assert execution.run_requests[0].raw_path != execution.run_requests[1].raw_path
    assert dict(result.points[0].hits_by_run) == {"run:001:increments": 1, "run:002:wraps": 2}


def test_native_merge_tool_failure_retains_normalized_per_run_evidence(tmp_path: Path) -> None:
    request = _request(tmp_path, "reset")

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
        ("outside", "COV_NATIVE_MERGE_UNSAFE", "failed", None),
        ("directory", "COV_NATIVE_MERGE_UNSAFE", "failed", None),
    ],
)
def test_bad_native_merge_never_discards_per_run_evidence(
    tmp_path: Path, mode: str, expected_code: str, merge_status: str, merged_state: str | None
) -> None:
    request = _request(tmp_path, "reset")
    if mode == "stale":
        merged = request.artifact_root / "native" / "merged" / "coverage.dat"
        merged.parent.mkdir(parents=True)
        merged.write_text(_HEADER + _LINE_POINT, encoding="utf-8")
        os.utime(merged, ns=(1, 1))

    result = collect(request, _BadMergeExecution(mode, tmp_path / "outside" / "merged.dat"))

    assert result.status == "collector_error"
    assert len(result.points) == 1
    assert result.merge.status == merge_status
    assert [finding.code for finding in result.findings] == [expected_code]
    unsafe_detail = {
        "outside": "escapes the coverage artifact root",
        "directory": "is not a regular file",
    }
    if mode in unsafe_detail:
        assert unsafe_detail[mode] in result.findings[0].message
    merged = [artifact for artifact in result.artifacts if artifact.kind == "merged_native"]
    assert [artifact.state for artifact in merged] == (
        [] if merged_state is None else [merged_state]
    )


def test_build_failure_reports_elaboration_truth_for_every_selected_test(tmp_path: Path) -> None:
    request = _request(tmp_path, "a", "b", sources=())

    result = collect(request, _BuildFailureExecution())

    assert result.status == "collector_error"
    assert [(run.test, run.simulation_verdict, run.collection) for run in result.runs] == [
        ("a", "elab_error", "collector_error"),
        ("b", "elab_error", "collector_error"),
    ]
    assert result.artifacts == ()
    assert [finding.code for finding in result.findings] == ["COV_COVERAGE_BUILD_FAILED"]


def test_collector_rejects_any_verilator_other_than_the_exact_safe_pin(tmp_path: Path) -> None:
    request = _request(tmp_path, "reset", sources=())

    result = collect(request, _WrongVerilatorExecution())

    assert result.status == "collector_error"
    assert result.infrastructure_error is True
    assert [run.simulation_verdict for run in result.runs] == ["inconclusive"]
    assert [finding.code for finding in result.findings] == ["COV_VERILATOR_IDENTITY_MISMATCH"]


@pytest.mark.parametrize(
    ("record_type", "hits"),
    [("line", 0), ("future", 0), ("toggle", 2)],
    ids=["line", "future", "toggle"],
)
def test_native_source_presence_precedes_metric_filtering(
    tmp_path: Path, record_type: str, hits: int
) -> None:
    payload = _HEADER + _native_entry(record_type, "point", hits=hits).replace(
        "rtl/counter.sv", "/stage/rtl/counter.sv"
    )

    result = collect(_request(tmp_path, f"presence-{record_type}"), _PresenceExecution(payload))

    assert result.status == "complete", _collection_detail(result)
    assert result.native_sources == ("rtl/counter.sv",)
    if record_type == "future":
        assert not result.points
        assert result.unrecognized_records


@pytest.mark.parametrize("isolated", [True, False], ids=["isolated", "non-isolated"])
def test_native_entry_in_a_mirrored_header_is_named_by_project_path(
    tmp_path: Path, isolated: bool
) -> None:
    """AC3: a header the build read through its private mirror shows its Project path."""
    request = _request(tmp_path, "mirrored-header")
    if isolated:
        target = replace(
            request.target,
            foreign_paths={
                "src/acme_demo_counter-booleycoverage_1/rtl/inc/defs.svh": "rtl/inc/defs.svh"
            },
        )
        request = replace(request, target=target)
    export = "src/acme_demo_counter-booleycoverage_1/rtl/inc/defs.svh"
    payload = _HEADER + _native_entry("line", "block").replace("rtl/counter.sv", export)

    result = collect(request, _PresenceExecution(payload))

    assert result.status == "complete", _collection_detail(result)
    # Non-isolated Projects keep the native name, as before.
    assert result.native_sources == (("rtl/inc/defs.svh",) if isolated else (export,))
    (point,) = result.points
    assert point.identity.source == result.native_sources[0]


def test_native_entry_in_the_authored_source_matches_through_the_mirror_name(
    tmp_path: Path,
) -> None:
    """A native ``src/<core>/rtl/counter.sv`` suffix-matches the Project-relative input."""
    export = "src/acme_demo_counter-booleycoverage_1/rtl/counter.sv"
    payload = _HEADER + _native_entry("line", "block").replace("rtl/counter.sv", export)

    result = collect(_request(tmp_path, "mirrored-source"), _PresenceExecution(payload))

    assert result.status == "complete", _collection_detail(result)
    (point,) = result.points
    assert point.identity.source == "rtl/counter.sv"


def test_mirror_export_name_settles_inputs_that_share_a_name_suffix(tmp_path: Path) -> None:
    """``tb.sv`` beside ``rtl/tb.sv`` stay two sources, not a header-looking foreign file."""
    prefix = "src/acme_demo_counter-booleycoverage_1/"
    exports = {
        prefix + "tb.sv": "tb.sv",
        prefix + "rtl/tb.sv": "rtl/tb.sv",
        prefix + "rtl/tb.vh": "rtl/tb.vh",
    }
    sources = (
        CoverageSource("tb.sv", "tb.sv", "testbench"),
        CoverageSource("rtl/tb.sv", "rtl/tb.sv", "rtl"),
    )
    request = _request(tmp_path, "mirror", sources=sources)
    request = replace(request, target=replace(request.target, foreign_paths=exports))
    payload = _HEADER + "".join(
        _native_entry("line", "block").replace("rtl/counter.sv", prefix + name)
        for name in ("rtl/tb.sv", "rtl/tb.vh")
    )

    result = collect(request, _PresenceExecution(payload))

    assert result.status == "complete", _collection_detail(result)
    assert result.native_sources == ("rtl/tb.sv", "rtl/tb.vh")
    assert {point.identity.source: point.disposition["kind"] for point in result.points} == {
        "rtl/tb.sv": "eligible",
        "rtl/tb.vh": "unscored",
    }


def test_run_stems_and_ids_name_the_raw_files_collect_asks_for(tmp_path: Path) -> None:
    class Recording(_GeneratedMainExecution):
        def __init__(self) -> None:
            self.requests = []

        def run(self, request) -> SimulationRunResult:
            self.requests.append(request)
            return super().run(request)

    execution = Recording()
    stress = SelectedCoverageRun("stress", build_test_run("stress", variant="soak", seed=42))
    request = _request(
        tmp_path, SelectedCoverageRun("smoke test"), SelectedCoverageRun("smoke_test"), stress
    )

    collect(request, execution)

    stems = [item.raw_path.stem for item in execution.requests]
    assert stems[0].startswith("001-~sha256-")
    assert stems[1:] == ["002-smoke_test", "003-stress+soak@42"]
    assert execution.requests[2].run_id == "run:003:stress+soak@42"


@pytest.mark.parametrize("verdict", ["pass", "fail"])
@pytest.mark.parametrize("artifact_error", [False, True])
def test_authenticated_cleanup_warning_survives_collection(
    tmp_path, monkeypatch, verdict, artifact_error
) -> None:
    from booley.flows.sim.coverage_record import CoverageFinding

    class Execution(_GeneratedMainExecution):
        def run(self, request):
            super().run(request)
            warning = CoverageFinding(
                "warning",
                "COV_ARTIFACT_PERSISTENCE_CLEANUP_FAILED",
                "/tests/runs",
                "partial cleanup denied",
            )
            return SimulationRunResult(verdict, "primary diagnostic", diagnostics=(warning,))

    if artifact_error:

        def denied_artifact(*_args, **_kwargs):
            raise OSError("raw evidence read denied")

        # No portable input makes reading a raw database fail after it was written.
        monkeypatch.setattr("booley.flows.sim.verilator_coverage._artifact", denied_artifact)
    result = collect(_request(tmp_path, "smoke"), Execution())
    assert result.runs[0].simulation_verdict == verdict
    assert result.status == ("collector_error" if artifact_error else "complete"), (
        _collection_detail(result)
    )
    assert result.merge.status == ("not_run" if artifact_error else "equivalent")
    warning = next(
        item for item in result.findings if item.code == "COV_ARTIFACT_PERSISTENCE_CLEANUP_FAILED"
    )
    assert warning.severity == "warning"
    assert warning.message == "partial cleanup denied"


class _MeasuredMerge:
    """Replays the wrapped port's merge result through the Flow's measured run."""

    def __init__(self, inner: _GeneratedMainExecution, root: Path) -> None:
        self.inner = inner
        self.flow = measured_flow(root)

    def build(self, request) -> SimulationBuildResult:
        return self.inner.build(request)

    def run(self, request) -> SimulationRunResult:
        return self.inner.run(request)

    def command(self, request) -> SimulationCommandResult:
        result = self.inner.command(request)
        process = SubprocessResult(result.returncode, result.stdout, result.stderr)
        with job_resources.resource_phase("sim", "sim_postprocess"):
            self.flow._execute_boundary(canned_command(process), timeout=30)
        return result


@pytest.mark.parametrize(
    ("mode", "outcome"),
    [
        pytest.param("accepted", "exit_zero", id="accepted"),
        pytest.param("missing", "exit_nonzero", id="missing-merged-file"),
        pytest.param("mismatch", "exit_nonzero", id="mismatched-counts"),
    ],
)
def test_merge_record_outcome_follows_the_merged_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, outcome: str
) -> None:
    """The merge records a failure when Booley rejects the file the zero-exit merge wrote (#4964)."""
    monkeypatch.setenv("BOOLEY_DIR", str(tmp_path / ".booley"))
    inner = (
        _GeneratedMainExecution()
        if mode == "accepted"
        else _BadMergeExecution(mode, tmp_path / "outside" / "merged.dat")
    )

    collect(_request(tmp_path, "reset"), _MeasuredMerge(inner, tmp_path))

    records = resource_history.load_records(tmp_path / ".booley")
    assert [(r["outcome"], r["returncode"]) for r in records] == [(outcome, 0)]

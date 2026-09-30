"""Authenticated result transport shared by Simulation adapters."""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal

from booley.core.boundary import (
    BoundaryError,
    require_bool,
    require_dict,
    require_finite_number,
    require_int,
    require_list,
)
from booley.flows.sim.adapter_contract import PreparedSimulationWork
from booley.flows.sim.backends.shared import (
    RunTermination,
    RunTerminationKind,
    SimulationFailureKind,
    allowed_failure_kinds,
)
from booley.flows.sim.result import count_sva_errors, parse_sim_verdict

ADAPTER_RESULT_SCHEMA = 1
AdapterVerdict = Literal["pass", "fail", "timeout", "inconclusive"]
AdapterFailureKind = SimulationFailureKind
AdapterTraceStatus = Literal["ok", "incident"]


class AdapterTransportError(RuntimeError):
    """An adapter result is missing, malformed, or belongs to another attempt."""


@dataclass(frozen=True)
class AdapterTransportIdentity:
    """Identity that binds one adapter result to its parent invocation."""

    adapter: str
    attempt_token: str
    target_identity: str
    selected_tests: tuple[str, ...]
    result_path: Path


@dataclass(frozen=True)
class AdapterTestResult:
    """One adapter-normalized per-test verdict."""

    name: str
    verdict: AdapterVerdict
    elapsed_s: float = 0.0
    detail: str = ""
    termination: RunTerminationKind = "completed"
    failure_kind: AdapterFailureKind = ""


@dataclass(frozen=True)
class AdapterTraceResult:
    """Authenticated trace success or incident evidence from one adapter."""

    status: AdapterTraceStatus
    path: str = ""
    detail: str = ""
    top_scope: str = ""
    signal_count: int = 0
    total_ticks: int = 0


@dataclass(frozen=True)
class AdapterResult:
    """Common terminal evidence emitted by every Simulation adapter."""

    passed: bool
    inconclusive: bool
    sva_errors: int
    tests: tuple[str, ...]
    simulator_returncode: int = 0
    termination: RunTerminationKind = "completed"
    missing_input_path: str = ""
    failure_kind: AdapterFailureKind = ""
    detail: str = ""
    test_results: tuple[AdapterTestResult, ...] = ()
    diagnostics: tuple[str, ...] = ()
    trace: AdapterTraceResult | None = None


@dataclass(frozen=True)
class NativeVerdictAssessment:
    """Simulator-neutral verdict facts derived at a native adapter seam."""

    verdict: bool | None
    sva_errors: int
    passed: bool
    inconclusive: bool


def partial_result_identity(identity: AdapterTransportIdentity) -> AdapterTransportIdentity:
    """Address authenticated nonterminal evidence for an in-flight attempt."""
    path = identity.result_path.with_name(f"{identity.result_path.name}.partial")
    return replace(identity, result_path=path)


def _payload(identity: AdapterTransportIdentity, result: AdapterResult) -> dict[str, Any]:
    return {
        "schema": ADAPTER_RESULT_SCHEMA,
        "adapter": identity.adapter,
        "attempt_token": identity.attempt_token,
        "target_identity": identity.target_identity,
        "selected_tests": list(identity.selected_tests),
        "passed": result.passed,
        "inconclusive": result.inconclusive,
        "sva_errors": result.sva_errors,
        "tests": list(result.tests),
        "simulator_returncode": result.simulator_returncode,
        "termination": result.termination,
        "missing_input_path": result.missing_input_path,
        "failure_kind": result.failure_kind,
        "detail": result.detail,
        "test_results": [
            {
                "name": test.name,
                "verdict": test.verdict,
                "elapsed_s": test.elapsed_s,
                "detail": test.detail,
                "termination": test.termination,
                "failure_kind": test.failure_kind,
            }
            for test in result.test_results
        ],
        "diagnostics": list(result.diagnostics),
        "trace": (
            {
                "status": result.trace.status,
                "path": result.trace.path,
                "detail": result.trace.detail,
                "top_scope": result.trace.top_scope,
                "signal_count": result.trace.signal_count,
                "total_ticks": result.trace.total_ticks,
            }
            if result.trace is not None
            else None
        ),
    }


def write_adapter_result(identity: AdapterTransportIdentity, result: AdapterResult) -> None:
    """Atomically publish one adapter's terminal evidence."""
    path = identity.result_path
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(_payload(identity, result)), encoding="utf-8")
        temporary.replace(path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _string_tuple(payload: dict[str, Any], field: str) -> tuple[str, ...]:
    raw = payload.get(field)
    if not isinstance(raw, list) or any(not isinstance(value, str) for value in raw):
        raise AdapterTransportError(f"adapter result {field} must be a string list")
    return tuple(raw)


def _read_payload(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return require_dict(raw, field="adapter result")
    except (OSError, json.JSONDecodeError, BoundaryError) as exc:
        raise AdapterTransportError(f"could not read adapter result: {exc}") from exc


def _validated_scalars(payload: dict[str, Any]) -> tuple[bool, bool, int, int]:
    """Decode the required scalar verdict fields."""
    try:
        schema = require_int(payload.get("schema"), field="schema")
    except BoundaryError as exc:
        raise AdapterTransportError(str(exc)) from exc
    if schema != ADAPTER_RESULT_SCHEMA:
        raise AdapterTransportError(f"unsupported adapter result schema {schema}")
    try:
        passed = require_bool(payload, "passed")
        inconclusive = require_bool(payload, "inconclusive")
        sva_errors = require_int(payload.get("sva_errors"), field="sva_errors")
        simulator_returncode = require_int(
            payload.get("simulator_returncode"), field="simulator_returncode"
        )
    except BoundaryError as exc:
        raise AdapterTransportError(str(exc)) from exc
    if sva_errors < 0:
        raise AdapterTransportError("adapter result sva_errors must be non-negative")
    if passed and (inconclusive or sva_errors):
        raise AdapterTransportError("adapter result pass contradicts its evidence")
    return passed, inconclusive, sva_errors, simulator_returncode


def _validate_identity(
    payload: dict[str, Any],
    identity: AdapterTransportIdentity,
) -> tuple[str, ...]:
    """Require the result to belong to the expected invocation."""
    if payload.get("adapter") != identity.adapter:
        raise AdapterTransportError("adapter result adapter identity does not match")
    if payload.get("attempt_token") != identity.attempt_token:
        raise AdapterTransportError("adapter result attempt token does not match")
    if payload.get("target_identity") != identity.target_identity:
        raise AdapterTransportError("adapter result Target identity does not match")
    selected_tests = _string_tuple(payload, "selected_tests")
    if selected_tests != identity.selected_tests:
        raise AdapterTransportError("adapter result selected tests do not match")
    return selected_tests


def _validated_result_fields(
    payload: dict[str, Any],
    identity: AdapterTransportIdentity,
    passed: bool,
    inconclusive: bool,
) -> tuple[tuple[str, ...], RunTerminationKind, str, str, str]:
    """Decode fields whose constraints depend on the invocation or verdict."""
    tests = _string_tuple(payload, "tests")
    if identity.selected_tests and tests != identity.selected_tests:
        raise AdapterTransportError("adapter result tests do not match selected tests")
    if len(set(tests)) != len(tests):
        raise AdapterTransportError("adapter result test names must be unique")
    failure_kind = payload.get("failure_kind", "")
    detail = payload.get("detail", "")
    termination = payload.get("termination")
    missing_input_path = payload.get("missing_input_path", "")
    termination = _validated_termination(termination)
    failure_kind = _validated_failure_kind(failure_kind)
    if not isinstance(detail, str) or not isinstance(missing_input_path, str):
        raise AdapterTransportError("adapter result detail must be a string")
    if passed and failure_kind:
        raise AdapterTransportError("adapter result pass contradicts its failure kind")
    if failure_kind not in allowed_failure_kinds(termination):
        raise AdapterTransportError("adapter result failure kind contradicts its termination")
    if termination != "completed":
        if passed or inconclusive or not detail:
            raise AdapterTransportError("adapter result contradicts its termination")
        if termination != "fatal_init" and missing_input_path:
            raise AdapterTransportError("only fatal_init may name a missing input")
    elif missing_input_path:
        raise AdapterTransportError("completed adapter result cannot name a missing input")
    return tests, termination, missing_input_path, failure_kind, detail


def _validated_termination(value: Any) -> RunTerminationKind:
    if value not in {
        "completed",
        "timeout",
        "disk_budget",
        "fatal_init",
        "sim_time_stall",
        "trace_stall",
    }:
        raise AdapterTransportError("adapter result termination is invalid")
    return value


def _validated_failure_kind(value: Any) -> AdapterFailureKind:
    if not isinstance(value, str) or value not in {
        "",
        "design",
        "infrastructure",
        "timeout",
        "inconclusive",
        "artifact",
        "missing_input",
    }:
        raise AdapterTransportError("adapter result failure_kind is invalid")
    return value


def _validated_test_results(payload: dict[str, Any]) -> tuple[AdapterTestResult, ...]:
    """Decode bounded per-test outcomes from the transport document."""
    raw = payload.get("test_results", [])
    try:
        entries = require_list(raw, field="test_results")
    except BoundaryError as exc:
        raise AdapterTransportError(str(exc)) from exc
    results: list[AdapterTestResult] = []
    for index, entry in enumerate(entries):
        results.append(_validated_test_result(entry, index))
    names = [result.name for result in results]
    if len(set(names)) != len(names):
        raise AdapterTransportError("adapter test result names must be unique")
    return tuple(results)


def _validated_test_result(value: Any, index: int) -> AdapterTestResult:
    try:
        entry = require_dict(value, field=f"test_results[{index}]")
        elapsed_s = require_finite_number(entry.get("elapsed_s", 0.0), field="elapsed_s")
    except BoundaryError as exc:
        raise AdapterTransportError(str(exc)) from exc
    name = entry.get("name")
    verdict = entry.get("verdict")
    detail = entry.get("detail", "")
    termination = entry.get("termination", "completed")
    failure_kind = entry.get("failure_kind", "")
    if not isinstance(name, str) or not name:
        raise AdapterTransportError("adapter test result name must be nonempty")
    if verdict not in {"pass", "fail", "timeout", "inconclusive"}:
        raise AdapterTransportError("adapter test result verdict is invalid")
    if not isinstance(detail, str) or elapsed_s < 0:
        raise AdapterTransportError("adapter test result detail or elapsed time is invalid")
    termination = _validated_termination(termination)
    failure_kind = _validated_failure_kind(failure_kind)
    if failure_kind not in allowed_failure_kinds(termination):
        raise AdapterTransportError("adapter test result failure kind contradicts its termination")
    if termination != "completed":
        expected_verdict = "timeout" if termination == "timeout" else "fail"
        if verdict != expected_verdict or not detail:
            raise AdapterTransportError("adapter test result contradicts its termination")
    return AdapterTestResult(name, verdict, elapsed_s, detail, termination, failure_kind)


def read_adapter_result(identity: AdapterTransportIdentity) -> AdapterResult:
    """Validate and return terminal evidence for exactly one expected attempt."""
    payload = _read_payload(identity.result_path)
    passed, inconclusive, sva_errors, simulator_returncode = _validated_scalars(payload)
    _validate_identity(payload, identity)
    tests, termination, missing_input_path, failure_kind, detail = _validated_result_fields(
        payload, identity, passed, inconclusive
    )
    test_results = _validated_test_results(payload)
    diagnostics = _optional_string_tuple(payload, "diagnostics")
    trace = _validated_trace(payload)
    if test_results and tuple(test.name for test in test_results) != tests:
        raise AdapterTransportError("adapter per-test results do not match result tests")
    if tests and not test_results:
        raise AdapterTransportError("adapter result omits required per-test verdicts")
    if passed and any(test.verdict != "pass" for test in test_results):
        raise AdapterTransportError("adapter pass contradicts a per-test verdict")
    if not inconclusive and any(test.verdict == "inconclusive" for test in test_results):
        raise AdapterTransportError("adapter result contradicts per-test inconclusive evidence")
    if (
        failure_kind == "artifact"
        and inconclusive
        and not passed
        and any(test.verdict == "pass" for test in test_results)
    ):
        raise AdapterTransportError("adapter trace failure contradicts a passing test")
    if termination == "completed" and any(
        test.termination != "completed" for test in test_results
    ):
        raise AdapterTransportError("completed adapter result contains an aborted test")
    if termination != "completed":
        _validate_aborted_test_results(test_results, termination, failure_kind)
    return AdapterResult(
        passed=passed,
        inconclusive=inconclusive,
        sva_errors=sva_errors,
        tests=tests,
        simulator_returncode=simulator_returncode,
        termination=termination,
        missing_input_path=missing_input_path,
        failure_kind=failure_kind,
        detail=detail,
        test_results=test_results,
        diagnostics=diagnostics,
        trace=trace,
    )


def _validate_aborted_test_results(
    results: tuple[AdapterTestResult, ...],
    termination: RunTerminationKind,
    failure_kind: AdapterFailureKind,
) -> None:
    for result in results:
        if result.termination == "completed":
            continue
        if result.termination != termination or result.failure_kind != failure_kind:
            raise AdapterTransportError(
                "adapter aggregate termination contradicts a per-test termination"
            )


def _validated_trace(payload: dict[str, Any]) -> AdapterTraceResult | None:
    raw = payload.get("trace")
    if raw is None:
        return None
    try:
        trace = require_dict(raw, field="trace")
        signal_count = require_int(trace.get("signal_count", 0), field="trace.signal_count")
        total_ticks = require_int(trace.get("total_ticks", 0), field="trace.total_ticks")
    except BoundaryError as exc:
        raise AdapterTransportError(str(exc)) from exc
    status = trace.get("status")
    path = trace.get("path", "")
    detail = trace.get("detail", "")
    top_scope = trace.get("top_scope", "")
    if status not in {"ok", "incident"}:
        raise AdapterTransportError("adapter trace status is invalid")
    if any(not isinstance(value, str) for value in (path, detail, top_scope)):
        raise AdapterTransportError("adapter trace text fields must be strings")
    if signal_count < 0 or total_ticks < 0:
        raise AdapterTransportError("adapter trace metadata must be non-negative")
    if status == "ok" and not path:
        raise AdapterTransportError("adapter trace success requires a path")
    if status == "incident" and not detail:
        raise AdapterTransportError("adapter trace incident requires detail")
    return AdapterTraceResult(status, path, detail, top_scope, signal_count, total_ticks)


def _optional_string_tuple(payload: dict[str, Any], field: str) -> tuple[str, ...]:
    raw = payload.get(field, [])
    if not isinstance(raw, list) or any(not isinstance(value, str) for value in raw):
        raise AdapterTransportError(f"adapter result {field} must be a string list")
    return tuple(raw)


def transport_arguments(identity: AdapterTransportIdentity | None) -> list[str]:
    """Render the shared authenticated-result CLI arguments."""
    if identity is None:
        return []
    args = [
        "--adapter-result",
        str(identity.result_path),
        "--attempt-token",
        identity.attempt_token,
        "--target-identity",
        identity.target_identity,
    ]
    return [*args, *(f"--selected-test={name}" for name in identity.selected_tests)]


def work_transport_arguments(work: PreparedSimulationWork) -> list[str]:
    """Render transport arguments carried by prepared adapter work."""
    if not work.adapter_result_path:
        return []
    return transport_arguments(
        AdapterTransportIdentity(
            adapter=work.adapter,
            attempt_token=work.attempt_token,
            target_identity=work.target_identity,
            selected_tests=work.tests,
            result_path=Path(work.adapter_result_path),
        )
    )


def publish_native_adapter_result(
    identity: AdapterTransportIdentity | None,
    output: str,
    returncode: int,
    *,
    failure_kind: AdapterFailureKind = "",
    pass_sentinels: list[str] | None = None,
    fail_sentinels: list[str] | None = None,
    trace_required: bool = False,
    trace: AdapterTraceResult | None = None,
    detail: str = "",
    termination: RunTermination | None = None,
) -> None:
    """Normalize and publish terminal evidence for a native adapter."""
    if identity is None:
        return
    write_adapter_result(
        identity,
        _native_adapter_result(
            identity,
            output,
            returncode,
            failure_kind=failure_kind,
            pass_sentinels=pass_sentinels,
            fail_sentinels=fail_sentinels,
            trace_required=trace_required,
            trace=trace,
            detail=detail,
            termination=termination or RunTermination(),
        ),
    )


def assess_native_verdict(
    output: str,
    returncode: int,
    *,
    pass_sentinels: list[str] | None = None,
    fail_sentinels: list[str] | None = None,
    termination: RunTermination | None = None,
) -> NativeVerdictAssessment:
    """Apply shared native sentinel/SVA/termination precedence once."""
    verdict = parse_sim_verdict(
        output,
        pass_sentinels=pass_sentinels,
        fail_sentinels=fail_sentinels,
    )
    sva_errors = count_sva_errors(output)
    termination = termination or RunTermination()
    passed = not termination.aborted and verdict is True and returncode == 0 and sva_errors == 0
    inconclusive = (
        not termination.aborted and verdict is None and returncode == 0 and sva_errors == 0
    )
    return NativeVerdictAssessment(verdict, sva_errors, passed, inconclusive)


def native_verdict_message(
    tool: str,
    assessment: NativeVerdictAssessment,
    returncode: int,
    termination: RunTermination,
) -> str:
    """Render the shared native verdict precedence with a backend label."""
    if termination.aborted:
        outcome = f"ABORTED ({termination.detail}; rc={returncode})"
    elif assessment.inconclusive:
        outcome = "INCONCLUSIVE (rc=0, no sentinel)"
    elif assessment.passed:
        outcome = f"PASSED (rc={returncode})"
    elif assessment.verdict is False:
        reason = f"rc={returncode}" if returncode else "fail sentinel matched"
        outcome = f"FAILED ({reason})"
    elif assessment.sva_errors:
        outcome = f"FAILED ({assessment.sva_errors} SVA assertion errors)"
    else:
        outcome = f"FAILED (rc={returncode})"
    return f"\n{tool} sim {outcome}"


def first_native_failure(output: str, default: str = "") -> str:
    """Return the first generic native failure diagnostic, if any."""
    for line in output.splitlines():
        if any(word in line.lower() for word in ("failed", "fatal", "error", "mismatch")):
            return line.strip()
    return default


def _native_adapter_result(
    identity: AdapterTransportIdentity,
    output: str,
    returncode: int,
    *,
    failure_kind: AdapterFailureKind,
    pass_sentinels: list[str] | None,
    fail_sentinels: list[str] | None,
    trace_required: bool,
    trace: AdapterTraceResult | None,
    detail: str,
    termination: RunTermination,
) -> AdapterResult:
    assessment = assess_native_verdict(
        output,
        returncode,
        pass_sentinels=pass_sentinels,
        fail_sentinels=fail_sentinels,
        termination=termination,
    )
    trace_missing, detail, inconclusive, kind = _native_result_fields(
        trace_required, trace, detail, termination, assessment, failure_kind
    )
    effective_detail = termination.detail or detail
    test_results = _native_test_results(
        identity.selected_tests,
        verdict=assessment.verdict,
        returncode=returncode,
        sva_errors=assessment.sva_errors,
        inconclusive=assessment.inconclusive,
        trace_missing=trace_missing,
        detail=effective_detail,
        termination=termination,
    )
    return AdapterResult(
        passed=assessment.passed and not trace_missing,
        inconclusive=inconclusive,
        sva_errors=assessment.sva_errors,
        tests=identity.selected_tests,
        simulator_returncode=returncode,
        termination=termination.kind,
        missing_input_path=termination.missing_input_path,
        failure_kind=kind,
        detail=effective_detail,
        test_results=test_results,
        trace=trace,
    )


def _native_result_fields(
    trace_required: bool,
    trace: AdapterTraceResult | None,
    detail: str,
    termination: RunTermination,
    assessment: NativeVerdictAssessment,
    failure_kind: AdapterFailureKind,
) -> tuple[bool, str, bool, AdapterFailureKind]:
    trace_missing = trace_required and (trace is None or trace.status != "ok")
    if trace_missing and not detail and trace is not None:
        detail = trace.detail
    inconclusive = termination.kind == "completed" and (assessment.inconclusive or trace_missing)
    kind = (
        termination.failure_kind
        or failure_kind
        or _native_failure_kind(termination.kind == "timeout", trace_missing, inconclusive)
    )
    return trace_missing, detail, inconclusive, kind


def _native_test_results(
    names: tuple[str, ...],
    *,
    verdict: bool | None,
    returncode: int,
    sva_errors: int,
    inconclusive: bool,
    trace_missing: bool,
    detail: str,
    termination: RunTermination,
) -> tuple[AdapterTestResult, ...]:
    """Map native evidence to per-test verdicts.

    ``inconclusive`` is the simulator's own verdict (no sentinel). A missing
    requested waveform only downgrades a would-be pass: timeouts, aborts and
    functional failures keep precedence so a trace problem never masks them.
    """
    passing = verdict is True and returncode == 0 and sva_errors == 0
    normalized = (
        "timeout"
        if termination.kind == "timeout"
        else "fail"
        if termination.aborted
        else ("inconclusive" if trace_missing else "pass")
        if passing
        else "inconclusive"
        if inconclusive
        else "fail"
    )
    return tuple(
        AdapterTestResult(
            name,
            normalized,
            detail=detail,
            termination=termination.kind,
            failure_kind=termination.failure_kind,
        )
        for name in names
    )


def _native_failure_kind(
    timed_out: bool, trace_missing: bool, inconclusive: bool
) -> AdapterFailureKind:
    if timed_out:
        return "timeout"
    if trace_missing:
        return "artifact"
    return "inconclusive" if inconclusive else ""


def add_transport_arguments(parser: argparse.ArgumentParser) -> None:
    """Add the optional authenticated-result channel to an adapter CLI."""
    parser.add_argument("--adapter-result", default="")
    parser.add_argument("--attempt-token", default="")
    parser.add_argument("--target-identity", default="")
    parser.add_argument("--selected-test", action="append", default=[])


def transport_identity_from_args(
    args: argparse.Namespace,
    adapter: str,
) -> AdapterTransportIdentity | None:
    """Build an identity when transport was requested; reject partial identity."""
    values = (args.adapter_result, args.attempt_token, args.target_identity)
    if not any(values):
        return None
    if not all(values):
        raise AdapterTransportError(
            "adapter result transport requires path, attempt token, and Target identity"
        )
    return AdapterTransportIdentity(
        adapter=adapter,
        attempt_token=args.attempt_token,
        target_identity=args.target_identity,
        selected_tests=tuple(args.selected_test),
        result_path=Path(args.adapter_result),
    )


__all__ = [
    "ADAPTER_RESULT_SCHEMA",
    "AdapterFailureKind",
    "AdapterResult",
    "AdapterTestResult",
    "AdapterTraceResult",
    "AdapterTraceStatus",
    "AdapterTransportError",
    "AdapterTransportIdentity",
    "AdapterVerdict",
    "NativeVerdictAssessment",
    "add_transport_arguments",
    "assess_native_verdict",
    "first_native_failure",
    "native_verdict_message",
    "partial_result_identity",
    "publish_native_adapter_result",
    "read_adapter_result",
    "transport_arguments",
    "transport_identity_from_args",
    "work_transport_arguments",
    "write_adapter_result",
]

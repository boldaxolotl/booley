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
from booley.flows.sim.result import count_sva_errors, parse_sim_verdict

ADAPTER_RESULT_SCHEMA = 1
AdapterVerdict = Literal["pass", "fail", "timeout", "inconclusive"]
AdapterFailureKind = Literal["", "design", "infrastructure", "timeout", "inconclusive", "artifact"]


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


@dataclass(frozen=True)
class AdapterResult:
    """Common terminal evidence emitted by every Simulation adapter."""

    passed: bool
    inconclusive: bool
    sva_errors: int
    tests: tuple[str, ...]
    failure_kind: AdapterFailureKind = ""
    detail: str = ""
    test_results: tuple[AdapterTestResult, ...] = ()
    diagnostics: tuple[str, ...] = ()


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
        "failure_kind": result.failure_kind,
        "detail": result.detail,
        "test_results": [
            {
                "name": test.name,
                "verdict": test.verdict,
                "elapsed_s": test.elapsed_s,
                "detail": test.detail,
            }
            for test in result.test_results
        ],
        "diagnostics": list(result.diagnostics),
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


def _validated_scalars(payload: dict[str, Any]) -> tuple[bool, bool, int]:
    """Decode the required scalar verdict fields."""
    try:
        schema = require_int(payload.get("schema"), field="schema")
        passed = require_bool(payload, "passed")
        inconclusive = require_bool(payload, "inconclusive")
        sva_errors = require_int(payload.get("sva_errors"), field="sva_errors")
    except BoundaryError as exc:
        raise AdapterTransportError(str(exc)) from exc
    if schema != ADAPTER_RESULT_SCHEMA:
        raise AdapterTransportError(f"unsupported adapter result schema {schema}")
    if sva_errors < 0:
        raise AdapterTransportError("adapter result sva_errors must be non-negative")
    if passed and (inconclusive or sva_errors):
        raise AdapterTransportError("adapter result pass contradicts its evidence")
    return passed, inconclusive, sva_errors


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
) -> tuple[tuple[str, ...], str, str]:
    """Decode fields whose constraints depend on the invocation or verdict."""
    tests = _string_tuple(payload, "tests")
    if identity.selected_tests and tests != identity.selected_tests:
        raise AdapterTransportError("adapter result tests do not match selected tests")
    if len(set(tests)) != len(tests):
        raise AdapterTransportError("adapter result test names must be unique")
    failure_kind = payload.get("failure_kind", "")
    detail = payload.get("detail", "")
    if not isinstance(failure_kind, str) or failure_kind not in {
        "",
        "design",
        "infrastructure",
        "timeout",
        "inconclusive",
        "artifact",
    }:
        raise AdapterTransportError("adapter result failure_kind is invalid")
    if not isinstance(detail, str):
        raise AdapterTransportError("adapter result detail must be a string")
    if passed and failure_kind:
        raise AdapterTransportError("adapter result pass contradicts its failure kind")
    return tests, failure_kind, detail


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
    if not isinstance(name, str) or not name:
        raise AdapterTransportError("adapter test result name must be nonempty")
    if verdict not in {"pass", "fail", "timeout", "inconclusive"}:
        raise AdapterTransportError("adapter test result verdict is invalid")
    if not isinstance(detail, str) or elapsed_s < 0:
        raise AdapterTransportError("adapter test result detail or elapsed time is invalid")
    return AdapterTestResult(name, verdict, elapsed_s, detail)


def read_adapter_result(identity: AdapterTransportIdentity) -> AdapterResult:
    """Validate and return terminal evidence for exactly one expected attempt."""
    payload = _read_payload(identity.result_path)
    passed, inconclusive, sva_errors = _validated_scalars(payload)
    _validate_identity(payload, identity)
    tests, failure_kind, detail = _validated_result_fields(payload, identity, passed)
    test_results = _validated_test_results(payload)
    diagnostics = _optional_string_tuple(payload, "diagnostics")
    if test_results and tuple(test.name for test in test_results) != tests:
        raise AdapterTransportError("adapter per-test results do not match result tests")
    if tests and not test_results:
        raise AdapterTransportError("adapter result omits required per-test verdicts")
    if passed and any(test.verdict != "pass" for test in test_results):
        raise AdapterTransportError("adapter pass contradicts a per-test verdict")
    if not inconclusive and any(test.verdict == "inconclusive" for test in test_results):
        raise AdapterTransportError("adapter result contradicts per-test inconclusive evidence")
    return AdapterResult(
        passed=passed,
        inconclusive=inconclusive,
        sva_errors=sva_errors,
        tests=tests,
        failure_kind=failure_kind,
        detail=detail,
        test_results=test_results,
        diagnostics=diagnostics,
    )


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
    failure_kind: str = "",
    pass_sentinels: list[str] | None = None,
    fail_sentinels: list[str] | None = None,
    trace_required: bool = False,
    detail: str = "",
) -> None:
    """Normalize and publish terminal evidence for a native adapter."""
    if identity is None:
        return
    verdict = parse_sim_verdict(
        output,
        pass_sentinels=pass_sentinels,
        fail_sentinels=fail_sentinels,
    )
    sva_errors = count_sva_errors(output)
    timed_out = "simulation timed out" in output.lower()
    trace_missing = trace_required and "TRACE_OK:" not in output
    inconclusive = (verdict is None and returncode == 0 and sva_errors == 0) or trace_missing
    kind = failure_kind or _native_failure_kind(timed_out, trace_missing, inconclusive)
    test_results = _native_test_results(
        identity.selected_tests,
        verdict=verdict,
        returncode=returncode,
        sva_errors=sva_errors,
        timed_out=timed_out,
        inconclusive=inconclusive,
        detail=detail,
    )
    write_adapter_result(
        identity,
        AdapterResult(
            passed=verdict is True and returncode == 0 and sva_errors == 0 and not trace_missing,
            inconclusive=inconclusive,
            sva_errors=sva_errors,
            tests=identity.selected_tests,
            failure_kind=kind,
            detail=detail,
            test_results=test_results,
        ),
    )


def _native_test_results(
    names: tuple[str, ...],
    *,
    verdict: bool | None,
    returncode: int,
    sva_errors: int,
    timed_out: bool,
    inconclusive: bool,
    detail: str,
) -> tuple[AdapterTestResult, ...]:
    normalized = (
        "timeout"
        if timed_out
        else "pass"
        if verdict is True and returncode == 0 and sva_errors == 0
        else "inconclusive"
        if inconclusive
        else "fail"
    )
    return tuple(AdapterTestResult(name, normalized, detail=detail) for name in names)


def _native_failure_kind(timed_out: bool, trace_missing: bool, inconclusive: bool) -> str:
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
    "AdapterTransportError",
    "AdapterTransportIdentity",
    "AdapterVerdict",
    "add_transport_arguments",
    "partial_result_identity",
    "publish_native_adapter_result",
    "read_adapter_result",
    "transport_arguments",
    "transport_identity_from_args",
    "work_transport_arguments",
    "write_adapter_result",
]

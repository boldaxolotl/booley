"""Authenticated result transport shared by Simulation adapters."""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from booley.core.boundary import BoundaryError, require_bool, require_dict, require_int

ADAPTER_RESULT_SCHEMA = 1


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
class AdapterResult:
    """Common terminal evidence emitted by every Simulation adapter."""

    passed: bool
    inconclusive: bool
    sva_errors: int
    tests: tuple[str, ...]
    failure_kind: str = ""
    detail: str = ""


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


def read_adapter_result(  # noqa: PLR0912 — fail-closed validation is intentionally linear
    identity: AdapterTransportIdentity,
) -> AdapterResult:
    """Validate and return terminal evidence for exactly one expected attempt."""
    payload = _read_payload(identity.result_path)
    try:
        schema = require_int(payload.get("schema"), field="schema")
        passed = require_bool(payload, "passed")
        inconclusive = require_bool(payload, "inconclusive")
        sva_errors = require_int(payload.get("sva_errors"), field="sva_errors")
    except BoundaryError as exc:
        raise AdapterTransportError(str(exc)) from exc
    if schema != ADAPTER_RESULT_SCHEMA:
        raise AdapterTransportError(f"unsupported adapter result schema {schema}")
    if payload.get("adapter") != identity.adapter:
        raise AdapterTransportError("adapter result adapter identity does not match")
    if payload.get("attempt_token") != identity.attempt_token:
        raise AdapterTransportError("adapter result attempt token does not match")
    if payload.get("target_identity") != identity.target_identity:
        raise AdapterTransportError("adapter result Target identity does not match")
    selected_tests = _string_tuple(payload, "selected_tests")
    if selected_tests != identity.selected_tests:
        raise AdapterTransportError("adapter result selected tests do not match")
    tests = _string_tuple(payload, "tests")
    if identity.selected_tests and tests != identity.selected_tests:
        raise AdapterTransportError("adapter result tests do not match selected tests")
    if len(set(tests)) != len(tests):
        raise AdapterTransportError("adapter result test names must be unique")
    if sva_errors < 0:
        raise AdapterTransportError("adapter result sva_errors must be non-negative")
    if passed and (inconclusive or sva_errors):
        raise AdapterTransportError("adapter result pass contradicts its evidence")
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
    return AdapterResult(
        passed=passed,
        inconclusive=inconclusive,
        sva_errors=sva_errors,
        tests=tests,
        failure_kind=failure_kind,
        detail=detail,
    )


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
    "AdapterResult",
    "AdapterTransportError",
    "AdapterTransportIdentity",
    "add_transport_arguments",
    "read_adapter_result",
    "transport_identity_from_args",
    "write_adapter_result",
]

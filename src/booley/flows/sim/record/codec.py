"""Canonical JSON codecs for immutable Simulation Record values.

This module is deliberately infrastructure-free. Records Booley wrote are trusted
on read: decoding parses the document, enforces its size ceiling, refuses a format
an older Booley wrote, and checks the required keys and ``$schema``. No field is
checked one by one (coding principle 17).
"""

from __future__ import annotations

import functools
import json
import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import NoReturn, ParamSpec, TypeAlias, TypeVar, cast

from booley.core.boundary import BoundaryError, require_dict, require_str_value
from booley.flows.sim.record_files import canonical_json
from booley.flows.simulation_record_results import OlderBooleyRecordError

from .model import (
    BundleBuildAttempt,
    BundleBuildResult,
    SimulationAttempt,
    SimulationRecordDocument,
    SimulationRecordManifest,
    SimulationResult,
    SimulatorBundle,
)

JsonScalar: TypeAlias = str | int | float | bool | None
FrozenJson: TypeAlias = JsonScalar | tuple["FrozenJson", ...] | Mapping[str, "FrozenJson"]

MANIFEST_MAX_BYTES = 16 * 1024 * 1024
SUMMARY_MAX_BYTES = 16 * 1024 * 1024
RECORD_MAX_BYTES = 1024 * 1024
MAX_WORK_ITEMS = 1_000
MAX_OBSERVATIONS = 4_000
MAX_PATH_BYTES = 1_024
MAX_COMMAND_BYTES = 16 * 1024
MAX_DETAIL_BYTES = 2 * 1024

_WINDOWS_DRIVE_RE = re.compile(r"[A-Za-z]:")


class SimulationRecordIntegrityError(ValueError):
    """An authoritative record value violates its exact contract."""


class UnsupportedSimulationRecordFormatError(OlderBooleyRecordError):
    """A record an older Booley wrote.

    Deliberately not an integrity error: the storage is intact, only too old, so no
    integrity catch may report it as corrupt. Callers that know the invocation
    directory bind it with :meth:`located` so the message can name it.
    """

    def __init__(self, label: str, schema: str, invocation: Path | None = None) -> None:
        where = (
            f"the Simulation invocation directory {invocation}"
            if invocation is not None
            else "its Simulation invocation directory"
        )
        super().__init__(
            f"{label} {schema} was written by an older Booley and is no longer supported; "
            f"delete {where} and start a fresh run"
        )
        self.label = label
        self.schema = schema

    def located(self, invocation: Path) -> UnsupportedSimulationRecordFormatError:
        """The same refusal naming the invocation directory to delete."""
        return UnsupportedSimulationRecordFormatError(self.label, self.schema, invocation)


def raise_if_retired_schema(schema: object) -> None:
    """Refuse a schema identifier an older Booley wrote, before any other check."""
    if isinstance(schema, str) and schema in _RETIRED_SCHEMAS:
        raise UnsupportedSimulationRecordFormatError(_RETIRED_SCHEMAS[schema], schema)


def raise_if_retired(document: Mapping[str, object]) -> None:
    """Refuse a document in a format an older Booley wrote, before any other check."""
    raise_if_retired_schema(document.get("$schema"))


def refuse_retired_document(raw: bytes) -> NoReturn:
    """Refuse *raw*, a document only an older Booley wrote.

    Raises the retired-format refusal when its ``$schema`` is a retired one, and
    :class:`SimulationRecordIntegrityError` for any other content: the caller
    found the file where only an older Booley put one.
    """
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise SimulationRecordIntegrityError(f"document is not valid UTF-8 JSON: {exc}") from exc
    if isinstance(value, dict):
        raise_if_retired(cast(Mapping[str, object], value))
    raise SimulationRecordIntegrityError("document is not a retired Simulation Record format")


def canonical_json_bytes(value: object) -> bytes:
    """Encode finite JSON as sorted UTF-8 with one trailing newline."""
    try:
        return canonical_json(value)
    except (TypeError, ValueError) as exc:
        raise SimulationRecordIntegrityError(f"value is not canonical JSON: {exc}") from exc


# v1 of the Simulation Record manifest (it replaces simulation-campaign-manifest/v4):
# the exact work-item shape with the required ``test_runs`` of every work item: the
# concrete Test Runs (Test Variant, seed, declared arguments and environment) the item
# executes (ADR 0072), an optional ``no_waivers`` and explicit declared source
# identities in disclosures. It is the only version written and read.
MANIFEST_SCHEMA = "booley.simulation-record-manifest/v1"

# v5 = the Simulation Record identity key, on top of v3's required ``test_run`` of every observation: the resolved name of the
# Test Run it observed (ADR 0072), and ``bundle_verification``, the in-place check of the
# launched bundle. Observations are unique by Test Run.
RESULT_SCHEMA = "booley.simulation-result/v5"

# Formats an older Booley wrote. They are recognised only to be refused.
_RETIRED_SCHEMAS = {
    "booley.simulation-result/v4": "Simulation Result",
    "booley.simulator-bundle/v2": "Simulator bundle",
    "booley.executable-snapshot/v2": "Executable snapshot",
    "booley.simulation-campaign-manifest/v4": "Simulation Campaign manifest",
    "booley.simulation-result/v3": "Simulation Result",
    "booley.simulation-campaign-dependency/v1": "Simulation Campaign dependency",
    "booley.simulation-attempt/v1": "Simulation Attempt",
    "booley.bundle-build-attempt/v1": "Bundle build attempt",
    "booley.bundle-build-result/v1": "Bundle build result",
    "booley.simulator-bundle/v1": "Simulator bundle",
    "booley.executable-snapshot/v1": "Executable snapshot",
    "booley.simulation-campaign-manifest/v1": "Simulation Campaign manifest",
    "booley.simulation-campaign-manifest/v2": "Simulation Campaign manifest",
    "booley.simulation-result/v2": "Simulation Result",
    "booley.simulation-projection/v2": "Simulation projection",
    "booley.simulation-projection/v3": "Simulation projection",
    "booley.simulation-campaign-summary/v1": "Simulation Campaign summary",
    "booley.cocotb-campaign-transport/v1": "cocotb Campaign transport",
    "booley.coverage-campaign/v1": "Coverage Campaign",
    "booley.coverage-campaign/v2": "Coverage Campaign",
    "booley.coverage-campaign/v3": "Coverage Campaign",
    "booley.coverage-campaign/v4": "Coverage Campaign",
    "booley.coverage-campaign/v5": "Coverage Campaign",
    "booley.coverage-campaign-reference/v1": "Coverage Campaign reference",
}

_MANIFEST_FIELDS = frozenset(
    {
        "$schema",
        "simulation_record_id",
        "created_at",
        "origin",
        "target",
        "workload",
        "required_suite",
        "build_variants",
        "planning_disclosures",
        "prerequisites",
        "work_items",
        "fingerprints",
    }
)
_ATTEMPT_FIELDS = frozenset(
    {
        "$schema",
        "simulation_record_id",
        "manifest_sha256",
        "workload_sha256",
        "work_item_id",
        "attempt_id",
        "attempt_ordinal",
        "producer_invocation_id",
        "build_variant_id",
        "run_directory",
        "child_execution_id",
        "child_entry_sha256",
        "pre_sim_build_access",
        "policy",
        "started_at",
    }
)
_BUILD_ATTEMPT_FIELDS = frozenset(
    {
        "$schema",
        "simulation_record_id",
        "manifest_sha256",
        "workload_sha256",
        "build_variant_id",
        "build_attempt_id",
        "build_attempt_ordinal",
        "producer_invocation_id",
        "sharing",
        "owner",
        "tool_provenance",
        "started_at",
    }
)
_BUILD_RESULT_FIELDS = frozenset(
    {
        "$schema",
        "simulation_record_id",
        "manifest_sha256",
        "workload_sha256",
        "build_variant_id",
        "build_attempt",
        "state",
        "phase",
        "finished_at",
        "elapsed_seconds",
        "bundle",
        "observation",
        "evidence",
    }
)
_RESULT_FIELDS = frozenset(
    {
        "$schema",
        "simulation_record_id",
        "manifest_sha256",
        "workload_sha256",
        "work_item_id",
        "attempt_id",
        "attempt_ordinal",
        "producer_invocation_id",
        "state",
        "build_result",
        "bundle_id",
        "finished_at",
        "elapsed_seconds",
        "bundle_verification",
        "runtime_inputs",
        "observations",
        "grade",
        "diagnostics",
        "evidence",
    }
)
_BUNDLE_FIELDS = frozenset(
    {
        "$schema",
        "simulation_record_id",
        "manifest_sha256",
        "workload_sha256",
        "build_variant_id",
        "build_attempt_id",
        "bundle_id",
        "sharing",
        "owner",
        "tool_provenance",
        "created_at",
        "artifacts",
        "inventory_sha256",
    }
)

T = TypeVar("T", bound=SimulationRecordDocument)
_P = ParamSpec("_P")
_R = TypeVar("_R")


def reads_simulation_record_documents(function: Callable[_P, _R]) -> Callable[_P, _R]:
    """Report bad nested data in a published record as record corruption.

    Decoders check only the document boundary (#1743, #1744, #5747), so the code that reads a nested member of a record from disk wraps itself
    here: a missing or mistyped member becomes the integrity error instead of a
    bare ``KeyError``/``TypeError``/``ValueError``/``AttributeError``. OS errors, including lock
    timeouts, pass through.
    """

    @functools.wraps(function)
    def guarded(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        try:
            return function(*args, **kwargs)
        except (SimulationRecordIntegrityError, UnsupportedSimulationRecordFormatError):
            raise
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            raise SimulationRecordIntegrityError(
                f"published record is malformed: {exc!r}"
            ) from exc

    return guarded


def _require_within_ceiling(raw: bytes, limit: int) -> None:
    """Refuse *raw* larger than its record's size ceiling."""
    if len(raw) > limit:
        raise SimulationRecordIntegrityError(f"document exceeds {limit}-byte size ceiling")


def _decode(
    raw: bytes, *, schema: str, fields: frozenset[str], limit: int, value_type: type[T]
) -> T:
    _require_within_ceiling(raw, limit)
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise SimulationRecordIntegrityError(f"document is not valid UTF-8 JSON: {exc}") from exc
    try:
        document = require_dict(value, field="document")
    except BoundaryError as exc:
        raise SimulationRecordIntegrityError(str(exc)) from exc
    raise_if_retired(document)
    missing = sorted(fields - set(document))
    if missing:
        raise SimulationRecordIntegrityError(f"document lacks required fields {missing}")
    if document["$schema"] != schema:
        raise SimulationRecordIntegrityError(f"unsupported $schema {document['$schema']!r}")
    return value_type(cast(Mapping[str, FrozenJson], document))


_DOCUMENT_SPECS: dict[type[SimulationRecordDocument], tuple[str, frozenset[str], int]] = {
    SimulationRecordManifest: (MANIFEST_SCHEMA, _MANIFEST_FIELDS, MANIFEST_MAX_BYTES),
    SimulationAttempt: ("booley.simulation-attempt/v2", _ATTEMPT_FIELDS, RECORD_MAX_BYTES),
    BundleBuildAttempt: (
        "booley.bundle-build-attempt/v2",
        _BUILD_ATTEMPT_FIELDS,
        RECORD_MAX_BYTES,
    ),
    BundleBuildResult: ("booley.bundle-build-result/v2", _BUILD_RESULT_FIELDS, RECORD_MAX_BYTES),
    SimulationResult: (RESULT_SCHEMA, _RESULT_FIELDS, RECORD_MAX_BYTES),
    SimulatorBundle: ("booley.simulator-bundle/v3", _BUNDLE_FIELDS, MANIFEST_MAX_BYTES),
}


def _decode_registered(raw: bytes, value_type: type[T]) -> T:
    schema, fields, limit = _DOCUMENT_SPECS[value_type]
    return _decode(raw, schema=schema, fields=fields, limit=limit, value_type=value_type)


def decode_simulation_record_manifest(raw: bytes) -> SimulationRecordManifest:
    return _decode_registered(raw, SimulationRecordManifest)


def decode_simulation_attempt(raw: bytes) -> SimulationAttempt:
    return _decode_registered(raw, SimulationAttempt)


def decode_bundle_build_attempt(raw: bytes) -> BundleBuildAttempt:
    return _decode_registered(raw, BundleBuildAttempt)


def decode_bundle_build_result(raw: bytes) -> BundleBuildResult:
    return _decode_registered(raw, BundleBuildResult)


def decode_simulation_result(raw: bytes) -> SimulationResult:
    return _decode_registered(raw, SimulationResult)


def decode_simulator_bundle(raw: bytes) -> SimulatorBundle:
    return _decode_registered(raw, SimulatorBundle)


def _encode_checked(value: SimulationRecordDocument, expected_type: type[T]) -> bytes:
    if type(value) is not expected_type:
        raise SimulationRecordIntegrityError(f"expected {expected_type.__name__}")
    raw = canonical_json_bytes(value.document)
    # Builders never decode what they build, so publication enforces each record's ceiling.
    _require_within_ceiling(raw, _DOCUMENT_SPECS[expected_type][2])
    return raw


def encode_simulation_record_manifest(value: SimulationRecordManifest) -> bytes:
    return _encode_checked(value, SimulationRecordManifest)


def encode_simulation_attempt(value: SimulationAttempt) -> bytes:
    return _encode_checked(value, SimulationAttempt)


def encode_bundle_build_attempt(value: BundleBuildAttempt) -> bytes:
    return _encode_checked(value, BundleBuildAttempt)


def encode_bundle_build_result(value: BundleBuildResult) -> bytes:
    return _encode_checked(value, BundleBuildResult)


def encode_simulation_result(value: SimulationResult) -> bytes:
    return _encode_checked(value, SimulationResult)


def encode_simulator_bundle(value: SimulatorBundle) -> bytes:
    return _encode_checked(value, SimulatorBundle)


def encode_simulation_record_document(value: SimulationRecordDocument) -> bytes:
    """Canonically encode any Phase-1 record document value."""
    value_type = type(value)
    if value_type not in _DOCUMENT_SPECS:
        raise SimulationRecordIntegrityError(f"unsupported record value {type(value).__name__}")
    return _encode_checked(value, value_type)


def validate_relative_path(path: object) -> str:
    """Validate a normalized contained portable relative record path."""
    try:
        parsed = require_str_value(path, field="path")
    except BoundaryError as exc:
        raise SimulationRecordIntegrityError(str(exc)) from exc
    if len(parsed.encode("utf-8")) > MAX_PATH_BYTES:
        raise SimulationRecordIntegrityError("path must be nonempty and at most 1 KiB")
    if "\\" in parsed or parsed.startswith("/") or _WINDOWS_DRIVE_RE.match(parsed):
        raise SimulationRecordIntegrityError("path must be separator-normalized and relative")
    parts = parsed.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise SimulationRecordIntegrityError("path must be contained and normalized")
    return parsed

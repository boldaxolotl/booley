"""Strict canonical JSON codecs for immutable Simulation Campaign values.

This module is deliberately infrastructure-free. It decodes untrusted
documents into immutable values. Manifests are checked for shape only: planning proved their content and
published them atomically, so each manifest reader below reads just the members
its callers use (#2136). Attempt, build attempt, build result, bundle, snapshot and
result records get only the boundary check: size, UTF-8 JSON, exact fields, ``$schema``
and canonical bytes (#1743, #1744).
"""

from __future__ import annotations

import functools
import json
import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import ParamSpec, TypeAlias, TypeVar, cast

from booley.core.boundary import (
    BoundaryError,
    require_bool_value,
    require_dict,
    require_finite_number,
    require_int,
    require_list,
    require_sha256_digest,
    require_str_value,
    require_uuid4,
)
from booley.flows.campaign_results import OlderBooleyRecordError
from booley.flows.sim.campaign_files import canonical_json
from booley.flows.sim.record_layout import RECORD_DIRECTORY
from booley.runtime.timefmt import parse_timestamp

from .model import (
    BundleBuildAttempt,
    BundleBuildResult,
    ExecutableSnapshot,
    SimulationAttempt,
    SimulationCampaignDocument,
    SimulationCampaignManifest,
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
MAX_STRING_BYTES = 4_096
MAX_COMMAND_BYTES = 16 * 1024
MAX_OBSERVATION_TEST_BYTES = 512
MAX_DETAIL_BYTES = 2 * 1024
MAX_JSON_DEPTH = 64
MAX_LIST_ITEMS = 100_000

_VARIANT_ID_RE = re.compile(r"variant:[0-9a-f]{64}\Z")
_WINDOWS_DRIVE_RE = re.compile(r"[A-Za-z]:")


class SimulationCampaignIntegrityError(ValueError):
    """An authoritative campaign value violates its exact contract."""


class UnsupportedSimulationCampaignFormatError(OlderBooleyRecordError):
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

    def located(self, invocation: Path) -> UnsupportedSimulationCampaignFormatError:
        """The same refusal naming the invocation directory to delete."""
        return UnsupportedSimulationCampaignFormatError(self.label, self.schema, invocation)


def raise_if_retired_schema(schema: object) -> None:
    """Refuse a schema identifier an older Booley wrote, before any other check."""
    if isinstance(schema, str) and schema in _RETIRED_SCHEMAS:
        raise UnsupportedSimulationCampaignFormatError(_RETIRED_SCHEMAS[schema], schema)


def raise_if_retired(document: Mapping[str, object]) -> None:
    """Refuse a document in a format an older Booley wrote, before any other check."""
    raise_if_retired_schema(document.get("$schema"))


def canonical_json_bytes(value: object) -> bytes:
    """Encode finite JSON as sorted UTF-8 with one trailing newline."""
    try:
        return canonical_json(value)
    except (TypeError, ValueError) as exc:
        raise SimulationCampaignIntegrityError(f"value is not canonical JSON: {exc}") from exc


# v1 of the Simulation Record manifest (it replaces simulation-campaign-manifest/v4):
# the exact work-item shape with the required ``test_runs`` of every work item: the
# concrete Test Runs (Test Variant, seed, declared arguments and environment) the item
# executes (ADR 0072), an optional ``no_waivers`` and explicit declared source
# identities in disclosures. It is the only version written and read.
MANIFEST_SCHEMA = "booley.simulation-record-manifest/v1"

# v4 = the Simulation Record identity key, on top of v3's required ``test_run`` of every observation: the resolved name of the
# Test Run it observed (ADR 0072). Observations are unique by Test Run.
RESULT_SCHEMA = "booley.simulation-result/v4"

# Formats Booley 0.4.0 and earlier wrote. They are recognised only to be refused.
_RETIRED_SCHEMAS = {
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
        "executable_snapshot",
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
        "snapshot_inventory_sha256",
    }
)
_SNAPSHOT_FIELDS = frozenset(
    {
        "$schema",
        "simulation_record_id",
        "manifest_sha256",
        "workload_sha256",
        "work_item_id",
        "attempt_id",
        "build_result",
        "bundle_id",
        "created_at",
        "artifacts",
        "inventory_sha256",
    }
)

T = TypeVar("T", bound=SimulationCampaignDocument)
_P = ParamSpec("_P")
_R = TypeVar("_R")


def reads_campaign_records(function: Callable[_P, _R]) -> Callable[_P, _R]:
    """Report bad nested data in a published record as campaign corruption.

    Attempt, build, bundle and result decoders check only the document boundary (#1743, #1744),
    so the code that reads a nested member of a record from disk wraps itself
    here: a missing or mistyped member becomes the integrity error instead of a
    bare ``KeyError``/``TypeError``/``ValueError``/``AttributeError``. OS errors, including lock
    timeouts, pass through.
    """

    @functools.wraps(function)
    def guarded(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        try:
            return function(*args, **kwargs)
        except (SimulationCampaignIntegrityError, UnsupportedSimulationCampaignFormatError):
            raise
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            raise SimulationCampaignIntegrityError(
                f"published campaign record is malformed: {exc!r}"
            ) from exc

    return guarded


def _require_within_ceiling(raw: bytes, limit: int) -> None:
    """Refuse *raw* larger than its record's size ceiling."""
    if len(raw) > limit:
        raise SimulationCampaignIntegrityError(f"document exceeds {limit}-byte size ceiling")


def _decode(
    raw: bytes,
    *,
    schema: str,
    fields: frozenset[str],
    limit: int,
    value_type: type[T],
) -> T:
    _require_within_ceiling(raw, limit)
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise SimulationCampaignIntegrityError(f"document is not valid UTF-8 JSON: {exc}") from exc
    try:
        document = require_dict(value, field="document")
    except BoundaryError as exc:
        raise SimulationCampaignIntegrityError(str(exc)) from exc
    raise_if_retired(document)
    if set(document) != fields:
        missing = sorted(fields - set(document))
        unknown = sorted(set(document) - fields)
        raise SimulationCampaignIntegrityError(
            f"document must have exact fields; missing={missing}, unknown={unknown}"
        )
    if document.get("$schema") != schema:
        raise SimulationCampaignIntegrityError(f"unsupported $schema {document.get('$schema')!r}")
    try:
        # The depth/size walk bounds recursion for every record. Records without
        # a reader get nothing more than the canonical-bytes check (#1743);
        # their consumers guard what they use.
        _validate_json(document)
        reader = _DOCUMENT_READERS.get(schema)
        if reader is not None:
            _validate_common(document)
        if canonical_json_bytes(document) != raw:
            raise SimulationCampaignIntegrityError("document is not canonical JSON")
        if reader is not None:
            reader(document)
    except BoundaryError as exc:
        raise SimulationCampaignIntegrityError(str(exc)) from exc
    return value_type(cast(Mapping[str, FrozenJson], document))


def _validate_json(value: object, *, depth: int = 0) -> None:
    if depth > MAX_JSON_DEPTH:
        raise SimulationCampaignIntegrityError("JSON nesting exceeds resource ceiling")
    if isinstance(value, str):
        if len(value.encode("utf-8")) > MAX_COMMAND_BYTES:
            raise SimulationCampaignIntegrityError("string exceeds resource ceiling")
        return
    if isinstance(value, bool | int) or value is None:
        return
    if isinstance(value, float):
        require_finite_number(value, field="number")
        return
    if isinstance(value, list):
        if len(value) > MAX_LIST_ITEMS:
            raise SimulationCampaignIntegrityError("list exceeds resource ceiling")
        for item in value:
            _validate_json(item, depth=depth + 1)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            require_str_value(key, field="object key", allow_empty=True)
            _validate_json(item, depth=depth + 1)
        return
    raise SimulationCampaignIntegrityError(f"unsupported JSON type {type(value).__name__}")


def _require_digest(value: object, field: str) -> str:
    try:
        return require_sha256_digest(value, field=field)
    except BoundaryError as exc:
        raise SimulationCampaignIntegrityError(f"{field} must be a sha256 digest") from exc


def _require_uuid(value: object, field: str) -> str:
    try:
        return require_uuid4(value, field=field)
    except BoundaryError as exc:
        raise SimulationCampaignIntegrityError(f"{field} must be lowercase UUIDv4") from exc


def _require_variant_id(value: object) -> str:
    parsed = _bounded_string(value, "build_variant_id")
    if not _VARIANT_ID_RE.fullmatch(parsed):
        raise SimulationCampaignIntegrityError("build_variant_id is invalid")
    return parsed


def _bounded_string(
    value: object,
    field: str,
    *,
    limit: int = MAX_STRING_BYTES,
    allow_empty: bool = False,
) -> str:
    parsed = require_str_value(value, field=field, allow_empty=allow_empty)
    if len(parsed.encode("utf-8")) > limit:
        raise SimulationCampaignIntegrityError(f"{field} exceeds {limit}-byte ceiling")
    return parsed


def _bounded_string_list(
    value: object,
    field: str,
    *,
    count_limit: int,
    byte_limit: int,
) -> list[str]:
    items = _exact_list(value, field, count_limit)
    return [
        _bounded_string(item, f"{field}[{index}]", limit=byte_limit)
        for index, item in enumerate(items)
    ]


def _validate_common(value: Mapping[str, object]) -> None:
    campaign_id = value.get("simulation_record_id")
    if campaign_id is not None:
        _require_uuid(campaign_id, "simulation_record_id")
    for key in ("manifest_sha256", "workload_sha256"):
        digest = value.get(key)
        if digest is not None:
            _require_digest(digest, key)
    for key in ("created_at", "started_at", "finished_at"):
        timestamp = value.get(key)
        if timestamp is not None:
            parsed_value = _bounded_string(timestamp, key)
            try:
                parsed_timestamp = parse_timestamp(parsed_value)
            except (TypeError, ValueError) as exc:
                raise SimulationCampaignIntegrityError(
                    f"{key} must be canonical UTC RFC3339"
                ) from exc
            if "." in parsed_value or not parsed_value.endswith("Z"):
                raise SimulationCampaignIntegrityError(f"{key} must be canonical UTC RFC3339")
            del parsed_timestamp


def _exact_list(value: object, field: str, ceiling: int) -> list[object]:
    decoded = require_list(value, field=field)
    if len(decoded) > ceiling:
        raise SimulationCampaignIntegrityError(f"{field} must be a list capped at {ceiling}")
    return decoded


def _read_manifest(value: Mapping[str, object]) -> None:
    """Read the members manifest readers use; planning proved the rest."""
    origin = require_dict(value["origin"], field="origin")
    require_int(origin.get("invocation_id"), field="origin.invocation_id")
    _read_strings(value["target"], "target", ("vlnv", "name", "selector", "revision", "role"))
    _read_workload(value["workload"])
    _read_required_suite(value["required_suite"])
    _read_build_variant_ids(value["build_variants"])
    for index, item in enumerate(require_list(value["prerequisites"], field="prerequisites")):
        _read_prerequisite(item, f"prerequisites[{index}]")
    for index, item in enumerate(require_list(value["work_items"], field="work_items")):
        _read_work_item(item, f"work_items[{index}]")
    fingerprints = require_dict(value["fingerprints"], field="fingerprints")
    require_str_value(fingerprints.get("workload_sha256"), field="fingerprints.workload_sha256")


def _read_strings(value: object, field: str, keys: tuple[str, ...]) -> dict[str, object]:
    """Read *value* as an object whose *keys* are nonempty strings."""
    mapping = require_dict(value, field=field)
    for key in keys:
        require_str_value(mapping.get(key), field=f"{field}.{key}")
    return mapping


def _read_prerequisite(value: object, field: str) -> None:
    """Read the prerequisite members resume, the Pre-Sim walk and acceptance use."""
    entry = _read_strings(value, field, ("simulation_record_id", "work_item_id"))
    require_dict(entry.get("target"), field=f"{field}.target")
    reference = _read_strings(entry.get("manifest"), f"{field}.manifest", ("path", "sha256"))
    require_int(reference.get("bytes"), field=f"{field}.manifest.bytes")


def _read_work_item(value: object, field: str) -> None:
    """Read the work-item members the store, scheduler and reports use."""
    item = _read_strings(
        value, field, ("work_item_id", "kind", "role", "revision", "build_variant_id")
    )
    require_int(item.get("ordinal"), field=f"{field}.ordinal")
    require_dict(item.get("target"), field=f"{field}.target")
    selection = _read_strings(item.get("selection"), f"{field}.selection", ("kind",))
    for name in require_list(selection.get("names"), field=f"{field}.selection.names"):
        require_str_value(name, field=f"{field}.selection.names[]")
    for index, run in enumerate(require_list(item.get("test_runs"), field=f"{field}.test_runs")):
        # Full Test Run parsing stays at its uses; name, fingerprint and test are read raw.
        entry = _read_strings(run, f"{field}.test_runs[{index}]", ("name", "fingerprint"))
        if entry.get("test") is not None:
            require_str_value(entry["test"], field=f"{field}.test_runs[{index}].test")


def _read_workload(value: object) -> None:
    """Read the workload members manifest readers use; planning proved the rest."""
    workload = require_dict(value, field="workload")
    require_bool_value(workload.get("trace"), field="workload.trace")
    require_bool_value(workload.get("coverage"), field="workload.coverage")
    eda = require_dict(workload.get("eda"), field="workload.eda")
    for key in ("kind", "version"):
        require_str_value(eda.get(key), field=f"workload.eda.{key}")
    require_str_value(
        workload.get("adapter_contract_version"), field="workload.adapter_contract_version"
    )
    run_cwd = require_dict(workload.get("run_cwd"), field="workload.run_cwd")
    for key in ("configured", "kind"):
        require_str_value(run_cwd.get(key), field=f"workload.run_cwd.{key}")


def _read_required_suite(value: object) -> None:
    """Read the required-suite members resume and acceptance use."""
    suite = require_dict(value, field="required_suite")
    require_bool_value(suite.get("default_invocation"), field="required_suite.default_invocation")
    _bounded_string_list(
        suite.get("names"),
        "required_suite.names",
        count_limit=MAX_OBSERVATIONS,
        byte_limit=MAX_OBSERVATION_TEST_BYTES,
    )


def _read_build_variant_ids(value: object) -> None:
    """Read the build-variant IDs inspection and retention use."""
    for index, item in enumerate(_exact_list(value, "build_variants", MAX_WORK_ITEMS)):
        variant = require_dict(item, field=f"build_variants[{index}]")
        _require_variant_id(variant.get("build_variant_id"))


_DOCUMENT_SPECS: dict[
    type[SimulationCampaignDocument],
    tuple[str, frozenset[str], int, Callable[[Mapping[str, object]], None] | None],
] = {
    SimulationCampaignManifest: (
        MANIFEST_SCHEMA,
        _MANIFEST_FIELDS,
        MANIFEST_MAX_BYTES,
        _read_manifest,
    ),
    SimulationAttempt: (
        "booley.simulation-attempt/v2",
        _ATTEMPT_FIELDS,
        RECORD_MAX_BYTES,
        None,
    ),
    BundleBuildAttempt: (
        "booley.bundle-build-attempt/v2",
        _BUILD_ATTEMPT_FIELDS,
        RECORD_MAX_BYTES,
        None,
    ),
    BundleBuildResult: (
        "booley.bundle-build-result/v2",
        _BUILD_RESULT_FIELDS,
        RECORD_MAX_BYTES,
        None,
    ),
    SimulationResult: (
        RESULT_SCHEMA,
        _RESULT_FIELDS,
        RECORD_MAX_BYTES,
        None,
    ),
    SimulatorBundle: (
        "booley.simulator-bundle/v2",
        _BUNDLE_FIELDS,
        MANIFEST_MAX_BYTES,
        None,
    ),
    ExecutableSnapshot: (
        "booley.executable-snapshot/v2",
        _SNAPSHOT_FIELDS,
        MANIFEST_MAX_BYTES,
        None,
    ),
}
_DOCUMENT_READERS = {spec[0]: spec[3] for spec in _DOCUMENT_SPECS.values() if spec[3] is not None}


def _decode_registered(raw: bytes, value_type: type[T]) -> T:
    schema, fields, limit, _ = _DOCUMENT_SPECS[value_type]
    return _decode(raw, schema=schema, fields=fields, limit=limit, value_type=value_type)


def decode_simulation_campaign_manifest(raw: bytes) -> SimulationCampaignManifest:
    return _decode_registered(raw, SimulationCampaignManifest)


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


def decode_executable_snapshot(raw: bytes) -> ExecutableSnapshot:
    return _decode_registered(raw, ExecutableSnapshot)


def _encode_checked(
    value: SimulationCampaignDocument,
    expected_type: type[T],
) -> bytes:
    if type(value) is not expected_type:
        raise SimulationCampaignIntegrityError(f"expected {expected_type.__name__}")
    raw = canonical_json_bytes(value.document)
    # Builders never decode what they build, so publication enforces each record's ceiling.
    _require_within_ceiling(raw, _DOCUMENT_SPECS[expected_type][2])
    return raw


def encode_simulation_campaign_manifest(value: SimulationCampaignManifest) -> bytes:
    return _encode_checked(value, SimulationCampaignManifest)


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


def encode_executable_snapshot(value: ExecutableSnapshot) -> bytes:
    return _encode_checked(value, ExecutableSnapshot)


def encode_simulation_campaign_document(value: SimulationCampaignDocument) -> bytes:
    """Canonically encode any Phase-1 campaign document value."""
    value_type = type(value)
    if value_type not in _DOCUMENT_SPECS:
        raise SimulationCampaignIntegrityError(
            f"unsupported campaign value {type(value).__name__}"
        )
    return _encode_checked(value, value_type)


def validate_relative_path(path: object) -> str:
    """Validate a normalized contained portable relative campaign path."""
    try:
        parsed = require_str_value(path, field="path")
    except BoundaryError as exc:
        raise SimulationCampaignIntegrityError(str(exc)) from exc
    if len(parsed.encode("utf-8")) > MAX_PATH_BYTES:
        raise SimulationCampaignIntegrityError("path must be nonempty and at most 1 KiB")
    if "\\" in parsed or parsed.startswith("/") or _WINDOWS_DRIVE_RE.match(parsed):
        raise SimulationCampaignIntegrityError("path must be separator-normalized and relative")
    parts = parsed.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise SimulationCampaignIntegrityError("path must be contained and normalized")
    return parsed


def linked_manifest_path(invocation: Path, reference: Mapping[str, object]) -> Path:
    """The prerequisite manifest *reference* names, inside its *invocation*.

    Decode does not check the path, so every walk checks it before opening it:
    exactly ``targets/<Target>/record/manifest.json``, relative and contained,
    which is what planning writes. That also keeps the invocation the same at
    every hop of a prerequisite walk.
    """
    path = reference["path"]
    parts = path.split("/") if isinstance(path, str) else []
    if (
        len(parts) != 4
        or parts[0] != "targets"
        or parts[2:] != [RECORD_DIRECTORY, "manifest.json"]
    ):
        raise SimulationCampaignIntegrityError(
            f"prerequisite manifest path {path!r} is not "
            "targets/<Target>/record/manifest.json inside its invocation"
        )
    return invocation / validate_relative_path(path)

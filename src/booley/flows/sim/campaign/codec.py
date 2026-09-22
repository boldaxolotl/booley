"""Strict canonical JSON codecs for immutable Simulation Campaign values.

This module is deliberately infrastructure-free. It validates untrusted
documents into immutable values that later store/execution modules can trust.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from string import Formatter
from typing import TypeAlias, TypeVar, cast

from booley.core.boundary import (
    BoundaryError,
    require_bool_value,
    require_dict,
    require_finite_number,
    require_int,
    require_list,
    require_str_value,
)
from booley.runtime.timefmt import parse_timestamp

from .model import (
    AssertionObservation,
    BundleBuildAttempt,
    BundleBuildResult,
    ExecutableSnapshot,
    ExecutionObservation,
    FailureClass,
    FunctionalObservation,
    SimulationAttempt,
    SimulationCampaignDocument,
    SimulationCampaignManifest,
    SimulationResult,
    SimulatorBundle,
    StrictGrade,
    grade_observations,
)

JsonScalar: TypeAlias = str | int | float | bool | None
FrozenJson: TypeAlias = JsonScalar | tuple["FrozenJson", ...] | Mapping[str, "FrozenJson"]

MANIFEST_MAX_BYTES = 16 * 1024 * 1024
SUMMARY_MAX_BYTES = 16 * 1024 * 1024
RECORD_MAX_BYTES = 1024 * 1024
MAX_WORK_ITEMS = 1_000
MAX_OBSERVATIONS = 4_000
MAX_ATTEMPTS_PER_ITEM = 10_000
MAX_EVIDENCE_REFS = 4_096
MAX_PATH_BYTES = 1_024
MAX_STRING_BYTES = 4_096
MAX_COMMAND_BYTES = 16 * 1024
MAX_OBSERVATION_TEST_BYTES = 512
MAX_DETAIL_BYTES = 2 * 1024
MAX_JSON_DEPTH = 64
MAX_LIST_ITEMS = 100_000

_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_EXECUTION_ID_RE = re.compile(r"[0-9a-f]{32}\Z")
_VARIANT_ID_RE = re.compile(r"variant:[0-9a-f]{64}\Z")
_WORK_ITEM_ID_RE = re.compile(r"item:[0-9]{4}:[0-9a-f]{16}\Z")
_WINDOWS_DRIVE_RE = re.compile(r"[A-Za-z]:")


class SimulationCampaignIntegrityError(ValueError):
    """An authoritative campaign value violates its exact contract."""


def _thaw(value: FrozenJson) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_thaw(item) for item in value]
    return value


def canonical_json_bytes(value: object) -> bytes:
    """Encode finite JSON as sorted UTF-8 with one trailing newline."""
    thawed = _thaw(cast(FrozenJson, value))
    try:
        text = json.dumps(
            thawed,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise SimulationCampaignIntegrityError(f"value is not canonical JSON: {exc}") from exc
    return text.encode("utf-8") + b"\n"


_MANIFEST_FIELDS = frozenset(
    {
        "$schema",
        "campaign_id",
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
        "campaign_id",
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
        "campaign_id",
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
        "campaign_id",
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
        "campaign_id",
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
        "campaign_id",
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
        "campaign_id",
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


def _decode(
    raw: bytes,
    *,
    schema: str,
    fields: frozenset[str],
    limit: int,
    value_type: type[T],
) -> T:
    if len(raw) > limit:
        raise SimulationCampaignIntegrityError(f"document exceeds {limit}-byte size ceiling")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise SimulationCampaignIntegrityError(f"document is not valid UTF-8 JSON: {exc}") from exc
    try:
        document = require_dict(value, field="document")
    except BoundaryError as exc:
        raise SimulationCampaignIntegrityError(str(exc)) from exc
    if set(document) != fields:
        missing = sorted(fields - set(document))
        unknown = sorted(set(document) - fields)
        raise SimulationCampaignIntegrityError(
            f"document must have exact fields; missing={missing}, unknown={unknown}"
        )
    if document.get("$schema") != schema:
        raise SimulationCampaignIntegrityError(f"unsupported $schema {document.get('$schema')!r}")
    try:
        _validate_json(document)
        _validate_common(document)
        if canonical_json_bytes(document) != raw:
            raise SimulationCampaignIntegrityError("document is not canonical JSON")
        _validate_document(schema, document)
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


def _require_positive_int(value: object, field: str) -> int:
    try:
        parsed = require_int(value, field=field)
    except BoundaryError as exc:
        raise SimulationCampaignIntegrityError(f"{field} must be a positive integer") from exc
    if parsed <= 0:
        raise SimulationCampaignIntegrityError(f"{field} must be a positive integer")
    return parsed


def _require_nonnegative_int(value: object, field: str) -> int:
    try:
        parsed = require_int(value, field=field)
    except BoundaryError as exc:
        raise SimulationCampaignIntegrityError(f"{field} must be a nonnegative integer") from exc
    if parsed < 0:
        raise SimulationCampaignIntegrityError(f"{field} must be a nonnegative integer")
    return parsed


def _require_digest(value: object, field: str) -> str:
    try:
        parsed = require_str_value(value, field=field)
    except BoundaryError as exc:
        raise SimulationCampaignIntegrityError(str(exc)) from exc
    if not _DIGEST_RE.fullmatch(parsed):
        raise SimulationCampaignIntegrityError(f"{field} must be a sha256 digest")
    return parsed


def _require_uuid(value: object, field: str) -> str:
    try:
        parsed_value = require_str_value(value, field=field)
    except BoundaryError as exc:
        raise SimulationCampaignIntegrityError(str(exc)) from exc
    try:
        parsed = uuid.UUID(parsed_value)
    except ValueError as exc:
        raise SimulationCampaignIntegrityError(f"{field} must be lowercase UUIDv4") from exc
    if parsed.version != 4 or str(parsed) != parsed_value:
        raise SimulationCampaignIntegrityError(f"{field} must be lowercase UUIDv4")
    return parsed_value


def _require_variant_id(value: object) -> str:
    parsed = _bounded_string(value, "build_variant_id")
    if not _VARIANT_ID_RE.fullmatch(parsed):
        raise SimulationCampaignIntegrityError("build_variant_id is invalid")
    return parsed


def _require_work_item_id(value: object) -> str:
    parsed = _bounded_string(value, "work_item_id")
    if not _WORK_ITEM_ID_RE.fullmatch(parsed):
        raise SimulationCampaignIntegrityError("work_item_id is invalid")
    return parsed


def _require_nonnegative_number(value: object, field: str) -> None:
    parsed = require_finite_number(value, field=field)
    if parsed < 0:
        raise SimulationCampaignIntegrityError(f"{field} must be a finite nonnegative number")


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


def _bounded_canonical_value(value: object, field: str, limit: int) -> None:
    _validate_json(value)
    if len(canonical_json_bytes(value)) - 1 > limit:
        raise SimulationCampaignIntegrityError(f"{field} exceeds {limit}-byte ceiling")


def _validate_bounded_json_strings(value: object, field: str) -> None:
    """Apply the ordinary-string ceiling recursively to a JSON value."""
    if isinstance(value, str):
        _bounded_string(value, field, allow_empty=True)
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_bounded_json_strings(item, f"{field}[{index}]")
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            _bounded_string(key, f"{field} key", allow_empty=True)
            _validate_bounded_json_strings(item, f"{field}.{key}")


def _bounded_string_list(
    value: object,
    field: str,
    *,
    count_limit: int,
    byte_limit: int,
    unique: bool = False,
) -> list[str]:
    items = _exact_list(value, field, count_limit)
    decoded = [
        _bounded_string(item, f"{field}[{index}]", limit=byte_limit)
        for index, item in enumerate(items)
    ]
    if unique and len(set(decoded)) != len(decoded):
        raise SimulationCampaignIntegrityError(f"{field} must contain unique strings")
    return decoded


def _validate_common(value: Mapping[str, object]) -> None:
    campaign_id = value.get("campaign_id")
    if campaign_id is not None:
        _require_uuid(campaign_id, "campaign_id")
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


def _validate_document(schema: str, value: Mapping[str, object]) -> None:
    _DOCUMENT_VALIDATORS[schema](value)


def _validate_attempt_identity(value: Mapping[str, object], ordinal_key: str) -> None:
    ordinal = _require_positive_int(value[ordinal_key], ordinal_key)
    if ordinal > MAX_ATTEMPTS_PER_ITEM:
        raise SimulationCampaignIntegrityError(
            f"{ordinal_key} exceeds {MAX_ATTEMPTS_PER_ITEM}-attempt ceiling"
        )
    _require_positive_int(value["producer_invocation_id"], "producer_invocation_id")


def _validate_simulation_attempt(value: Mapping[str, object]) -> None:
    _validate_attempt_identity(value, "attempt_ordinal")
    _require_uuid(value["attempt_id"], "attempt_id")
    if value["pre_sim_build_access"] not in {"immutable", "legacy-per-test"}:
        raise SimulationCampaignIntegrityError("attempt pre_sim_build_access is invalid")
    _require_variant_id(value["build_variant_id"])
    _require_work_item_id(value["work_item_id"])
    run_directory = _exact_object(
        value["run_directory"],
        {"kind", "configured", "resolved", "collision_key", "owned"},
        "run_directory",
    )
    if run_directory["kind"] not in {"literal", "templated"}:
        raise SimulationCampaignIntegrityError("invalid run directory kind")
    configured = _bounded_string(
        run_directory["configured"], "run_directory.configured", allow_empty=True
    )
    _validate_configured_run_kind(configured, cast(str, run_directory["kind"]))
    _validate_run_directory_path(run_directory["resolved"], "run_directory.resolved")
    _validate_run_directory_path(run_directory["collision_key"], "run_directory.collision_key")
    require_bool_value(run_directory["owned"], field="run_directory.owned")
    policy = _exact_object(value["policy"], {"timeout_seconds", "no_kill", "diagnostic"}, "policy")
    if policy["timeout_seconds"] is not None:
        _require_positive_int(policy["timeout_seconds"], "policy.timeout_seconds")
    require_bool_value(policy["no_kill"], field="policy.no_kill")
    require_bool_value(policy["diagnostic"], field="policy.diagnostic")
    _validate_child_identity(value)


def _validate_child_identity(value: Mapping[str, object]) -> None:
    child_id, child_digest = value["child_execution_id"], value["child_entry_sha256"]
    if (child_id is None) != (child_digest is None):
        raise SimulationCampaignIntegrityError(
            "child execution fields must both be null or present"
        )
    if child_id is not None:
        parsed_child_id = _bounded_string(child_id, "child_execution_id")
        if not _EXECUTION_ID_RE.fullmatch(parsed_child_id):
            raise SimulationCampaignIntegrityError("child_execution_id must be 32 lowercase hex")
    if child_digest is not None:
        _require_digest(child_digest, "child_entry_sha256")


def _validate_build_attempt(value: Mapping[str, object]) -> None:
    _validate_attempt_identity(value, "build_attempt_ordinal")
    _require_uuid(value["build_attempt_id"], "build_attempt_id")
    _require_variant_id(value["build_variant_id"])
    owner = _exact_object(value["owner"], {"work_item_id", "simulation_attempt_id"}, "owner")
    provenance = _exact_object(
        value["tool_provenance"],
        {"eda_kind", "eda_version", "adapter_contract_version"},
        "tool_provenance",
    )
    for key, item in provenance.items():
        _bounded_string(item, f"tool_provenance.{key}")
    sharing = value["sharing"]
    if sharing not in {"shared_variant", "private_work_item"}:
        raise SimulationCampaignIntegrityError("invalid build sharing scope")
    owner_values = tuple(owner.values())
    if any(item is None for item in owner_values) != all(item is None for item in owner_values):
        raise SimulationCampaignIntegrityError("build owner fields must both be null or present")
    expected_private = all(item is not None for item in owner_values)
    if (sharing == "private_work_item") != expected_private:
        raise SimulationCampaignIntegrityError("build owner disagrees with sharing scope")
    if expected_private:
        _require_work_item_id(owner["work_item_id"])
        _require_uuid(owner["simulation_attempt_id"], "owner.simulation_attempt_id")


def _validate_build_result(value: Mapping[str, object]) -> None:
    _require_nonnegative_number(value["elapsed_seconds"], "elapsed_seconds")
    _validate_build_result_ref(value["build_attempt"])
    states = {
        "ready": ({"ready"}, True, False),
        "design_failure": ({"pre_sim", "compile", "elaboration"}, False, True),
        "infrastructure_error": ({"pre_sim", "build_transport", "storage"}, False, True),
    }
    if value["state"] not in states:
        raise SimulationCampaignIntegrityError("invalid build result state")
    phases, has_bundle, has_observation = states[cast(str, value["state"])]
    if value["phase"] not in phases:
        raise SimulationCampaignIntegrityError("build result phase disagrees with state")
    if (value["bundle"] is not None) != has_bundle or (
        value["observation"] is not None
    ) != has_observation:
        raise SimulationCampaignIntegrityError("build result nullability disagrees with state")
    _validate_bundle(value["bundle"])
    _validate_build_observation(value["observation"])
    if value["observation"] is not None:
        observed_class = cast(Mapping[str, object], value["observation"])["class"]
        if (value["state"] == "design_failure") != (observed_class == "design"):
            raise SimulationCampaignIntegrityError("build observation class disagrees with state")
    _validate_evidence_list(value["evidence"], "evidence")


def _validate_simulation_result(value: Mapping[str, object]) -> None:
    _require_nonnegative_number(value["elapsed_seconds"], "elapsed_seconds")
    _validate_attempt_identity(value, "attempt_ordinal")
    _require_uuid(value["attempt_id"], "attempt_id")
    _require_work_item_id(value["work_item_id"])
    build_result = _validate_simulation_build_ref(value["build_result"])
    state = value["state"]
    if state not in {"completed", "timeout", "crash", "setup_error", "blocked_by_build"}:
        raise SimulationCampaignIntegrityError("invalid simulation result state")
    if value["grade"] not in {grade.value for grade in StrictGrade}:
        raise SimulationCampaignIntegrityError("invalid strict grade")
    _validate_result_state(value, build_result)
    _validate_runtime_inputs(value["runtime_inputs"], blocked=state == "blocked_by_build")
    observations = _validate_observations(value["observations"], cast(str, state))
    _validate_result_grade(value["grade"], observations)
    _validate_diagnostics(value["diagnostics"])
    _validate_evidence_list(value["evidence"], "evidence")


def _validate_bundle_document(value: Mapping[str, object]) -> None:
    _require_uuid(value["build_attempt_id"], "build_attempt_id")
    _require_uuid(value["bundle_id"], "bundle_id")
    _require_variant_id(value["build_variant_id"])
    owner = _exact_object(value["owner"], {"work_item_id", "simulation_attempt_id"}, "owner")
    provenance = _exact_object(
        value["tool_provenance"],
        {"eda_kind", "eda_version", "adapter_contract_version"},
        "tool_provenance",
    )
    for key, item in provenance.items():
        _bounded_string(item, f"tool_provenance.{key}")
    artifacts = _exact_list(value["artifacts"], "artifacts", MAX_EVIDENCE_REFS)
    decoded = [_validate_bundle_artifact(item, index) for index, item in enumerate(artifacts)]
    if len({item["path"] for item in decoded}) != len(decoded):
        raise SimulationCampaignIntegrityError("bundle artifact paths must be unique")
    if value["inventory_sha256"] != _digest(artifacts):
        raise SimulationCampaignIntegrityError("bundle inventory digest disagrees")
    snapshot = [
        item for item in artifacts if cast(Mapping[str, object], item)["kind"] != "runtime_input"
    ]
    if value["snapshot_inventory_sha256"] != _digest(snapshot):
        raise SimulationCampaignIntegrityError("bundle snapshot inventory digest disagrees")
    private = value["sharing"] == "private_work_item"
    owner_values = tuple(owner.values())
    if any(item is None for item in owner_values) != all(item is None for item in owner_values):
        raise SimulationCampaignIntegrityError("bundle owner fields must both be null or present")
    if value["sharing"] not in {"shared_variant", "private_work_item"} or private != all(
        item is not None for item in owner_values
    ):
        raise SimulationCampaignIntegrityError("bundle sharing/owner disagrees")
    if private:
        _require_work_item_id(owner["work_item_id"])
        _require_uuid(owner["simulation_attempt_id"], "owner.simulation_attempt_id")


def _validate_snapshot_document(value: Mapping[str, object]) -> None:
    _require_uuid(value["attempt_id"], "attempt_id")
    _require_uuid(value["bundle_id"], "bundle_id")
    _require_work_item_id(value["work_item_id"])
    _validate_simulation_build_ref(value["build_result"])
    artifacts = _exact_list(value["artifacts"], "artifacts", MAX_EVIDENCE_REFS)
    decoded = [_validate_bundle_artifact(item, index) for index, item in enumerate(artifacts)]
    if any(item["kind"] == "runtime_input" for item in decoded):
        raise SimulationCampaignIntegrityError("snapshot cannot contain runtime inputs")
    if len({item["path"] for item in decoded}) != len(decoded):
        raise SimulationCampaignIntegrityError("snapshot artifact paths must be unique")
    if value["inventory_sha256"] != _digest(artifacts):
        raise SimulationCampaignIntegrityError("snapshot inventory digest disagrees")


def _exact_object(value: object, fields: set[str], field: str) -> Mapping[str, object]:
    decoded = require_dict(value, field=field)
    if set(decoded) != fields:
        raise SimulationCampaignIntegrityError(f"{field} must have exact fields {sorted(fields)}")
    return cast(Mapping[str, object], decoded)


def _exact_list(value: object, field: str, ceiling: int) -> list[object]:
    decoded = require_list(value, field=field)
    if len(decoded) > ceiling:
        raise SimulationCampaignIntegrityError(f"{field} must be a list capped at {ceiling}")
    return decoded


def _validate_build_result_ref(value: object) -> Mapping[str, object]:
    reference = _exact_object(
        value,
        {"path", "bytes", "sha256", "kind", "owner", "build_attempt_id"},
        "build_attempt",
    )
    if reference["kind"] != "bundle_build_attempt":
        raise SimulationCampaignIntegrityError("build_attempt reference has wrong kind")
    _validate_evidence_members(reference, "build_attempt")
    _require_uuid(reference["build_attempt_id"], "build_attempt_id")
    if reference["owner"] != reference["build_attempt_id"]:
        raise SimulationCampaignIntegrityError("build_attempt owner disagrees with identity")
    return reference


def _validate_simulation_build_ref(value: object) -> Mapping[str, object]:
    reference = _exact_object(
        value,
        {
            "path",
            "bytes",
            "sha256",
            "kind",
            "owner",
            "build_attempt_id",
            "state",
            "sharing",
        },
        "build_result",
    )
    if reference["kind"] != "bundle_build_result":
        raise SimulationCampaignIntegrityError("build_result reference has wrong kind")
    _validate_evidence_members(reference, "build_result")
    if reference["state"] not in {"ready", "design_failure", "infrastructure_error"}:
        raise SimulationCampaignIntegrityError("simulation result references invalid build state")
    if reference["sharing"] not in {"shared_variant", "private_work_item"}:
        raise SimulationCampaignIntegrityError(
            "simulation result references invalid sharing scope"
        )
    _require_uuid(reference["build_attempt_id"], "build_attempt_id")
    if reference["owner"] != reference["build_attempt_id"]:
        raise SimulationCampaignIntegrityError("build_result owner disagrees with identity")
    return reference


def _validate_bundle(value: object) -> None:
    if value is None:
        return
    bundle = _exact_object(
        value,
        {
            "bundle_id",
            "manifest_path",
            "manifest_bytes",
            "manifest_sha256",
            "sharing",
            "artifacts",
        },
        "bundle",
    )
    _require_uuid(bundle["bundle_id"], "bundle.bundle_id")
    validate_relative_path(cast(str, bundle["manifest_path"]))
    _require_nonnegative_int(bundle["manifest_bytes"], "bundle.manifest_bytes")
    _require_digest(bundle["manifest_sha256"], "bundle.manifest_sha256")
    if bundle["sharing"] not in {"shared_variant", "private_work_item"}:
        raise SimulationCampaignIntegrityError("bundle sharing scope is invalid")
    artifacts = _exact_list(bundle["artifacts"], "bundle.artifacts", MAX_EVIDENCE_REFS)
    decoded = [_validate_bundle_artifact(item, index) for index, item in enumerate(artifacts)]
    paths = [item["path"] for item in decoded]
    if len(set(paths)) != len(paths):
        raise SimulationCampaignIntegrityError("bundle artifact paths must be unique")
    if not any(item["kind"] == "simulator_executable" for item in decoded):
        raise SimulationCampaignIntegrityError("ready bundle requires a simulator executable")


def _validate_bundle_artifact(value: object, index: int) -> Mapping[str, object]:
    artifact = _exact_object(
        value, {"path", "bytes", "sha256", "kind"}, f"bundle.artifacts[{index}]"
    )
    validate_relative_path(cast(str, artifact["path"]))
    _require_nonnegative_int(artifact["bytes"], "artifact.bytes")
    _require_digest(artifact["sha256"], "artifact.sha256")
    if artifact["kind"] not in {
        "simulator_executable",
        "shared_library",
        "runtime_data",
        "runtime_input",
    }:
        raise SimulationCampaignIntegrityError("bundle artifact kind is invalid")
    return artifact


def _validate_build_observation(value: object) -> None:
    if value is None:
        return
    observation = _exact_object(value, {"class", "code", "message", "detail"}, "build observation")
    if observation["class"] not in {"design", "infrastructure"}:
        raise SimulationCampaignIntegrityError("build observation class is invalid")
    for key in ("code", "message"):
        _bounded_string(observation[key], f"build observation {key}")
    _bounded_canonical_value(observation["detail"], "build observation detail", MAX_DETAIL_BYTES)


def _validate_result_state(
    value: Mapping[str, object], build_result: Mapping[str, object]
) -> None:
    state = value["state"]
    if state == "blocked_by_build":
        if build_result["state"] not in {"design_failure", "infrastructure_error"}:
            raise SimulationCampaignIntegrityError("blocked result requires terminal failed build")
        if value["bundle_id"] is not None or value["executable_snapshot"] is not None:
            raise SimulationCampaignIntegrityError(
                "blocked result cannot bind a bundle or snapshot"
            )
        return
    if build_result["state"] != "ready":
        raise SimulationCampaignIntegrityError("non-blocked result requires ready build")
    _require_uuid(value["bundle_id"], "bundle_id")
    if state in {"completed", "timeout", "crash"} and value["executable_snapshot"] is None:
        raise SimulationCampaignIntegrityError("post-launch result requires executable snapshot")
    _validate_executable_snapshot(value["executable_snapshot"], value["attempt_id"])


def _validate_executable_snapshot(value: object, attempt_id: object) -> None:
    if value is None:
        return
    snapshot = _exact_object(
        value,
        {
            "manifest",
            "bundle_manifest_sha256",
            "pre_launch_sha256",
            "post_exit_sha256",
            "verified_after_exit",
        },
        "executable_snapshot",
    )
    manifest = _validate_evidence_ref(snapshot["manifest"], "executable_snapshot.manifest")
    if manifest["kind"] != "executable_snapshot_manifest" or manifest["owner"] != attempt_id:
        raise SimulationCampaignIntegrityError("executable snapshot manifest identity disagrees")
    for key in ("bundle_manifest_sha256", "pre_launch_sha256", "post_exit_sha256"):
        _require_digest(snapshot[key], f"executable_snapshot.{key}")
    if snapshot["pre_launch_sha256"] != snapshot["post_exit_sha256"]:
        raise SimulationCampaignIntegrityError("executable snapshot changed during execution")
    if snapshot["verified_after_exit"] is not True:
        raise SimulationCampaignIntegrityError("executable snapshot must be verified after exit")


def _validate_runtime_inputs(value: object, *, blocked: bool) -> None:
    inputs = _exact_list(value, "runtime_inputs", MAX_EVIDENCE_REFS)
    if blocked and inputs:
        raise SimulationCampaignIntegrityError("blocked result runtime_inputs must be empty")
    destinations: list[object] = []
    declarations: list[object] = []
    for index, item in enumerate(inputs):
        binding = _exact_object(
            item,
            {
                "declaration_id",
                "authoritative_copy",
                "destination",
                "method",
                "destination_bytes",
                "destination_sha256",
                "owned",
            },
            f"runtime_inputs[{index}]",
        )
        _require_digest(binding["declaration_id"], "runtime input declaration_id")
        _validate_evidence_ref(binding["authoritative_copy"], "authoritative_copy")
        validate_relative_path(cast(str, binding["destination"]))
        if binding["method"] not in {"copy", "link", "identical_existing"}:
            raise SimulationCampaignIntegrityError("runtime input method is invalid")
        _require_nonnegative_int(binding["destination_bytes"], "destination_bytes")
        _require_digest(binding["destination_sha256"], "destination_sha256")
        authoritative = cast(Mapping[str, object], binding["authoritative_copy"])
        if (
            authoritative["bytes"] != binding["destination_bytes"]
            or authoritative["sha256"] != binding["destination_sha256"]
        ):
            raise SimulationCampaignIntegrityError("runtime input copy and destination disagree")
        require_bool_value(binding["owned"], field="runtime input owned")
        destinations.append(binding["destination"])
        declarations.append(binding["declaration_id"])
    if len(set(destinations)) != len(destinations) or len(set(declarations)) != len(declarations):
        raise SimulationCampaignIntegrityError("runtime input identities must be unique")


def _validate_observations(value: object, state: str) -> list[Mapping[str, object]]:
    observations = _exact_list(value, "observations", MAX_OBSERVATIONS)
    if not observations:
        raise SimulationCampaignIntegrityError("result observations must be nonempty")
    tests: list[object] = []
    decoded: list[Mapping[str, object]] = []
    for index, item in enumerate(observations):
        observation = _validate_observation(item, index)
        tests.append(observation["test"])
        decoded.append(observation)
        if state == "completed" and observation["execution"] != "completed":
            raise SimulationCampaignIntegrityError(
                "completed result requires completed observations"
            )
        if state == "blocked_by_build" and observation["execution"] != "blocked_by_build":
            raise SimulationCampaignIntegrityError("blocked result requires blocked observations")
        if state == "setup_error" and observation["execution"] != "setup_error":
            raise SimulationCampaignIntegrityError("setup result requires setup observations")
    named = [test for test in tests if test is not None]
    if len(set(named)) != len(named):
        raise SimulationCampaignIntegrityError("observation test names must be unique")
    if state in {"completed", "timeout", "crash"}:
        strength = {"completed": 0, "timeout": 1, "crash": 2}
        expected = max(
            (cast(str, item["execution"]) for item in decoded),
            key=lambda execution: strength.get(execution, 3),
        )
        if expected != state:
            raise SimulationCampaignIntegrityError("result state disagrees with observations")
    return decoded


def _validate_result_grade(value: object, observations: list[Mapping[str, object]]) -> None:
    grades = [
        grade_observations(
            ExecutionObservation(cast(str, item["execution"])),
            FailureClass(cast(str, item["failure_class"])) if item["failure_class"] else None,
            FunctionalObservation(cast(str, item["functional"])),
            AssertionObservation(cast(str, item["assertions"])),
        )
        for item in observations
    ]
    precedence = {
        StrictGrade.PASS: 0,
        StrictGrade.INCONCLUSIVE: 1,
        StrictGrade.FAIL: 2,
        StrictGrade.ERROR: 3,
    }
    expected = max(grades, key=precedence.__getitem__)
    if value != expected.value:
        raise SimulationCampaignIntegrityError("result grade disagrees with observations")


def _validate_observation(value: object, index: int) -> Mapping[str, object]:
    observation = _exact_object(
        value,
        {
            "test",
            "execution",
            "failure_class",
            "functional",
            "assertions",
            "assertion_count",
            "detail",
            "cycle_count",
        },
        f"observations[{index}]",
    )
    if observation["execution"] not in {
        "completed",
        "timeout",
        "crash",
        "setup_error",
        "blocked_by_build",
    }:
        raise SimulationCampaignIntegrityError("observation execution is invalid")
    if observation["failure_class"] not in {None, "design", "infrastructure"}:
        raise SimulationCampaignIntegrityError("observation failure class is invalid")
    if observation["functional"] not in {"pass", "fail", "inconclusive", "not_observed"}:
        raise SimulationCampaignIntegrityError("functional observation is invalid")
    if observation["assertions"] not in {"clean", "dirty", "not_observed"}:
        raise SimulationCampaignIntegrityError("assertion observation is invalid")
    if observation["test"] is not None:
        _bounded_string(
            observation["test"],
            f"observations[{index}].test",
            limit=MAX_OBSERVATION_TEST_BYTES,
        )
    _bounded_canonical_value(
        observation["detail"], f"observations[{index}].detail", MAX_DETAIL_BYTES
    )
    _require_nonnegative_int(observation["assertion_count"], "assertion_count")
    if observation["cycle_count"] is not None:
        _require_nonnegative_int(observation["cycle_count"], "cycle_count")
        if observation["execution"] != "completed":
            raise SimulationCampaignIntegrityError("cycle count requires completed execution")
    _validate_observation_matrix(observation)
    return observation


def _validate_observation_matrix(observation: Mapping[str, object]) -> None:
    if observation["execution"] in {"setup_error", "blocked_by_build"} and (
        observation["failure_class"] not in {"design", "infrastructure"}
        or observation["functional"] != "not_observed"
        or observation["assertions"] != "not_observed"
        or observation["cycle_count"] is not None
    ):
        raise SimulationCampaignIntegrityError("setup/build-block observation matrix is invalid")


def _validate_diagnostics(value: object) -> None:
    diagnostics = _exact_list(value, "diagnostics", MAX_EVIDENCE_REFS)
    for index, item in enumerate(diagnostics):
        diagnostic = _exact_object(
            item, {"severity", "code", "pointer", "message"}, f"diagnostics[{index}]"
        )
        if diagnostic["severity"] not in {"warning", "error"}:
            raise SimulationCampaignIntegrityError("diagnostic severity is invalid")
        _bounded_string(diagnostic["code"], f"diagnostics[{index}].code")
        _bounded_string(diagnostic["pointer"], f"diagnostics[{index}].pointer", allow_empty=True)
        _bounded_string(diagnostic["message"], f"diagnostics[{index}].message")


def _validate_manifest(value: Mapping[str, object]) -> None:
    origin = _exact_object(value["origin"], {"execution_id", "invocation_id"}, "origin")
    execution_id = origin["execution_id"]
    parsed_execution_id = _bounded_string(execution_id, "origin.execution_id", allow_empty=True)
    if parsed_execution_id and not _EXECUTION_ID_RE.fullmatch(parsed_execution_id):
        raise SimulationCampaignIntegrityError(
            "origin.execution_id must be empty or 32 lowercase hex"
        )
    _require_positive_int(origin["invocation_id"], "origin.invocation_id")
    target = _validate_target(value["target"], "target")
    workload = _validate_workload(value["workload"])
    suite = _validate_required_suite(value["required_suite"])
    variants = _validate_variants(value["build_variants"], workload)
    disclosures = _validate_disclosures(value["planning_disclosures"])
    prerequisites = _validate_prerequisites(value["prerequisites"])
    items = _validate_work_items(value["work_items"], target, suite, variants)
    _validate_manifest_fingerprints(
        value, target, workload, suite, variants, disclosures, prerequisites, items
    )


def _validate_target(value: object, field: str) -> Mapping[str, object]:
    target = _exact_object(
        value,
        {"vlnv", "name", "selector", "project_identity", "revision", "role", "display_name"},
        field,
    )
    for key in ("vlnv", "name", "selector", "project_identity", "revision"):
        _bounded_string(target[key], f"{field}.{key}")
    _bounded_string(target["display_name"], f"{field}.display_name", allow_empty=True)
    if target["role"] not in {"candidate", "cycle_count_baseline"}:
        raise SimulationCampaignIntegrityError(f"{field}.role is invalid")
    return target


def _validate_workload(value: object) -> Mapping[str, object]:
    workload = _exact_object(
        value,
        {
            "mode",
            "trace",
            "coverage",
            "eda",
            "planner_contract_version",
            "adapter_contract_version",
            "pre_sim_build_access",
            "run_cwd",
            "runtime_inputs",
            "source_recipe",
            "build_recipe",
        },
        "workload",
    )
    if workload["mode"] != "simulate":
        raise SimulationCampaignIntegrityError("workload mode/trace/coverage is invalid")
    require_bool_value(workload["trace"], field="workload.trace")
    require_bool_value(workload["coverage"], field="workload.coverage")
    _validate_eda(workload["eda"])
    _bounded_string(workload["planner_contract_version"], "planner_contract_version")
    _bounded_string(workload["adapter_contract_version"], "adapter_contract_version")
    _validate_run_cwd(workload["run_cwd"])
    _validate_runtime_declarations(workload["runtime_inputs"])
    _validate_source_recipe(workload["source_recipe"])
    _validate_build_recipe(workload["build_recipe"])
    if workload["pre_sim_build_access"] not in {"immutable", "legacy-per-test"}:
        raise SimulationCampaignIntegrityError("pre_sim_build_access is invalid")
    return workload


def _validate_eda(value: object) -> None:
    eda = _exact_object(value, {"kind", "version"}, "eda")
    for key, item in eda.items():
        _bounded_string(item, f"eda.{key}")


def _validate_run_cwd(value: object) -> None:
    run_cwd = _exact_object(value, {"configured", "kind", "placeholders"}, "run_cwd")
    if run_cwd["kind"] not in {"literal", "templated"}:
        raise SimulationCampaignIntegrityError("run_cwd.kind is invalid")
    placeholders = _bounded_string_list(
        run_cwd["placeholders"],
        "run_cwd.placeholders",
        count_limit=4,
        byte_limit=MAX_STRING_BYTES,
        unique=True,
    )
    if set(placeholders) - {
        "campaign",
        "target",
        "test",
        "attempt",
    }:
        raise SimulationCampaignIntegrityError("run_cwd placeholders are invalid")
    configured = _bounded_string(run_cwd["configured"], "run_cwd.configured", allow_empty=True)
    parsed = _validate_configured_run_kind(configured, cast(str, run_cwd["kind"]))
    if placeholders != parsed:
        raise SimulationCampaignIntegrityError(
            "run_cwd placeholders/kind disagree with configured value"
        )


def _validate_configured_run_kind(configured: str, kind: str) -> list[str]:
    placeholders = _parse_run_placeholders(configured)
    if (kind == "literal") != (not placeholders):
        raise SimulationCampaignIntegrityError(
            "run-directory kind disagrees with configured value"
        )
    return placeholders


def _parse_run_placeholders(configured: str) -> list[str]:
    allowed = {"campaign", "target", "test", "attempt"}
    ordered: list[str] = []
    try:
        parts = Formatter().parse(configured)
        for _, field_name, format_spec, conversion in parts:
            if field_name is None:
                continue
            if field_name not in allowed:
                raise SimulationCampaignIntegrityError(
                    f"unknown run-directory placeholder {field_name!r}"
                )
            if conversion is not None or format_spec:
                raise SimulationCampaignIntegrityError(
                    "run-directory placeholders cannot use conversion or format specs"
                )
            if field_name not in ordered:
                ordered.append(field_name)
    except ValueError as exc:
        raise SimulationCampaignIntegrityError("invalid run-directory format string") from exc
    return ordered


def _validate_runtime_declarations(value: object) -> None:
    declarations = _exact_list(value, "runtime_inputs", MAX_EVIDENCE_REFS)
    ids: list[object] = []
    destinations: list[object] = []
    for index, item in enumerate(declarations):
        declaration = _exact_object(
            item,
            {"declaration_id", "source_artifact_path", "destination"},
            f"runtime_inputs[{index}]",
        )
        source = validate_relative_path(cast(str, declaration["source_artifact_path"]))
        destination = validate_relative_path(cast(str, declaration["destination"]))
        expected = _digest({"source_artifact_path": source, "destination": destination})
        if declaration["declaration_id"] != expected:
            raise SimulationCampaignIntegrityError("runtime declaration digest disagrees")
        ids.append(expected)
        destinations.append(destination)
    if len(set(ids)) != len(ids) or len(set(destinations)) != len(destinations):
        raise SimulationCampaignIntegrityError("runtime declarations must be unique")


def _validate_source_recipe(value: object) -> None:
    recipe = _exact_object(
        value, {"sources", "parameters", "defines", "pre_sim_commands"}, "source_recipe"
    )
    _validate_source_entries(recipe["sources"], "source_recipe.sources")
    parameters = _exact_list(recipe["parameters"], "parameters", 10_000)
    names: list[object] = []
    for index, item in enumerate(parameters):
        parameter = _exact_object(item, {"name", "value"}, f"parameters[{index}]")
        _bounded_string(parameter["name"], f"parameters[{index}].name")
        _validate_bounded_json_strings(parameter["value"], f"parameters[{index}].value")
        names.append(parameter["name"])
    if len(set(names)) != len(names):
        raise SimulationCampaignIntegrityError("parameter names must be unique")
    _bounded_string_list(
        recipe["defines"], "defines", count_limit=10_000, byte_limit=MAX_STRING_BYTES
    )
    _bounded_string_list(
        recipe["pre_sim_commands"],
        "pre_sim_commands",
        count_limit=10_000,
        byte_limit=MAX_COMMAND_BYTES,
    )


def _validate_source_entries(value: object, field: str) -> list[Mapping[str, object]]:
    entries = _exact_list(value, field, MAX_LIST_ITEMS)
    decoded: list[Mapping[str, object]] = []
    for index, item in enumerate(entries):
        entry = _exact_object(item, {"path", "bytes", "sha256", "kind"}, f"{field}[{index}]")
        validate_relative_path(cast(str, entry["path"]))
        _require_nonnegative_int(entry["bytes"], f"{field}.bytes")
        _require_digest(entry["sha256"], f"{field}.sha256")
        if entry["kind"] not in {"rtl", "testbench", "constraint", "user", "generated_input"}:
            raise SimulationCampaignIntegrityError(f"{field}.kind is invalid")
        decoded.append(entry)
    return decoded


def _validate_build_recipe(value: object) -> None:
    recipe = _exact_object(
        value, {"eda_tool", "toplevel", "arguments", "command_model_sha256"}, "build_recipe"
    )
    _bounded_string(recipe["eda_tool"], "build EDA tool")
    _bounded_string(recipe["toplevel"], "build toplevel")
    _bounded_string_list(
        recipe["arguments"],
        "build arguments",
        count_limit=10_000,
        byte_limit=MAX_COMMAND_BYTES,
    )
    _require_digest(recipe["command_model_sha256"], "command_model_sha256")


def _validate_required_suite(value: object) -> Mapping[str, object]:
    suite = _exact_object(
        value,
        {"names", "default_invocation", "source_path", "source_bytes", "source_sha256"},
        "required_suite",
    )
    names = _bounded_string_list(
        suite["names"],
        "required_suite.names",
        count_limit=MAX_OBSERVATIONS,
        byte_limit=MAX_OBSERVATION_TEST_BYTES,
        unique=True,
    )
    require_bool_value(suite["default_invocation"], field="default_invocation")
    _require_nonnegative_int(suite["source_bytes"], "required_suite.source_bytes")
    _require_digest(suite["source_sha256"], "required_suite.source_sha256")
    source_path = _bounded_string(
        suite["source_path"], "required_suite.source_path", allow_empty=True
    )
    if source_path:
        validate_relative_path(source_path)
    elif not suite["default_invocation"]:
        raise SimulationCampaignIntegrityError("catalog-backed required suite needs a source path")
    if suite["default_invocation"] and names:
        raise SimulationCampaignIntegrityError("default required suite cannot name tests")
    if suite["default_invocation"] and (
        suite["source_path"] != ""
        or suite["source_bytes"] != 0
        or suite["source_sha256"] != _digest_bytes(b"")
    ):
        raise SimulationCampaignIntegrityError(
            "default required suite must authenticate empty source"
        )
    return suite


def _validate_variants(
    value: object, workload: Mapping[str, object]
) -> list[Mapping[str, object]]:
    variants = _exact_list(value, "build_variants", MAX_WORK_ITEMS)
    decoded: list[Mapping[str, object]] = []
    for index, item in enumerate(variants):
        variant = _exact_object(
            item,
            {"build_variant_id", "kind", "sharing_eligible", "source_closure", "recipe_sha256"},
            f"build_variants[{index}]",
        )
        _validate_variant(variant, workload)
        decoded.append(variant)
    ids = [item["build_variant_id"] for item in decoded]
    if len(set(ids)) != len(ids):
        raise SimulationCampaignIntegrityError("build variant IDs must be unique")
    return decoded


def _validate_variant(variant: Mapping[str, object], workload: Mapping[str, object]) -> None:
    if variant["kind"] not in {"candidate", "trace", "coverage"}:
        raise SimulationCampaignIntegrityError("build variant kind is invalid")
    require_bool_value(variant["sharing_eligible"], field="sharing_eligible")
    _validate_source_entries(variant["source_closure"], "source_closure")
    recipe = {
        "kind": variant["kind"],
        "source_closure": variant["source_closure"],
        "source_recipe": workload["source_recipe"],
        "build_recipe": workload["build_recipe"],
        "eda": workload["eda"],
        "trace": workload["trace"],
        "coverage": workload["coverage"],
    }
    expected = _digest(recipe)
    if variant["recipe_sha256"] != expected or variant["build_variant_id"] != (
        f"variant:{expected.removeprefix('sha256:')}"
    ):
        raise SimulationCampaignIntegrityError("build variant recipe digest/ID disagrees")


def _validate_disclosures(value: object) -> list[Mapping[str, object]]:
    disclosures = _exact_list(value, "planning_disclosures", MAX_WORK_ITEMS)
    decoded: list[Mapping[str, object]] = []
    for index, item in enumerate(disclosures):
        disclosure = _exact_object(
            item,
            {"planner", "scratch_inputs", "generated_files", "tool_provenance", "cleanup"},
            f"planning_disclosures[{index}]",
        )
        _bounded_string(disclosure["planner"], f"planning_disclosures[{index}].planner")
        _validate_source_entries(disclosure["scratch_inputs"], "scratch_inputs")
        _validate_source_entries(disclosure["generated_files"], "generated_files")
        provenance = _exact_object(
            disclosure["tool_provenance"],
            {"kind", "version", "contract_version"},
            "planning tool_provenance",
        )
        for key, entry in provenance.items():
            _bounded_string(entry, f"planning tool_provenance.{key}")
        cleanup = _exact_object(disclosure["cleanup"], {"removed"}, "cleanup")
        if require_bool_value(cleanup["removed"], field="cleanup.removed") is not True:
            raise SimulationCampaignIntegrityError("planning scratch must be removed")
        decoded.append(disclosure)
    return decoded


def _validate_prerequisites(value: object) -> list[Mapping[str, object]]:
    prerequisites = _exact_list(value, "prerequisites", MAX_WORK_ITEMS)
    decoded: list[Mapping[str, object]] = []
    identities: list[tuple[object, object, object]] = []
    for index, item in enumerate(prerequisites):
        prerequisite = _exact_object(
            item,
            {"role", "manifest", "campaign_id", "target", "required_observation", "work_item_id"},
            f"prerequisites[{index}]",
        )
        if (
            prerequisite["role"] != "cycle_count_baseline"
            or prerequisite["required_observation"] != "cycle_count"
        ):
            raise SimulationCampaignIntegrityError("prerequisite role/observation is invalid")
        _require_uuid(prerequisite["campaign_id"], "prerequisite campaign_id")
        target = _validate_target(prerequisite["target"], "prerequisite.target")
        if target["role"] != "cycle_count_baseline":
            raise SimulationCampaignIntegrityError("prerequisite target must have baseline role")
        _require_work_item_id(prerequisite["work_item_id"])
        manifest = _exact_object(
            prerequisite["manifest"],
            {"path_base", "path", "bytes", "sha256", "kind", "owner"},
            "prerequisite.manifest",
        )
        if (
            manifest["path_base"] != "origin_invocation"
            or manifest["kind"] != "simulation_campaign_manifest"
        ):
            raise SimulationCampaignIntegrityError("prerequisite manifest reference is invalid")
        _validate_evidence_members(manifest, "prerequisite.manifest")
        if not cast(str, manifest["path"]).startswith("targets/"):
            raise SimulationCampaignIntegrityError(
                "prerequisite manifest must start beneath targets/"
            )
        if manifest["owner"] != prerequisite["campaign_id"]:
            raise SimulationCampaignIntegrityError("prerequisite manifest owner disagrees")
        identities.append(
            (prerequisite["campaign_id"], target["project_identity"], target["revision"])
        )
        decoded.append(prerequisite)
    if len(set(identities)) != len(identities):
        raise SimulationCampaignIntegrityError("prerequisite identities must be unique")
    return decoded


def _validate_work_items(
    value: object,
    target: Mapping[str, object],
    suite: Mapping[str, object],
    variants: list[Mapping[str, object]],
) -> list[Mapping[str, object]]:
    items = _exact_list(value, "work_items", MAX_WORK_ITEMS)
    variant_ids = {variant["build_variant_id"] for variant in variants}
    decoded_items: list[Mapping[str, object]] = []
    for ordinal, item in enumerate(items):
        decoded = _decode_work_item(item, ordinal)
        if decoded["target"] != target or decoded["build_variant_id"] not in variant_ids:
            raise SimulationCampaignIntegrityError("work item target/build variant disagrees")
        if decoded["role"] != target["role"] or decoded["revision"] != target["revision"]:
            raise SimulationCampaignIntegrityError("work item role/revision disagrees with target")
        selection = cast(Mapping[str, object], decoded["selection"])
        if selection["kind"] == "default" and not suite["default_invocation"]:
            raise SimulationCampaignIntegrityError("default work item requires an unnamed target")
        identity_input = {
            key: entry
            for key, entry in decoded.items()
            if key not in {"fingerprint_sha256", "work_item_id"}
        }
        expected = _digest(identity_input)
        expected_id = f"item:{ordinal:04d}:{expected.removeprefix('sha256:')[:16]}"
        if decoded["fingerprint_sha256"] != expected or decoded["work_item_id"] != expected_id:
            raise SimulationCampaignIntegrityError("work item fingerprint/ID disagrees")
        decoded_items.append(decoded)
    observation_count = sum(
        len(cast(Mapping[str, object], item["selection"])["names"])
        or (1 if cast(Mapping[str, object], item["selection"])["kind"] == "default" else 0)
        for item in decoded_items
    )
    if observation_count > MAX_OBSERVATIONS:
        raise SimulationCampaignIntegrityError("planned observations exceed campaign ceiling")
    return decoded_items


def _decode_work_item(value: object, ordinal: int) -> Mapping[str, object]:
    item = _exact_object(
        value,
        {
            "work_item_id",
            "ordinal",
            "kind",
            "role",
            "revision",
            "target",
            "selection",
            "arguments",
            "build_variant_id",
            "run_directory",
            "fingerprint_sha256",
        },
        f"work_items[{ordinal}]",
    )
    if require_int(item["ordinal"], field="work item ordinal") != ordinal:
        raise SimulationCampaignIntegrityError("work item ordinal disagrees with order")
    if item["kind"] not in {"ordinary_hdl", "cocotb_batch", "coverage_aggregate"}:
        raise SimulationCampaignIntegrityError("work item kind is invalid")
    if item["role"] not in {"candidate", "cycle_count_baseline"}:
        raise SimulationCampaignIntegrityError("work item role is invalid")
    _bounded_string(item["revision"], "work item revision")
    _validate_work_item_selection(item["selection"], cast(str, item["kind"]))
    _validate_work_item_run_directory(item["run_directory"])
    _bounded_string_list(
        item["arguments"], "arguments", count_limit=10_000, byte_limit=MAX_COMMAND_BYTES
    )
    return item


def _validate_work_item_selection(value: object, work_item_kind: str) -> None:
    selection = _exact_object(value, {"kind", "names"}, "selection")
    names = _bounded_string_list(
        selection["names"],
        "selection.names",
        count_limit=MAX_OBSERVATIONS,
        byte_limit=MAX_OBSERVATION_TEST_BYTES,
        unique=True,
    )
    if selection["kind"] == "named" and not names:
        raise SimulationCampaignIntegrityError("named selection must be nonempty")
    if selection["kind"] in {"default", "unfiltered"} and names:
        raise SimulationCampaignIntegrityError("unnamed selection must have no names")
    if selection["kind"] not in {"named", "default", "unfiltered"}:
        raise SimulationCampaignIntegrityError("selection kind is invalid")
    if work_item_kind == "ordinary_hdl" and selection["kind"] == "named" and len(names) != 1:
        raise SimulationCampaignIntegrityError("named ordinary work item requires one test")
    if work_item_kind == "ordinary_hdl" and selection["kind"] == "unfiltered":
        raise SimulationCampaignIntegrityError("ordinary work item cannot be unfiltered")
    if work_item_kind == "cocotb_batch" and selection["kind"] == "default":
        raise SimulationCampaignIntegrityError("Cocotb work item cannot use default selection")
    if work_item_kind == "coverage_aggregate" and selection["kind"] != "named":
        raise SimulationCampaignIntegrityError("coverage work item requires named selection")


def _validate_work_item_run_directory(value: object) -> None:
    run_directory = _exact_object(
        value, {"configured", "kind", "collision_template"}, "run_directory"
    )
    if run_directory["kind"] not in {"literal", "templated"}:
        raise SimulationCampaignIntegrityError("work item run-directory kind is invalid")
    configured = _bounded_string(
        run_directory["configured"], "work item run_directory.configured", allow_empty=True
    )
    _validate_configured_run_kind(configured, cast(str, run_directory["kind"]))
    _validate_run_directory_path(
        run_directory["collision_template"],
        "work item run_directory.collision_template",
    )


def _validate_run_directory_path(value: object, field: str) -> str:
    """Validate an absolute or Project-relative run cwd/collision identity."""
    text = _bounded_string(value, field)
    path = Path(text)
    if "\0" in text or any(part == ".." for part in path.parts):
        raise SimulationCampaignIntegrityError(f"{field} is invalid")
    return text


def _validate_manifest_fingerprints(
    manifest: Mapping[str, object],
    target: Mapping[str, object],
    workload: Mapping[str, object],
    suite: Mapping[str, object],
    variants: list[Mapping[str, object]],
    disclosures: list[Mapping[str, object]],
    prerequisites: list[Mapping[str, object]],
    items: list[Mapping[str, object]],
) -> None:
    fingerprints = _exact_object(
        manifest["fingerprints"],
        {
            "workload_sha256",
            "target_recipe_sha256",
            "source_closures_sha256",
            "required_suite_sha256",
            "planning_disclosures_sha256",
            "prerequisites_sha256",
            "work_items_sha256",
        },
        "fingerprints",
    )
    expected = _manifest_component_digests(
        manifest, target, workload, suite, variants, disclosures, prerequisites, items
    )
    for key, digest in expected.items():
        if fingerprints[key] != digest:
            raise SimulationCampaignIntegrityError(f"fingerprints.{key} disagrees")


def _manifest_component_digests(
    manifest: Mapping[str, object],
    target: Mapping[str, object],
    workload: Mapping[str, object],
    suite: Mapping[str, object],
    variants: list[Mapping[str, object]],
    disclosures: list[Mapping[str, object]],
    prerequisites: list[Mapping[str, object]],
    items: list[Mapping[str, object]],
) -> dict[str, str]:
    expected = {
        "target_recipe_sha256": _digest(
            {
                "target": target,
                "source_recipe": workload["source_recipe"],
                "build_recipe": workload["build_recipe"],
            }
        ),
        "source_closures_sha256": _digest(
            [
                {
                    "build_variant_id": item["build_variant_id"],
                    "source_closure": item["source_closure"],
                }
                for item in variants
            ]
        ),
        "required_suite_sha256": _digest(suite),
        "planning_disclosures_sha256": _digest(disclosures),
        "prerequisites_sha256": _digest(prerequisites),
        "work_items_sha256": _digest(items),
    }
    virtual = {
        key: manifest[key]
        for key in (
            "target",
            "workload",
            "required_suite",
            "build_variants",
            "planning_disclosures",
            "prerequisites",
            "work_items",
        )
    }
    expected["workload_sha256"] = _digest(virtual)
    return expected


def _digest(value: object) -> str:
    return _digest_bytes(canonical_json_bytes(value)[:-1])


def _digest_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _validate_evidence_ref(value: object, field: str) -> Mapping[str, object]:
    required = {"path", "bytes", "sha256", "kind", "owner"}
    reference = _exact_object(value, required, field)
    _validate_evidence_members(reference, field)
    return reference


def _validate_evidence_members(reference: Mapping[str, object], field: str) -> None:
    validate_relative_path(_bounded_string(reference["path"], f"{field}.path"))
    _require_nonnegative_int(reference["bytes"], f"{field}.bytes")
    _require_digest(reference["sha256"], f"{field}.sha256")
    _bounded_string(reference["kind"], f"{field}.kind")
    _require_uuid(reference["owner"], f"{field}.owner")


def _validate_evidence_list(value: object, field: str) -> None:
    references = _exact_list(value, field, MAX_EVIDENCE_REFS)
    for index, reference in enumerate(references):
        _validate_evidence_ref(reference, f"{field}[{index}]")


_DOCUMENT_SPECS: dict[
    type[SimulationCampaignDocument],
    tuple[str, frozenset[str], int, Callable[[Mapping[str, object]], None]],
] = {
    SimulationCampaignManifest: (
        "booley.simulation-campaign-manifest/v1",
        _MANIFEST_FIELDS,
        MANIFEST_MAX_BYTES,
        _validate_manifest,
    ),
    SimulationAttempt: (
        "booley.simulation-attempt/v1",
        _ATTEMPT_FIELDS,
        RECORD_MAX_BYTES,
        _validate_simulation_attempt,
    ),
    BundleBuildAttempt: (
        "booley.bundle-build-attempt/v1",
        _BUILD_ATTEMPT_FIELDS,
        RECORD_MAX_BYTES,
        _validate_build_attempt,
    ),
    BundleBuildResult: (
        "booley.bundle-build-result/v1",
        _BUILD_RESULT_FIELDS,
        RECORD_MAX_BYTES,
        _validate_build_result,
    ),
    SimulationResult: (
        "booley.simulation-result/v1",
        _RESULT_FIELDS,
        RECORD_MAX_BYTES,
        _validate_simulation_result,
    ),
    SimulatorBundle: (
        "booley.simulator-bundle/v1",
        _BUNDLE_FIELDS,
        MANIFEST_MAX_BYTES,
        _validate_bundle_document,
    ),
    ExecutableSnapshot: (
        "booley.executable-snapshot/v1",
        _SNAPSHOT_FIELDS,
        MANIFEST_MAX_BYTES,
        _validate_snapshot_document,
    ),
}
_DOCUMENT_VALIDATORS = {spec[0]: spec[3] for spec in _DOCUMENT_SPECS.values()}


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


decode_build_result = decode_bundle_build_result


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
    _decode_registered(raw, expected_type)
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
    """Validate and canonically encode any Phase-1 campaign document value."""
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

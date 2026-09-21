"""Strict canonical JSON codecs for immutable Simulation Campaign values.

This module is deliberately infrastructure-free. It validates untrusted
documents into immutable values that later store/execution modules can trust.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from collections.abc import Mapping
from types import MappingProxyType
from typing import TypeAlias, TypeVar, cast

from booley.runtime.timefmt import parse_timestamp

from .model import (
    AssertionObservation,
    BundleBuildAttempt,
    BundleBuildResult,
    CampaignDocument,
    ExecutableSnapshot,
    ExecutionObservation,
    FailureClass,
    FunctionalObservation,
    SimulationAttempt,
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
MAX_JSON_DEPTH = 64
MAX_LIST_ITEMS = 100_000

_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_EXECUTION_ID_RE = re.compile(r"[0-9a-f]{32}\Z")
_VARIANT_ID_RE = re.compile(r"variant:[0-9a-f]{64}\Z")
_WORK_ITEM_ID_RE = re.compile(r"item:[0-9]{4}:[0-9a-f]{16}\Z")
_WINDOWS_DRIVE_RE = re.compile(r"[A-Za-z]:")


class CampaignIntegrityError(ValueError):
    """An authoritative campaign value violates its exact contract."""


def _freeze(value: object) -> FrozenJson:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, list | tuple):
        return tuple(_freeze(item) for item in value)
    if value is None or isinstance(value, str | int | float | bool):
        return value
    raise CampaignIntegrityError(f"non-JSON value {type(value).__name__}")


def _thaw(value: FrozenJson) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
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
        raise CampaignIntegrityError(f"value is not canonical JSON: {exc}") from exc
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

T = TypeVar("T", bound=CampaignDocument)


def _decode(
    raw: bytes,
    *,
    schema: str,
    fields: frozenset[str],
    limit: int,
    value_type: type[T],
) -> T:
    if len(raw) > limit:
        raise CampaignIntegrityError(f"document exceeds {limit}-byte size ceiling")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise CampaignIntegrityError(f"document is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise CampaignIntegrityError("document must be an object")
    if set(value) != fields:
        missing = sorted(fields - set(value))
        unknown = sorted(set(value) - fields)
        raise CampaignIntegrityError(
            f"document must have exact fields; missing={missing}, unknown={unknown}"
        )
    if value.get("$schema") != schema:
        raise CampaignIntegrityError(f"unsupported $schema {value.get('$schema')!r}")
    _validate_json(value)
    _validate_common(value)
    if canonical_json_bytes(value) != raw:
        raise CampaignIntegrityError("document is not canonical JSON")
    _validate_document(schema, value)
    return value_type(cast(Mapping[str, FrozenJson], _freeze(value)))


def _validate_json(value: object, *, depth: int = 0) -> None:
    if depth > MAX_JSON_DEPTH:
        raise CampaignIntegrityError("JSON nesting exceeds resource ceiling")
    if isinstance(value, str):
        if len(value.encode("utf-8")) > MAX_COMMAND_BYTES:
            raise CampaignIntegrityError("string exceeds resource ceiling")
        return
    if isinstance(value, bool | int) or value is None:
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CampaignIntegrityError("numbers must be finite")
        return
    if isinstance(value, list):
        if len(value) > MAX_LIST_ITEMS:
            raise CampaignIntegrityError("list exceeds resource ceiling")
        for item in value:
            _validate_json(item, depth=depth + 1)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise CampaignIntegrityError("object keys must be strings")
            _validate_json(item, depth=depth + 1)
        return
    raise CampaignIntegrityError(f"unsupported JSON type {type(value).__name__}")


def _require_positive_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise CampaignIntegrityError(f"{field} must be a positive integer")
    return value


def _require_nonnegative_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CampaignIntegrityError(f"{field} must be a nonnegative integer")
    return value


def _require_digest(value: object, field: str) -> str:
    if not isinstance(value, str) or not _DIGEST_RE.fullmatch(value):
        raise CampaignIntegrityError(f"{field} must be a sha256 digest")
    return value


def _require_uuid(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise CampaignIntegrityError(f"{field} must be lowercase UUIDv4")
    try:
        parsed = uuid.UUID(value)
    except ValueError as exc:
        raise CampaignIntegrityError(f"{field} must be lowercase UUIDv4") from exc
    if parsed.version != 4 or str(parsed) != value:
        raise CampaignIntegrityError(f"{field} must be lowercase UUIDv4")
    return value


def _require_variant_id(value: object) -> str:
    if not isinstance(value, str) or not _VARIANT_ID_RE.fullmatch(value):
        raise CampaignIntegrityError("build_variant_id is invalid")
    return value


def _require_work_item_id(value: object) -> str:
    if not isinstance(value, str) or not _WORK_ITEM_ID_RE.fullmatch(value):
        raise CampaignIntegrityError("work_item_id is invalid")
    return value


def _require_nonnegative_number(value: object, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise CampaignIntegrityError(f"{field} must be a finite nonnegative number")
    if not math.isfinite(value) or value < 0:
        raise CampaignIntegrityError(f"{field} must be a finite nonnegative number")


def _validate_common(value: Mapping[str, object]) -> None:
    campaign_id = value.get("campaign_id")
    if campaign_id is not None:
        try:
            parsed = uuid.UUID(str(campaign_id))
        except ValueError as exc:
            raise CampaignIntegrityError("campaign_id must be UUIDv4") from exc
        if parsed.version != 4 or str(parsed) != campaign_id:
            raise CampaignIntegrityError("campaign_id must be lowercase UUIDv4")
    for key in ("manifest_sha256", "workload_sha256"):
        digest = value.get(key)
        if digest is not None and (
            not isinstance(digest, str) or not _DIGEST_RE.fullmatch(digest)
        ):
            raise CampaignIntegrityError(f"{key} must be a sha256 digest")
    for key in ("created_at", "started_at", "finished_at"):
        timestamp = value.get(key)
        if timestamp is not None:
            try:
                parsed_timestamp = parse_timestamp(cast(str, timestamp))
            except (TypeError, ValueError) as exc:
                raise CampaignIntegrityError(f"{key} must be canonical UTC RFC3339") from exc
            if "." in cast(str, timestamp) or not cast(str, timestamp).endswith("Z"):
                raise CampaignIntegrityError(f"{key} must be canonical UTC RFC3339")
            del parsed_timestamp


def _validate_document(schema: str, value: Mapping[str, object]) -> None:
    _validate_evidence_refs(value)
    validators = {
        "booley.simulation-campaign-manifest/v1": _validate_manifest,
        "booley.simulation-attempt/v1": _validate_simulation_attempt,
        "booley.bundle-build-attempt/v1": _validate_build_attempt,
        "booley.bundle-build-result/v1": _validate_build_result,
        "booley.simulation-result/v1": _validate_simulation_result,
        "booley.simulator-bundle/v1": _validate_bundle_document,
        "booley.executable-snapshot/v1": _validate_snapshot_document,
    }
    validators[schema](value)


def _validate_attempt_identity(value: Mapping[str, object], ordinal_key: str) -> None:
    _require_positive_int(value[ordinal_key], ordinal_key)
    _require_positive_int(value["producer_invocation_id"], "producer_invocation_id")


def _validate_simulation_attempt(value: Mapping[str, object]) -> None:
    _validate_attempt_identity(value, "attempt_ordinal")
    _require_uuid(value["attempt_id"], "attempt_id")
    if value["pre_sim_build_access"] not in {"immutable", "legacy-per-test"}:
        raise CampaignIntegrityError("attempt pre_sim_build_access is invalid")
    _require_variant_id(value["build_variant_id"])
    _require_work_item_id(value["work_item_id"])
    run_directory = _exact_object(
        value["run_directory"],
        {"kind", "configured", "resolved", "collision_key", "owned"},
        "run_directory",
    )
    if run_directory["kind"] not in {"literal", "templated"}:
        raise CampaignIntegrityError("invalid run directory kind")
    if not isinstance(run_directory["owned"], bool):
        raise CampaignIntegrityError("run_directory.owned must be boolean")
    policy = _exact_object(value["policy"], {"timeout_seconds", "no_kill", "diagnostic"}, "policy")
    if policy["timeout_seconds"] is not None:
        _require_positive_int(policy["timeout_seconds"], "policy.timeout_seconds")
    if not isinstance(policy["no_kill"], bool) or not isinstance(policy["diagnostic"], bool):
        raise CampaignIntegrityError("attempt policy flags must be boolean")
    _validate_child_identity(value)


def _validate_child_identity(value: Mapping[str, object]) -> None:
    child_id, child_digest = value["child_execution_id"], value["child_entry_sha256"]
    if (child_id is None) != (child_digest is None):
        raise CampaignIntegrityError("child execution fields must both be null or present")
    if child_id is not None and (
        not isinstance(child_id, str) or not _EXECUTION_ID_RE.fullmatch(child_id)
    ):
        raise CampaignIntegrityError("child_execution_id must be 32 lowercase hex")
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
    if any(not isinstance(item, str) or not item for item in provenance.values()):
        raise CampaignIntegrityError("tool provenance strings must be nonempty")
    sharing = value["sharing"]
    if sharing not in {"shared_variant", "private_work_item"}:
        raise CampaignIntegrityError("invalid build sharing scope")
    expected_private = all(item is not None for item in owner.values())
    if (sharing == "private_work_item") != expected_private:
        raise CampaignIntegrityError("build owner disagrees with sharing scope")
    if expected_private:
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
        raise CampaignIntegrityError("invalid build result state")
    phases, has_bundle, has_observation = states[cast(str, value["state"])]
    if value["phase"] not in phases:
        raise CampaignIntegrityError("build result phase disagrees with state")
    if (value["bundle"] is not None) != has_bundle or (
        value["observation"] is not None
    ) != has_observation:
        raise CampaignIntegrityError("build result nullability disagrees with state")
    _validate_bundle(value["bundle"])
    _validate_build_observation(value["observation"])
    if value["observation"] is not None:
        observed_class = cast(Mapping[str, object], value["observation"])["class"]
        if (value["state"] == "design_failure") != (observed_class == "design"):
            raise CampaignIntegrityError("build observation class disagrees with state")
    _exact_list(value["evidence"], "evidence", MAX_EVIDENCE_REFS)


def _validate_simulation_result(value: Mapping[str, object]) -> None:
    _require_nonnegative_number(value["elapsed_seconds"], "elapsed_seconds")
    _validate_attempt_identity(value, "attempt_ordinal")
    _require_uuid(value["attempt_id"], "attempt_id")
    _require_work_item_id(value["work_item_id"])
    build_result = _validate_simulation_build_ref(value["build_result"])
    state = value["state"]
    if state not in {"completed", "timeout", "crash", "setup_error", "blocked_by_build"}:
        raise CampaignIntegrityError("invalid simulation result state")
    if value["grade"] not in {grade.value for grade in StrictGrade}:
        raise CampaignIntegrityError("invalid strict grade")
    _validate_result_state(value, build_result)
    _validate_runtime_inputs(value["runtime_inputs"], blocked=state == "blocked_by_build")
    observations = _validate_observations(value["observations"], cast(str, state))
    _validate_result_grade(value["grade"], observations)
    _validate_diagnostics(value["diagnostics"])
    _exact_list(value["evidence"], "evidence", MAX_EVIDENCE_REFS)


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
    if any(not isinstance(item, str) or not item for item in provenance.values()):
        raise CampaignIntegrityError("bundle tool provenance must be nonempty")
    artifacts = _exact_list(value["artifacts"], "artifacts", MAX_EVIDENCE_REFS)
    decoded = [_validate_bundle_artifact(item, index) for index, item in enumerate(artifacts)]
    if len({item["path"] for item in decoded}) != len(decoded):
        raise CampaignIntegrityError("bundle artifact paths must be unique")
    if value["inventory_sha256"] != _digest(artifacts):
        raise CampaignIntegrityError("bundle inventory digest disagrees")
    snapshot = [
        item for item in artifacts if cast(Mapping[str, object], item)["kind"] != "runtime_input"
    ]
    if value["snapshot_inventory_sha256"] != _digest(snapshot):
        raise CampaignIntegrityError("bundle snapshot inventory digest disagrees")
    private = value["sharing"] == "private_work_item"
    if value["sharing"] not in {"shared_variant", "private_work_item"} or private != all(
        item is not None for item in owner.values()
    ):
        raise CampaignIntegrityError("bundle sharing/owner disagrees")


def _validate_snapshot_document(value: Mapping[str, object]) -> None:
    _require_uuid(value["attempt_id"], "attempt_id")
    _require_uuid(value["bundle_id"], "bundle_id")
    _require_work_item_id(value["work_item_id"])
    _validate_simulation_build_ref(value["build_result"])
    artifacts = _exact_list(value["artifacts"], "artifacts", MAX_EVIDENCE_REFS)
    decoded = [_validate_bundle_artifact(item, index) for index, item in enumerate(artifacts)]
    if any(item["kind"] == "runtime_input" for item in decoded):
        raise CampaignIntegrityError("snapshot cannot contain runtime inputs")
    if len({item["path"] for item in decoded}) != len(decoded):
        raise CampaignIntegrityError("snapshot artifact paths must be unique")
    if value["inventory_sha256"] != _digest(artifacts):
        raise CampaignIntegrityError("snapshot inventory digest disagrees")


def _exact_object(value: object, fields: set[str], field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise CampaignIntegrityError(f"{field} must have exact fields {sorted(fields)}")
    return cast(Mapping[str, object], value)


def _exact_list(value: object, field: str, ceiling: int) -> list[object]:
    if not isinstance(value, list) or len(value) > ceiling:
        raise CampaignIntegrityError(f"{field} must be a list capped at {ceiling}")
    return value


def _validate_build_result_ref(value: object) -> Mapping[str, object]:
    reference = _exact_object(
        value,
        {"path", "bytes", "sha256", "kind", "owner", "build_attempt_id"},
        "build_attempt",
    )
    if reference["kind"] != "bundle_build_attempt":
        raise CampaignIntegrityError("build_attempt reference has wrong kind")
    _require_uuid(reference["build_attempt_id"], "build_attempt_id")
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
        raise CampaignIntegrityError("build_result reference has wrong kind")
    if reference["state"] not in {"ready", "design_failure"}:
        raise CampaignIntegrityError("simulation result references invalid build state")
    if reference["sharing"] not in {"shared_variant", "private_work_item"}:
        raise CampaignIntegrityError("simulation result references invalid sharing scope")
    _require_uuid(reference["build_attempt_id"], "build_attempt_id")
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
        raise CampaignIntegrityError("bundle sharing scope is invalid")
    artifacts = _exact_list(bundle["artifacts"], "bundle.artifacts", MAX_EVIDENCE_REFS)
    decoded = [_validate_bundle_artifact(item, index) for index, item in enumerate(artifacts)]
    paths = [item["path"] for item in decoded]
    if len(set(paths)) != len(paths):
        raise CampaignIntegrityError("bundle artifact paths must be unique")
    if not any(item["kind"] == "simulator_executable" for item in decoded):
        raise CampaignIntegrityError("ready bundle requires a simulator executable")


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
        raise CampaignIntegrityError("bundle artifact kind is invalid")
    return artifact


def _validate_build_observation(value: object) -> None:
    if value is None:
        return
    observation = _exact_object(value, {"class", "code", "message", "detail"}, "build observation")
    if observation["class"] not in {"design", "infrastructure"}:
        raise CampaignIntegrityError("build observation class is invalid")
    for key in ("code", "message"):
        if not isinstance(observation[key], str) or not observation[key]:
            raise CampaignIntegrityError(f"build observation {key} must be nonempty")


def _validate_result_state(
    value: Mapping[str, object], build_result: Mapping[str, object]
) -> None:
    state = value["state"]
    if state == "blocked_by_build":
        if build_result["state"] != "design_failure":
            raise CampaignIntegrityError("blocked result requires design-failure build")
        if value["bundle_id"] is not None or value["executable_snapshot"] is not None:
            raise CampaignIntegrityError("blocked result cannot bind a bundle or snapshot")
        return
    if build_result["state"] != "ready":
        raise CampaignIntegrityError("non-blocked result requires ready build")
    _require_uuid(value["bundle_id"], "bundle_id")
    if state in {"completed", "timeout", "crash"} and value["executable_snapshot"] is None:
        raise CampaignIntegrityError("post-launch result requires executable snapshot")
    _validate_executable_snapshot(value["executable_snapshot"])


def _validate_executable_snapshot(value: object) -> None:
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
    _validate_evidence_ref(snapshot["manifest"], "executable_snapshot.manifest")
    for key in ("bundle_manifest_sha256", "pre_launch_sha256", "post_exit_sha256"):
        _require_digest(snapshot[key], f"executable_snapshot.{key}")
    if snapshot["pre_launch_sha256"] != snapshot["post_exit_sha256"]:
        raise CampaignIntegrityError("executable snapshot changed during execution")
    if snapshot["verified_after_exit"] is not True:
        raise CampaignIntegrityError("executable snapshot must be verified after exit")


def _validate_runtime_inputs(value: object, *, blocked: bool) -> None:
    inputs = _exact_list(value, "runtime_inputs", MAX_EVIDENCE_REFS)
    if blocked and inputs:
        raise CampaignIntegrityError("blocked result runtime_inputs must be empty")
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
            raise CampaignIntegrityError("runtime input method is invalid")
        _require_nonnegative_int(binding["destination_bytes"], "destination_bytes")
        _require_digest(binding["destination_sha256"], "destination_sha256")
        authoritative = cast(Mapping[str, object], binding["authoritative_copy"])
        if (
            authoritative["bytes"] != binding["destination_bytes"]
            or authoritative["sha256"] != binding["destination_sha256"]
        ):
            raise CampaignIntegrityError("runtime input copy and destination disagree")
        if not isinstance(binding["owned"], bool):
            raise CampaignIntegrityError("runtime input owned must be boolean")
        destinations.append(binding["destination"])
        declarations.append(binding["declaration_id"])
    if len(set(destinations)) != len(destinations) or len(set(declarations)) != len(declarations):
        raise CampaignIntegrityError("runtime input identities must be unique")


def _validate_observations(value: object, state: str) -> list[Mapping[str, object]]:
    observations = _exact_list(value, "observations", MAX_OBSERVATIONS)
    if not observations:
        raise CampaignIntegrityError("result observations must be nonempty")
    tests: list[object] = []
    decoded: list[Mapping[str, object]] = []
    for index, item in enumerate(observations):
        observation = _validate_observation(item, index)
        tests.append(observation["test"])
        decoded.append(observation)
        if state == "completed" and observation["execution"] != "completed":
            raise CampaignIntegrityError("completed result requires completed observations")
        if state == "blocked_by_build" and observation["execution"] != "blocked_by_build":
            raise CampaignIntegrityError("blocked result requires blocked observations")
        if state == "setup_error" and observation["execution"] != "setup_error":
            raise CampaignIntegrityError("setup result requires setup observations")
    named = [test for test in tests if test is not None]
    if len(set(named)) != len(named):
        raise CampaignIntegrityError("observation test names must be unique")
    if state in {"completed", "timeout", "crash"}:
        strength = {"completed": 0, "timeout": 1, "crash": 2}
        expected = max(
            (cast(str, item["execution"]) for item in decoded),
            key=lambda execution: strength.get(execution, 3),
        )
        if expected != state:
            raise CampaignIntegrityError("result state disagrees with observations")
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
        raise CampaignIntegrityError("result grade disagrees with observations")


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
        raise CampaignIntegrityError("observation execution is invalid")
    if observation["failure_class"] not in {None, "design", "infrastructure"}:
        raise CampaignIntegrityError("observation failure class is invalid")
    if observation["functional"] not in {"pass", "fail", "inconclusive", "not_observed"}:
        raise CampaignIntegrityError("functional observation is invalid")
    if observation["assertions"] not in {"clean", "dirty", "not_observed"}:
        raise CampaignIntegrityError("assertion observation is invalid")
    _require_nonnegative_int(observation["assertion_count"], "assertion_count")
    if observation["cycle_count"] is not None:
        _require_nonnegative_int(observation["cycle_count"], "cycle_count")
        if observation["execution"] != "completed":
            raise CampaignIntegrityError("cycle count requires completed execution")
    if observation["execution"] in {"setup_error", "blocked_by_build"} and (
        observation["failure_class"] != "design"
        or observation["functional"] != "not_observed"
        or observation["assertions"] != "not_observed"
        or observation["cycle_count"] is not None
    ):
        raise CampaignIntegrityError("setup/build-block observation matrix is invalid")
    return observation


def _validate_diagnostics(value: object) -> None:
    diagnostics = _exact_list(value, "diagnostics", MAX_EVIDENCE_REFS)
    for index, item in enumerate(diagnostics):
        diagnostic = _exact_object(
            item, {"severity", "code", "pointer", "message"}, f"diagnostics[{index}]"
        )
        if diagnostic["severity"] not in {"warning", "error"}:
            raise CampaignIntegrityError("diagnostic severity is invalid")


def _validate_manifest(value: Mapping[str, object]) -> None:
    origin = _exact_object(value["origin"], {"execution_id", "invocation_id"}, "origin")
    execution_id = origin["execution_id"]
    if execution_id != "" and (
        not isinstance(execution_id, str) or not _EXECUTION_ID_RE.fullmatch(execution_id)
    ):
        raise CampaignIntegrityError("origin.execution_id must be empty or 32 lowercase hex")
    _require_positive_int(origin["invocation_id"], "origin.invocation_id")
    target = _validate_target(value["target"], "target")
    workload = _validate_workload(value["workload"])
    suite = _validate_required_suite(value["required_suite"])
    variants = _validate_variants(value["build_variants"], workload)
    disclosures = _validate_disclosures(value["planning_disclosures"])
    prerequisites = _validate_prerequisites(value["prerequisites"])
    items = _validate_work_items(value["work_items"], target, variants)
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
        if not isinstance(target[key], str) or not target[key]:
            raise CampaignIntegrityError(f"{field}.{key} must be nonempty")
    if target["role"] not in {"candidate", "cycle_count_baseline"}:
        raise CampaignIntegrityError(f"{field}.role is invalid")
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
    if workload["mode"] != "simulate" or not all(
        isinstance(workload[key], bool) for key in ("trace", "coverage")
    ):
        raise CampaignIntegrityError("workload mode/trace/coverage is invalid")
    _validate_eda(workload["eda"])
    _validate_run_cwd(workload["run_cwd"])
    _validate_runtime_declarations(workload["runtime_inputs"])
    _validate_source_recipe(workload["source_recipe"])
    _validate_build_recipe(workload["build_recipe"])
    if workload["pre_sim_build_access"] not in {"immutable", "legacy-per-test"}:
        raise CampaignIntegrityError("pre_sim_build_access is invalid")
    return workload


def _validate_eda(value: object) -> None:
    eda = _exact_object(value, {"kind", "version"}, "eda")
    if any(not isinstance(item, str) or not item for item in eda.values()):
        raise CampaignIntegrityError("EDA kind/version must be nonempty")


def _validate_run_cwd(value: object) -> None:
    run_cwd = _exact_object(value, {"configured", "kind", "placeholders"}, "run_cwd")
    if run_cwd["kind"] not in {"literal", "templated"}:
        raise CampaignIntegrityError("run_cwd.kind is invalid")
    placeholders = _exact_list(run_cwd["placeholders"], "run_cwd.placeholders", 4)
    if len(set(placeholders)) != len(placeholders) or set(placeholders) - {
        "campaign",
        "target",
        "test",
        "attempt",
    }:
        raise CampaignIntegrityError("run_cwd placeholders are invalid")
    configured = run_cwd["configured"]
    if not isinstance(configured, str):
        raise CampaignIntegrityError("run_cwd.configured must be a string")
    parsed = list(dict.fromkeys(re.findall(r"\{(campaign|target|test|attempt)\}", configured)))
    if placeholders != parsed or (run_cwd["kind"] == "literal") != (not parsed):
        raise CampaignIntegrityError("run_cwd placeholders/kind disagree with configured value")


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
            raise CampaignIntegrityError("runtime declaration digest disagrees")
        ids.append(expected)
        destinations.append(destination)
    if len(set(ids)) != len(ids) or len(set(destinations)) != len(destinations):
        raise CampaignIntegrityError("runtime declarations must be unique")


def _validate_source_recipe(value: object) -> None:
    recipe = _exact_object(
        value, {"sources", "parameters", "defines", "pre_sim_commands"}, "source_recipe"
    )
    _validate_source_entries(recipe["sources"], "source_recipe.sources")
    parameters = _exact_list(recipe["parameters"], "parameters", 10_000)
    names: list[object] = []
    for index, item in enumerate(parameters):
        parameter = _exact_object(item, {"name", "value"}, f"parameters[{index}]")
        if not isinstance(parameter["name"], str) or not parameter["name"]:
            raise CampaignIntegrityError("parameter name must be nonempty")
        names.append(parameter["name"])
    if len(set(names)) != len(names):
        raise CampaignIntegrityError("parameter names must be unique")
    _exact_list(recipe["defines"], "defines", 10_000)
    _exact_list(recipe["pre_sim_commands"], "pre_sim_commands", 10_000)


def _validate_source_entries(value: object, field: str) -> list[Mapping[str, object]]:
    entries = _exact_list(value, field, MAX_LIST_ITEMS)
    decoded: list[Mapping[str, object]] = []
    for index, item in enumerate(entries):
        entry = _exact_object(item, {"path", "bytes", "sha256", "kind"}, f"{field}[{index}]")
        validate_relative_path(cast(str, entry["path"]))
        _require_nonnegative_int(entry["bytes"], f"{field}.bytes")
        _require_digest(entry["sha256"], f"{field}.sha256")
        if entry["kind"] not in {"rtl", "testbench", "constraint", "user", "generated_input"}:
            raise CampaignIntegrityError(f"{field}.kind is invalid")
        decoded.append(entry)
    return decoded


def _validate_build_recipe(value: object) -> None:
    recipe = _exact_object(
        value, {"backend", "toplevel", "arguments", "command_model_sha256"}, "build_recipe"
    )
    if not isinstance(recipe["backend"], str) or not recipe["backend"]:
        raise CampaignIntegrityError("build backend must be nonempty")
    if not isinstance(recipe["toplevel"], str) or not recipe["toplevel"]:
        raise CampaignIntegrityError("build toplevel must be nonempty")
    _exact_list(recipe["arguments"], "build arguments", 10_000)
    _require_digest(recipe["command_model_sha256"], "command_model_sha256")


def _validate_required_suite(value: object) -> Mapping[str, object]:
    suite = _exact_object(
        value,
        {"names", "default_invocation", "source_path", "source_bytes", "source_sha256"},
        "required_suite",
    )
    names = _exact_list(suite["names"], "required_suite.names", MAX_OBSERVATIONS)
    if any(not isinstance(name, str) or not name for name in names) or len(set(names)) != len(
        names
    ):
        raise CampaignIntegrityError("required suite names must be unique and nonempty")
    if not isinstance(suite["default_invocation"], bool):
        raise CampaignIntegrityError("default_invocation must be boolean")
    _require_nonnegative_int(suite["source_bytes"], "required_suite.source_bytes")
    _require_digest(suite["source_sha256"], "required_suite.source_sha256")
    if suite["source_path"]:
        validate_relative_path(cast(str, suite["source_path"]))
    if suite["default_invocation"] != (not names):
        raise CampaignIntegrityError("required suite default flag disagrees with names")
    if suite["default_invocation"] and (
        suite["source_path"] != ""
        or suite["source_bytes"] != 0
        or suite["source_sha256"] != _digest_bytes(b"")
    ):
        raise CampaignIntegrityError("default required suite must authenticate empty source")
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
        raise CampaignIntegrityError("build variant IDs must be unique")
    return decoded


def _validate_variant(variant: Mapping[str, object], workload: Mapping[str, object]) -> None:
    if variant["kind"] not in {"candidate", "trace", "coverage"}:
        raise CampaignIntegrityError("build variant kind is invalid")
    if not isinstance(variant["sharing_eligible"], bool):
        raise CampaignIntegrityError("sharing_eligible must be boolean")
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
        raise CampaignIntegrityError("build variant recipe digest/ID disagrees")


def _validate_disclosures(value: object) -> list[Mapping[str, object]]:
    disclosures = _exact_list(value, "planning_disclosures", MAX_WORK_ITEMS)
    decoded: list[Mapping[str, object]] = []
    for index, item in enumerate(disclosures):
        disclosure = _exact_object(
            item,
            {"planner", "scratch_inputs", "generated_files", "tool_provenance", "cleanup"},
            f"planning_disclosures[{index}]",
        )
        _validate_source_entries(disclosure["scratch_inputs"], "scratch_inputs")
        _validate_source_entries(disclosure["generated_files"], "generated_files")
        provenance = _exact_object(
            disclosure["tool_provenance"],
            {"kind", "version", "contract_version"},
            "planning tool_provenance",
        )
        if any(not isinstance(entry, str) or not entry for entry in provenance.values()):
            raise CampaignIntegrityError("planning tool provenance must be nonempty")
        cleanup = _exact_object(disclosure["cleanup"], {"removed"}, "cleanup")
        if cleanup["removed"] is not True:
            raise CampaignIntegrityError("planning scratch must be removed")
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
            raise CampaignIntegrityError("prerequisite role/observation is invalid")
        _require_uuid(prerequisite["campaign_id"], "prerequisite campaign_id")
        target = _validate_target(prerequisite["target"], "prerequisite.target")
        manifest = _exact_object(
            prerequisite["manifest"],
            {"path_base", "path", "bytes", "sha256", "kind", "owner"},
            "prerequisite.manifest",
        )
        if (
            manifest["path_base"] != "origin_invocation"
            or manifest["kind"] != "simulation_campaign_manifest"
        ):
            raise CampaignIntegrityError("prerequisite manifest reference is invalid")
        _validate_evidence_ref(manifest, "prerequisite.manifest")
        if not cast(str, manifest["path"]).startswith("targets/"):
            raise CampaignIntegrityError("prerequisite manifest must start beneath targets/")
        if manifest["owner"] != prerequisite["campaign_id"]:
            raise CampaignIntegrityError("prerequisite manifest owner disagrees")
        identities.append(
            (prerequisite["campaign_id"], target["project_identity"], target["revision"])
        )
        decoded.append(prerequisite)
    if len(set(identities)) != len(identities):
        raise CampaignIntegrityError("prerequisite identities must be unique")
    return decoded


def _validate_work_items(
    value: object, target: Mapping[str, object], variants: list[Mapping[str, object]]
) -> list[Mapping[str, object]]:
    items = _exact_list(value, "work_items", MAX_WORK_ITEMS)
    variant_ids = {variant["build_variant_id"] for variant in variants}
    decoded_items: list[Mapping[str, object]] = []
    for ordinal, item in enumerate(items):
        decoded = _decode_work_item(item, ordinal)
        if decoded["target"] != target or decoded["build_variant_id"] not in variant_ids:
            raise CampaignIntegrityError("work item target/build variant disagrees")
        identity_input = {
            key: entry
            for key, entry in decoded.items()
            if key not in {"fingerprint_sha256", "work_item_id"}
        }
        expected = _digest(identity_input)
        expected_id = f"item:{ordinal:04d}:{expected.removeprefix('sha256:')[:16]}"
        if decoded["fingerprint_sha256"] != expected or decoded["work_item_id"] != expected_id:
            raise CampaignIntegrityError("work item fingerprint/ID disagrees")
        decoded_items.append(decoded)
    observation_count = sum(
        len(cast(Mapping[str, object], item["selection"])["names"])
        or (1 if cast(Mapping[str, object], item["selection"])["kind"] == "default" else 0)
        for item in decoded_items
    )
    if observation_count > MAX_OBSERVATIONS:
        raise CampaignIntegrityError("planned observations exceed campaign ceiling")
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
    if isinstance(item["ordinal"], bool) or item["ordinal"] != ordinal:
        raise CampaignIntegrityError("work item ordinal disagrees with order")
    if item["kind"] not in {"ordinary_hdl", "cocotb_batch", "coverage_aggregate"}:
        raise CampaignIntegrityError("work item kind is invalid")
    selection = _exact_object(item["selection"], {"kind", "names"}, "selection")
    names = _exact_list(selection["names"], "selection.names", MAX_OBSERVATIONS)
    if selection["kind"] == "named" and not names:
        raise CampaignIntegrityError("named selection must be nonempty")
    if selection["kind"] in {"default", "unfiltered"} and names:
        raise CampaignIntegrityError("unnamed selection must have no names")
    if selection["kind"] not in {"named", "default", "unfiltered"}:
        raise CampaignIntegrityError("selection kind is invalid")
    kind = item["kind"]
    if kind == "ordinary_hdl" and selection["kind"] == "named" and len(names) != 1:
        raise CampaignIntegrityError("named ordinary work item requires one test")
    if kind == "ordinary_hdl" and selection["kind"] == "unfiltered":
        raise CampaignIntegrityError("ordinary work item cannot be unfiltered")
    if kind == "cocotb_batch" and selection["kind"] == "default":
        raise CampaignIntegrityError("Cocotb work item cannot use default selection")
    if kind == "coverage_aggregate" and selection["kind"] != "named":
        raise CampaignIntegrityError("coverage work item requires named selection")
    run_directory = _exact_object(
        item["run_directory"], {"configured", "kind", "collision_template"}, "run_directory"
    )
    if run_directory["kind"] not in {"literal", "templated"}:
        raise CampaignIntegrityError("work item run-directory kind is invalid")
    arguments = _exact_list(item["arguments"], "arguments", 10_000)
    if any(not isinstance(argument, str) for argument in arguments):
        raise CampaignIntegrityError("work item arguments must be strings")
    return item


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
            raise CampaignIntegrityError(f"fingerprints.{key} disagrees")


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


def _validate_evidence_refs(value: object) -> None:
    if isinstance(value, list):
        for item in value:
            _validate_evidence_refs(item)
        return
    if not isinstance(value, Mapping):
        return
    fields = set(value)
    evidence_fields = {"path", "bytes", "sha256", "kind", "owner"}
    if evidence_fields <= fields and frozenset(fields) in {
        frozenset(evidence_fields),
        frozenset({*evidence_fields, "build_attempt_id"}),
        frozenset({*evidence_fields, "build_attempt_id", "state", "sharing"}),
    }:
        _validate_evidence_ref(value, "EvidenceRef")
    for item in value.values():
        _validate_evidence_refs(item)


def _validate_evidence_ref(value: object, field: str) -> Mapping[str, object]:
    reference = cast(Mapping[str, object], value)
    required = {"path", "bytes", "sha256", "kind", "owner"}
    if not isinstance(value, Mapping) or not required <= set(value):
        raise CampaignIntegrityError(f"{field} must be an EvidenceRef")
    validate_relative_path(cast(str, reference["path"]))
    _require_nonnegative_int(reference["bytes"], f"{field}.bytes")
    _require_digest(reference["sha256"], f"{field}.sha256")
    for key in ("kind", "owner"):
        if not isinstance(reference[key], str) or not reference[key]:
            raise CampaignIntegrityError(f"{field}.{key} must be nonempty")
    return reference


def decode_campaign_manifest(raw: bytes) -> SimulationCampaignManifest:
    return _decode(
        raw,
        schema="booley.simulation-campaign-manifest/v1",
        fields=_MANIFEST_FIELDS,
        limit=MANIFEST_MAX_BYTES,
        value_type=SimulationCampaignManifest,
    )


def decode_simulation_attempt(raw: bytes) -> SimulationAttempt:
    return _decode(
        raw,
        schema="booley.simulation-attempt/v1",
        fields=_ATTEMPT_FIELDS,
        limit=RECORD_MAX_BYTES,
        value_type=SimulationAttempt,
    )


def decode_bundle_build_attempt(raw: bytes) -> BundleBuildAttempt:
    return _decode(
        raw,
        schema="booley.bundle-build-attempt/v1",
        fields=_BUILD_ATTEMPT_FIELDS,
        limit=RECORD_MAX_BYTES,
        value_type=BundleBuildAttempt,
    )


def decode_bundle_build_result(raw: bytes) -> BundleBuildResult:
    return _decode(
        raw,
        schema="booley.bundle-build-result/v1",
        fields=_BUILD_RESULT_FIELDS,
        limit=RECORD_MAX_BYTES,
        value_type=BundleBuildResult,
    )


decode_build_result = decode_bundle_build_result


def decode_simulation_result(raw: bytes) -> SimulationResult:
    return _decode(
        raw,
        schema="booley.simulation-result/v1",
        fields=_RESULT_FIELDS,
        limit=RECORD_MAX_BYTES,
        value_type=SimulationResult,
    )


def decode_simulator_bundle(raw: bytes) -> SimulatorBundle:
    return _decode(
        raw,
        schema="booley.simulator-bundle/v1",
        fields=_BUNDLE_FIELDS,
        limit=MANIFEST_MAX_BYTES,
        value_type=SimulatorBundle,
    )


def decode_executable_snapshot(raw: bytes) -> ExecutableSnapshot:
    return _decode(
        raw,
        schema="booley.executable-snapshot/v1",
        fields=_SNAPSHOT_FIELDS,
        limit=MANIFEST_MAX_BYTES,
        value_type=ExecutableSnapshot,
    )


def validate_relative_path(path: str) -> str:
    """Validate a normalized contained portable relative campaign path."""
    if not path or len(path.encode("utf-8")) > MAX_PATH_BYTES:
        raise CampaignIntegrityError("path must be nonempty and at most 1 KiB")
    if "\\" in path or path.startswith("/") or _WINDOWS_DRIVE_RE.match(path):
        raise CampaignIntegrityError("path must be separator-normalized and relative")
    parts = path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise CampaignIntegrityError("path must be contained and normalized")
    return path

"""Strict canonical JSON codecs for immutable Simulation Campaign values.

This module is deliberately infrastructure-free. It decodes untrusted
documents into immutable values. Manifests are checked for shape only: planning proved their content and
published them atomically, so each manifest reader below reads just the members
its callers use (#2136). Attempt, build attempt/result, bundle and snapshot
records get only the boundary check: size, UTF-8 JSON, exact fields, ``$schema``
and canonical bytes (#1743).
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
from booley.flows.sim.campaign_files import canonical_json
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

_VARIANT_ID_RE = re.compile(r"variant:[0-9a-f]{64}\Z")
_WORK_ITEM_ID_RE = re.compile(r"item:[0-9]{4}:[0-9a-f]{16}\Z")
_WINDOWS_DRIVE_RE = re.compile(r"[A-Za-z]:")


class SimulationCampaignIntegrityError(ValueError):
    """An authoritative campaign value violates its exact contract."""


class UnsupportedSimulationCampaignFormatError(ValueError):
    """A record Booley 0.3.0 wrote.

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
            f"{label} {schema} was written by Booley 0.3.0 and is no longer supported; "
            f"delete {where} by hand"
        )
        self.label = label
        self.schema = schema

    def located(self, invocation: Path) -> UnsupportedSimulationCampaignFormatError:
        """The same refusal naming the invocation directory to delete."""
        return UnsupportedSimulationCampaignFormatError(self.label, self.schema, invocation)


def raise_if_retired(document: Mapping[str, object]) -> None:
    """Refuse a document in a format Booley 0.3.0 wrote, before any other check."""
    schema = document.get("$schema")
    if isinstance(schema, str) and schema in _RETIRED_SCHEMAS:
        raise UnsupportedSimulationCampaignFormatError(_RETIRED_SCHEMAS[schema], schema)


def canonical_json_bytes(value: object) -> bytes:
    """Encode finite JSON as sorted UTF-8 with one trailing newline."""
    try:
        return canonical_json(value)
    except (TypeError, ValueError) as exc:
        raise SimulationCampaignIntegrityError(f"value is not canonical JSON: {exc}") from exc


# v4 = the exact work-item shape with the required ``test_runs`` of every work
# item: the concrete Test Runs (Test Variant, seed, declared arguments and
# environment) the item executes (ADR 0072), an optional ``no_waivers`` and
# explicit declared source identities in disclosures. It is the only version
# written and read.
MANIFEST_SCHEMA = "booley.simulation-campaign-manifest/v4"

# v3 = the required ``test_run`` of every observation: the resolved name of the
# Test Run it observed (ADR 0072). Observations are unique by Test Run.
RESULT_SCHEMA = "booley.simulation-result/v3"

# Formats Booley 0.3.0 wrote. They are recognised only to be refused.
_RETIRED_SCHEMAS = {
    "booley.simulation-campaign-manifest/v1": "Simulation Campaign manifest",
    "booley.simulation-campaign-manifest/v2": "Simulation Campaign manifest",
    "booley.simulation-result/v2": "Simulation Result",
    "booley.simulation-projection/v2": "Simulation projection",
}

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
_P = ParamSpec("_P")
_R = TypeVar("_R")


def reads_campaign_records(function: Callable[_P, _R]) -> Callable[_P, _R]:
    """Report bad nested data in a published record as campaign corruption.

    Attempt, build and bundle decoders check only the document boundary (#1743),
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


def _validate_attempt_identity(value: Mapping[str, object], ordinal_key: str) -> None:
    ordinal = _require_positive_int(value[ordinal_key], ordinal_key)
    if ordinal > MAX_ATTEMPTS_PER_ITEM:
        raise SimulationCampaignIntegrityError(
            f"{ordinal_key} exceeds {MAX_ATTEMPTS_PER_ITEM}-attempt ceiling"
        )
    _require_positive_int(value["producer_invocation_id"], "producer_invocation_id")


def _validate_simulation_result(value: Mapping[str, object]) -> None:
    _require_nonnegative_number(value["elapsed_seconds"], "elapsed_seconds")
    _validate_attempt_identity(value, "attempt_ordinal")
    _require_uuid(value["attempt_id"], "attempt_id")
    _require_work_item_id(value["work_item_id"])
    build_result = _validate_simulation_build_ref(value["build_result"])
    state = value["state"]
    if state not in {
        "completed",
        "timeout",
        "crash",
        "aborted",
        "setup_error",
        "blocked_by_build",
        "not_run",
    }:
        raise SimulationCampaignIntegrityError("invalid simulation result state")
    if value["grade"] not in {grade.value for grade in StrictGrade}:
        raise SimulationCampaignIntegrityError("invalid strict grade")
    _validate_result_state(value, build_result)
    _validate_runtime_inputs(value["runtime_inputs"], blocked=state == "blocked_by_build")
    observations = _validate_observations(value["observations"], cast(str, state))
    _validate_result_grade(value["grade"], observations)
    _validate_diagnostics(value["diagnostics"])
    _validate_evidence_list(value["evidence"], "evidence")


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
    if state == "aborted" and build_result["state"] == "infrastructure_error":
        _validate_infrastructure_build_abort(value)
        return
    if build_result["state"] != "ready":
        raise SimulationCampaignIntegrityError("non-blocked result requires ready build")
    _require_uuid(value["bundle_id"], "bundle_id")
    if (
        state in {"completed", "timeout", "crash", "aborted"}
        and value["executable_snapshot"] is None
    ):
        raise SimulationCampaignIntegrityError("post-launch result requires executable snapshot")
    _validate_executable_snapshot(value["executable_snapshot"], value["attempt_id"])


def _validate_infrastructure_build_abort(value: Mapping[str, object]) -> None:
    if value["bundle_id"] is not None or value["executable_snapshot"] is not None:
        raise SimulationCampaignIntegrityError("infrastructure build abort cannot bind executable")
    if value["runtime_inputs"] or value["grade"] != "error":
        raise SimulationCampaignIntegrityError("infrastructure build abort has invalid execution")
    observations = _exact_list(value["observations"], "observations", MAX_OBSERVATIONS)
    if not observations or any(
        not isinstance(item, dict)
        or item.get("execution") != "aborted"
        or item.get("failure_class") != "infrastructure"
        or item.get("functional") != "not_observed"
        or item.get("assertions") != "not_observed"
        or item.get("cycle_count") is not None
        for item in observations
    ):
        raise SimulationCampaignIntegrityError("infrastructure build abort has observed verdict")


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
        # A result is unique by Test Run: one Test may be observed under
        # several Test Variants or seeds.
        tests.append(observation["test_run"])
        decoded.append(observation)
        if state == "completed" and observation["execution"] != "completed":
            raise SimulationCampaignIntegrityError(
                "completed result requires completed observations"
            )
        if state == "blocked_by_build" and observation["execution"] != "blocked_by_build":
            raise SimulationCampaignIntegrityError("blocked result requires blocked observations")
        if state == "setup_error" and observation["execution"] != "setup_error":
            raise SimulationCampaignIntegrityError("setup result requires setup observations")
        if state == "aborted" and observation["execution"] not in {
            "completed",
            "aborted",
            "not_run",
        }:
            raise SimulationCampaignIntegrityError("aborted result has invalid observations")
    named = [test for test in tests if test is not None]
    if len(set(named)) != len(named):
        raise SimulationCampaignIntegrityError("observation test names must be unique")
    if state in {"completed", "timeout", "crash", "aborted"}:
        strength = {"completed": 0, "not_run": 0, "timeout": 1, "crash": 2, "aborted": 3}
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


#: How the execution of one observed Test Run ended.
_OBSERVATION_EXECUTIONS = frozenset(
    {"completed", "timeout", "crash", "aborted", "setup_error", "blocked_by_build", "not_run"}
)
#: What one simulator observation records besides its Test Run.
_OBSERVATION_FIELDS = frozenset(
    {
        "test",
        "execution",
        "failure_class",
        "functional",
        "assertions",
        "assertion_count",
        "detail",
        "cycle_count",
    }
)


def _validate_observation(value: object, index: int) -> Mapping[str, object]:
    observation = _exact_object(
        value, {"test_run"} | _OBSERVATION_FIELDS, f"observations[{index}]"
    )
    _bounded_string(observation["test_run"], f"observations[{index}].test_run")
    if observation["execution"] not in _OBSERVATION_EXECUTIONS:
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
    if observation["execution"] in {"setup_error", "blocked_by_build", "not_run"} and (
        observation["failure_class"] not in {"design", "infrastructure"}
        or observation["functional"] != "not_observed"
        or observation["assertions"] != "not_observed"
        or observation["cycle_count"] is not None
    ):
        raise SimulationCampaignIntegrityError("setup/build-block observation matrix is invalid")
    if observation["execution"] == "aborted":
        infrastructure = observation["failure_class"] == "infrastructure"
        valid_functional = (
            observation["functional"] == "not_observed"
            if infrastructure
            else observation["functional"] == "fail"
        )
        if (
            observation["failure_class"] not in {"design", "infrastructure"}
            or not valid_functional
            or observation["assertions"] != "not_observed"
            or observation["cycle_count"] is not None
        ):
            raise SimulationCampaignIntegrityError("aborted observation matrix is invalid")


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
    entry = _read_strings(value, field, ("campaign_id", "work_item_id"))
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
    tuple[str, frozenset[str], int, Callable[[Mapping[str, object]], None] | None],
] = {
    SimulationCampaignManifest: (
        MANIFEST_SCHEMA,
        _MANIFEST_FIELDS,
        MANIFEST_MAX_BYTES,
        _read_manifest,
    ),
    SimulationAttempt: (
        "booley.simulation-attempt/v1",
        _ATTEMPT_FIELDS,
        RECORD_MAX_BYTES,
        None,
    ),
    BundleBuildAttempt: (
        "booley.bundle-build-attempt/v1",
        _BUILD_ATTEMPT_FIELDS,
        RECORD_MAX_BYTES,
        None,
    ),
    BundleBuildResult: (
        "booley.bundle-build-result/v1",
        _BUILD_RESULT_FIELDS,
        RECORD_MAX_BYTES,
        None,
    ),
    SimulationResult: (
        RESULT_SCHEMA,
        _RESULT_FIELDS,
        RECORD_MAX_BYTES,
        _validate_simulation_result,
    ),
    SimulatorBundle: (
        "booley.simulator-bundle/v1",
        _BUNDLE_FIELDS,
        MANIFEST_MAX_BYTES,
        None,
    ),
    ExecutableSnapshot: (
        "booley.executable-snapshot/v1",
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
    return canonical_json_bytes(value.document)


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
    exactly ``targets/<Target>/campaign/manifest.json``, relative and contained,
    which is what planning writes. That also keeps the invocation the same at
    every hop of a prerequisite walk.
    """
    path = reference["path"]
    parts = path.split("/") if isinstance(path, str) else []
    if len(parts) != 4 or parts[0] != "targets" or parts[2:] != ["campaign", "manifest.json"]:
        raise SimulationCampaignIntegrityError(
            f"prerequisite manifest path {path!r} is not "
            "targets/<Target>/campaign/manifest.json inside its invocation"
        )
    return invocation / validate_relative_path(path)

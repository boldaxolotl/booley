"""Strict immutable acceptance facts reconstructed from campaign records."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from .codec import SimulationCampaignIntegrityError, canonical_json_bytes

_SCHEMA = "booley.simulation-acceptance-facts/v1"
_FIELDS = frozenset(
    {
        "$schema",
        "campaign_id",
        "manifest_sha256",
        "origin",
        "target",
        "required_suite",
        "prerequisites",
        "consumed_results",
        "observations",
        "coverage_reference",
    }
)
_MAX_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class AcceptanceFacts:
    """Validated JSON-shaped facts consumed by the acceptance coordinator."""

    document: Mapping[str, object]

    def __post_init__(self) -> None:
        validated = decode_acceptance_facts(canonical_json_bytes(self.document))
        object.__setattr__(self, "document", validated.document)

    @property
    def sha256(self) -> str:
        return acceptance_facts_sha256(self)

    def canonical_bytes(self) -> bytes:
        return encode_acceptance_facts(self)


def decode_acceptance_facts(raw: bytes) -> AcceptanceFacts:
    """Decode canonical facts with exact fields and bounded cardinalities."""
    if len(raw) > _MAX_BYTES:
        raise SimulationCampaignIntegrityError("acceptance facts exceed size ceiling")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SimulationCampaignIntegrityError("acceptance facts are not valid JSON") from exc
    if not isinstance(value, dict) or set(value) != _FIELDS:
        raise SimulationCampaignIntegrityError(
            f"acceptance facts must have exact fields {sorted(_FIELDS)}"
        )
    if canonical_json_bytes(value) != raw:
        raise SimulationCampaignIntegrityError("acceptance facts are not canonical JSON")
    _validate_facts(value)
    instance = object.__new__(AcceptanceFacts)
    object.__setattr__(instance, "document", _freeze(value))
    return instance


def encode_acceptance_facts(value: AcceptanceFacts) -> bytes:
    """Validate and encode exact canonical facts."""
    if type(value) is not AcceptanceFacts:
        raise SimulationCampaignIntegrityError("expected AcceptanceFacts")
    raw = canonical_json_bytes(value.document)
    decode_acceptance_facts(raw)
    return raw


def acceptance_facts_sha256(value: AcceptanceFacts) -> str:
    raw = encode_acceptance_facts(value)
    return "sha256:" + hashlib.sha256(raw.rstrip(b"\n")).hexdigest()


def _validate_facts(value: Mapping[str, object]) -> None:
    if value["$schema"] != _SCHEMA:
        raise SimulationCampaignIntegrityError("unsupported acceptance facts schema")
    _uuid(value["campaign_id"], "campaign_id")
    _digest(value["manifest_sha256"], "manifest_sha256")
    _exact(value["origin"], {"execution_id", "invocation_id"}, "origin")
    _exact(
        value["target"],
        {"vlnv", "name", "selector", "project_identity", "revision", "role", "display_name"},
        "target",
    )
    suite = _exact(
        value["required_suite"], {"names", "default_invocation", "source_sha256"}, "required_suite"
    )
    names = _list(suite["names"], "required_suite.names", 4_000)
    if any(not isinstance(name, str) or not name for name in names) or len(set(names)) != len(
        names
    ):
        raise SimulationCampaignIntegrityError("required suite names are invalid")
    if type(suite["default_invocation"]) is not bool:
        raise SimulationCampaignIntegrityError("required_suite.default_invocation must be boolean")
    if names and suite["default_invocation"]:
        raise SimulationCampaignIntegrityError(
            "required suite names disagree with default_invocation"
        )
    _digest(suite["source_sha256"], "required_suite.source_sha256")
    prerequisites = _list(value["prerequisites"], "prerequisites", 1_000)
    for index, item in enumerate(prerequisites):
        _validate_prerequisite(item, index)
    consumed = _list(value["consumed_results"], "consumed_results", 1_000)
    for index, item in enumerate(consumed):
        _validate_consumed(item, index)
    observations = _list(value["observations"], "observations", 4_000)
    observed_tests: set[object] = set()
    for index, item in enumerate(observations):
        _validate_observation(item, index)
        assert isinstance(item, Mapping)
        test = item["test"] if item["test"] is not None else "default"
        if test in observed_tests:
            raise SimulationCampaignIntegrityError("acceptance facts repeat an observation test")
        observed_tests.add(test)
    coverage = value["coverage_reference"]
    if coverage is not None:
        _validate_coverage_reference(value, coverage)


def _validate_coverage_reference(facts: Mapping[str, object], value: object) -> None:
    from booley.flows.sim.coverage_reference import decode_coverage_campaign_reference

    exact = _exact(value, {"reference", "document"}, "coverage_reference")
    reference = _exact(
        exact["reference"],
        {"path_base", "path", "bytes", "sha256", "kind", "owner"},
        "coverage_reference.reference",
    )
    _evidence(reference, "coverage_reference.reference")
    raw = canonical_json_bytes(exact["document"])
    document = decode_coverage_campaign_reference(raw).document
    target = facts["target"]
    assert isinstance(target, Mapping)
    origin = _exact(facts["origin"], {"execution_id", "invocation_id"}, "origin")
    bound_target = document["target"]
    assert isinstance(bound_target, Mapping)
    if (
        reference["bytes"] != len(raw)
        or reference["sha256"] != "sha256:" + hashlib.sha256(raw).hexdigest()
        or reference["kind"] != "coverage_campaign_reference"
        or reference["owner"] != facts["campaign_id"]
        or reference["path"] != f"targets/{target['selector']}/coverage.json"
        or document["simulation_campaign_id"] != facts["campaign_id"]
        or document["simulation_manifest_sha256"] != facts["manifest_sha256"]
        or document["origin_invocation_id"] != origin["invocation_id"]
        or bound_target["identity"] != f"{target['vlnv']}#{target['name']}"
        or bound_target["selector"] != target["selector"]
    ):
        raise SimulationCampaignIntegrityError(
            "coverage reference evidence disagrees with acceptance facts"
        )


def _validate_prerequisite(value: object, index: int) -> None:
    item = _exact(
        value,
        {
            "role",
            "manifest",
            "campaign_id",
            "target",
            "work_item_id",
            "result",
            "cycle_observation",
        },
        f"prerequisites[{index}]",
    )
    if item["role"] != "cycle_count_baseline":
        raise SimulationCampaignIntegrityError("prerequisite role is invalid")
    _uuid(item["campaign_id"], "prerequisite campaign_id")
    _evidence(item["manifest"], "prerequisite manifest")
    _evidence(item["result"], "prerequisite result")
    cycle = _exact(item["cycle_observation"], {"test", "cycle_count", "unit"}, "cycle_observation")
    if (
        cycle["unit"] != "cycles"
        or type(cycle["cycle_count"]) is not int
        or cycle["cycle_count"] < 0
    ):
        raise SimulationCampaignIntegrityError("cycle observation is invalid")


def _validate_consumed(value: object, index: int) -> None:
    item = _exact(
        value,
        {"work_item_id", "role", "revision", "target", "attempt_id", "result", "finished_at"},
        f"consumed_results[{index}]",
    )
    _uuid(item["attempt_id"], "attempt_id")
    _evidence(item["result"], "consumed result")


def _validate_observation(value: object, index: int) -> None:
    item = _exact(
        value,
        {
            "work_item_id",
            "role",
            "revision",
            "target",
            "result_sha256",
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
    _digest(item["result_sha256"], "observation result_sha256")
    if item["test"] is not None and (
        not isinstance(item["test"], str) or len(item["test"].encode()) > 512
    ):
        raise SimulationCampaignIntegrityError("observation test is invalid")
    if len(canonical_json_bytes(item["detail"]).rstrip(b"\n")) > 2 * 1024:
        raise SimulationCampaignIntegrityError("observation detail is invalid")
    if item["execution"] not in {
        "completed",
        "timeout",
        "crash",
        "setup_error",
        "blocked_by_build",
    }:
        raise SimulationCampaignIntegrityError("observation execution is invalid")
    if item["failure_class"] not in {None, "design", "infrastructure"}:
        raise SimulationCampaignIntegrityError("observation failure_class is invalid")
    if item["functional"] not in {"pass", "fail", "inconclusive", "not_observed"}:
        raise SimulationCampaignIntegrityError("observation functional value is invalid")
    if item["assertions"] not in {"clean", "dirty", "not_observed"}:
        raise SimulationCampaignIntegrityError("observation assertions value is invalid")
    if type(item["assertion_count"]) is not int or item["assertion_count"] < 0:
        raise SimulationCampaignIntegrityError("observation assertion_count is invalid")
    if item["cycle_count"] is not None and (
        type(item["cycle_count"]) is not int or item["cycle_count"] < 0
    ):
        raise SimulationCampaignIntegrityError("observation cycle_count is invalid")


def _evidence(value: object, field: str) -> None:
    item = _exact(value, {"path_base", "path", "bytes", "sha256", "kind", "owner"}, field)
    if item["path_base"] != "origin_invocation":
        raise SimulationCampaignIntegrityError(f"{field}.path_base is invalid")
    if not isinstance(item["path"], str) or len(item["path"].encode()) > 1024:
        raise SimulationCampaignIntegrityError(f"{field}.path is invalid")
    if type(item["bytes"]) is not int or item["bytes"] < 0:
        raise SimulationCampaignIntegrityError(f"{field}.bytes is invalid")
    _digest(item["sha256"], f"{field}.sha256")


def _exact(value: object, fields: set[str], label: str) -> Mapping[str, object]:
    if not isinstance(value, dict) or set(value) != fields:
        raise SimulationCampaignIntegrityError(f"{label} must have exact fields {sorted(fields)}")
    return value


def _list(value: object, label: str, maximum: int) -> list[object]:
    if not isinstance(value, list) or len(value) > maximum:
        raise SimulationCampaignIntegrityError(f"{label} must be a bounded list")
    return value


def _digest(value: object, field: str) -> None:
    if not isinstance(value, str) or not value.startswith("sha256:") or len(value) != 71:
        raise SimulationCampaignIntegrityError(f"{field} must be a sha256 digest")
    try:
        int(value[7:], 16)
    except ValueError as exc:
        raise SimulationCampaignIntegrityError(f"{field} must be a sha256 digest") from exc


def _uuid(value: object, field: str) -> None:
    import uuid

    if not isinstance(value, str):
        raise SimulationCampaignIntegrityError(f"{field} must be UUIDv4")
    try:
        parsed = uuid.UUID(value)
    except ValueError as exc:
        raise SimulationCampaignIntegrityError(f"{field} must be UUIDv4") from exc
    if parsed.version != 4 or str(parsed) != value:
        raise SimulationCampaignIntegrityError(f"{field} must be UUIDv4")


def _freeze(value: object) -> object:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


__all__ = [
    "AcceptanceFacts",
    "acceptance_facts_sha256",
    "decode_acceptance_facts",
    "encode_acceptance_facts",
]

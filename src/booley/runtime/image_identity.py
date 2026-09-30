"""Read-only identity comparison for issued Sandbox Images."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass
from enum import StrEnum

from booley.core.boundary import (
    BoundaryError,
    require_dict,
    require_list,
    require_str,
    require_str_value,
)
from booley.core.differences import format_differences
from booley.runtime.image_provenance import (
    ENV_REVISION,
    ENV_SANDBOX_FLAVOR,
    ENV_VERSION,
    LABEL_ARTIFACT_ROLE,
    LABEL_EFFECTIVE_INPUTS,
    LABEL_LOGICAL_SELECTION_FINGERPRINT,
    LABEL_PARENT_ARTIFACT,
    LABEL_PARENT_ARTIFACT_KIND,
    LABEL_PAYLOAD_FINGERPRINT,
    LABEL_RECIPE_FINGERPRINT,
    LABEL_REVISION,
    LABEL_RUNTIME_BASE_CONTRACT,
    LABEL_SCHEMA,
    LABEL_STANDARD_SUBSTRATE_CONTRACT,
    LABEL_VERSION,
    LABEL_WHEEL_SOURCE_FINGERPRINT,
    PARENT_ARTIFACT_LOCAL_IMAGE_ID,
    PARENT_ARTIFACT_REGISTRY_DIGEST,
    PROVENANCE_SCHEMA,
)

_HEX_REVISION = re.compile(r"[0-9a-fA-F]+")
_SHA256_HEX = re.compile(r"[0-9a-fA-F]{64}")
_MAX_ANCESTRY = 32
_MAX_METADATA_ENTRIES = 256
_MAX_METADATA_VALUE_LENGTH = 16_384


class Status(StrEnum):
    """Whether available identity evidence proves equality or inequality."""

    MATCH = "match"
    MISMATCH = "mismatch"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class Comparison:
    """One tri-state comparison with safe, named mismatch detail."""

    status: Status
    detail: str = ""


@dataclass(frozen=True, slots=True)
class ImageMetadata:
    """Bounded Docker metadata for one exact image reference."""

    reference: str
    image_id: str
    labels: Mapping[str, str]
    environment: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class BooleyBuildIdentity:
    """Operator-visible Booley code identity embedded in a Sandbox Image."""

    version: str | None
    revision: str | None
    payload_fingerprint: str | None
    wheel_source_fingerprint: str | None


@dataclass(frozen=True, slots=True)
class LogicalSelectionNode:
    """ID-independent compatibility projection of one image graph node."""

    role: str
    effective_inputs: str
    recipe_fingerprint: str
    parent_compatibility_key: str
    runtime_base_contract: str
    standard_substrate_contract: str


def _available(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized or normalized.lower() == "unknown":
        return None
    return normalized


def _decode_labels(raw: object) -> dict[str, str]:
    if raw is None:
        return {}
    values = require_dict(raw, field="Config.Labels")
    labels = {
        require_str_value(key, field="label name"): require_str_value(
            value, field=f"label {key!r}", allow_empty=True
        )
        for key, value in values.items()
    }
    if len(labels) > _MAX_METADATA_ENTRIES or any(
        len(key) > _MAX_METADATA_VALUE_LENGTH or len(value) > _MAX_METADATA_VALUE_LENGTH
        for key, value in labels.items()
    ):
        raise BoundaryError("Config.Labels exceeds metadata bounds")
    return labels


def _decode_environment(raw: object) -> dict[str, str]:
    if raw is None:
        return {}
    values = require_list(raw, field="Config.Env")
    items = [require_str_value(item, field="Config.Env item", allow_empty=True) for item in values]
    if len(items) > _MAX_METADATA_ENTRIES or any(
        len(item) > _MAX_METADATA_VALUE_LENGTH for item in items
    ):
        raise BoundaryError("Config.Env exceeds metadata bounds")
    return {key: value for item in items if "=" in item for key, value in (item.split("=", 1),)}


def decode_image_metadata(reference: str, document: object) -> ImageMetadata | None:
    """Decode one ``docker image inspect`` document without guessing."""
    try:
        records = require_list(document, field="docker image inspect response")
        if len(records) != 1:
            return None
        record = require_dict(records[0], field="docker image inspect record")
        image_id = require_str(record, "Id")
        config = require_dict(record.get("Config"), field="Config")
        labels = _decode_labels(config.get("Labels"))
        environment = _decode_environment(config.get("Env"))
    except BoundaryError:
        return None
    return ImageMetadata(reference, image_id, labels, environment)


def build_identity(metadata: ImageMetadata) -> BooleyBuildIdentity:
    """Extract typed code identity; ambiguous legacy environment hashes are ignored."""
    labels = metadata.labels
    environment = metadata.environment
    return BooleyBuildIdentity(
        _available(labels.get(LABEL_VERSION)) or _available(environment.get(ENV_VERSION)),
        _available(labels.get(LABEL_REVISION)) or _available(environment.get(ENV_REVISION)),
        _available(labels.get(LABEL_PAYLOAD_FINGERPRINT)),
        _available(labels.get(LABEL_WHEEL_SOURCE_FINGERPRINT)),
    )


def current_host_build_identity() -> BooleyBuildIdentity | None:
    """Return the recorded canonical host's comparable embedded wheel identity."""
    from booley.runtime.build_stamp import (
        embedded_payload_fingerprint,
        embedded_wheel_source_fingerprint,
    )
    from booley.runtime.host_install import HostInstallationError, load_host_installation

    try:
        recorded = load_host_installation()
    except HostInstallationError:
        return None
    embedded_payload = embedded_payload_fingerprint()
    payload = embedded_payload if embedded_payload == recorded.payload_fingerprint else None
    return BooleyBuildIdentity(
        _available(recorded.version),
        _available(recorded.revision),
        _available(payload),
        _available(embedded_wheel_source_fingerprint()),
    )


def _clean_revisions_match(left: str, right: str) -> bool | None:
    if _HEX_REVISION.fullmatch(left) is None or _HEX_REVISION.fullmatch(right) is None:
        return None
    return left.lower().startswith(right.lower()) or right.lower().startswith(left.lower())


def _mismatch(expected: BooleyBuildIdentity, observed: BooleyBuildIdentity) -> Comparison:
    expected_values = asdict(expected)
    observed_values = asdict(observed)
    differing_expected = {
        field: value
        for field, value in expected_values.items()
        if value is not None
        and observed_values[field] is not None
        and value != observed_values[field]
    }
    differing_observed = {field: observed_values[field] for field in differing_expected}
    return Comparison(
        Status.MISMATCH,
        format_differences(differing_expected, differing_observed),
    )


def compare_build_identity(
    expected: BooleyBuildIdentity,
    observed: BooleyBuildIdentity,
) -> Comparison:
    """Compare only same-kind content identities, with clean revision as fallback."""
    for field in ("wheel_source_fingerprint", "payload_fingerprint"):
        left = getattr(expected, field)
        right = getattr(observed, field)
        if left is not None and right is not None:
            return Comparison(Status.MATCH) if left == right else _mismatch(expected, observed)
    if expected.revision is None or observed.revision is None:
        return Comparison(Status.UNKNOWN)
    revisions_match = _clean_revisions_match(expected.revision, observed.revision)
    if revisions_match is None:
        return Comparison(Status.UNKNOWN)
    return Comparison(Status.MATCH) if revisions_match else _mismatch(expected, observed)


def logical_selection_fingerprint(
    selected_reference: str,
    nodes: Iterable[LogicalSelectionNode],
) -> str:
    """Hash the ordered, bounded identity of one selected logical image graph."""
    document = {
        "selected_reference": selected_reference,
        "nodes": [asdict(node) for node in nodes],
    }
    encoded = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def logical_selection_fingerprint_for_chain(
    selected_reference: str,
    nodes: Iterable[tuple[str, str, str, str, str]],
    *,
    initial_parent_key: str = "",
) -> str:
    """Fingerprint ordered node fields while deriving parent compatibility keys."""
    projection = []
    parent_key = initial_parent_key
    for role, effective_inputs, recipe, runtime_contract, standard_contract in nodes:
        node = LogicalSelectionNode(
            role,
            effective_inputs,
            recipe,
            parent_key,
            runtime_contract,
            standard_contract,
        )
        projection.append(node)
        parent_key = hashlib.sha256(
            json.dumps(asdict(node), sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    return logical_selection_fingerprint(selected_reference, projection)


def _selection_projection(  # noqa: PLR0911 -- each malformed boundary shape fails closed
    reference: str,
    inspect: Callable[[str], ImageMetadata | None],
) -> dict[str, object] | None:
    nodes: list[dict[str, str]] = []
    seen: set[str] = set()
    current = reference
    for _ in range(_MAX_ANCESTRY):
        metadata = inspect(current)
        if metadata is None or metadata.image_id in seen:
            return None
        seen.add(metadata.image_id)
        labels = metadata.labels
        if labels.get(LABEL_SCHEMA) != PROVENANCE_SCHEMA:
            legacy = {
                "role": _available(labels.get(LABEL_ARTIFACT_ROLE)),
                "effective_inputs": _available(labels.get(LABEL_EFFECTIVE_INPUTS)),
                "recipe_fingerprint": _available(labels.get(LABEL_RECIPE_FINGERPRINT)),
            }
            if nodes:
                return None
            return {"nodes": [legacy]} if any(legacy.values()) else None
        role = _available(labels.get(LABEL_ARTIFACT_ROLE))
        recipe = _available(labels.get(LABEL_RECIPE_FINGERPRINT))
        if role is None or recipe is None:
            return None
        nodes.append(
            {
                "role": role,
                "effective_inputs": ""
                if role == "wheel-overlay"
                else (_available(labels.get(LABEL_EFFECTIVE_INPUTS)) or ""),
                "recipe_fingerprint": recipe,
                "runtime_base_contract": _available(labels.get(LABEL_RUNTIME_BASE_CONTRACT)) or "",
                "standard_substrate_contract": _available(
                    labels.get(LABEL_STANDARD_SUBSTRATE_CONTRACT)
                )
                or "",
            }
        )
        parent = _available(labels.get(LABEL_PARENT_ARTIFACT))
        if parent is None:
            return {"nodes": list(reversed(nodes))}
        kind = labels.get(LABEL_PARENT_ARTIFACT_KIND)
        if kind == PARENT_ARTIFACT_REGISTRY_DIGEST:
            nodes[-1]["registry_parent"] = parent
            return {"nodes": list(reversed(nodes))}
        if kind != PARENT_ARTIFACT_LOCAL_IMAGE_ID:
            return None
        current = parent
    return None


def _sandbox_flavor(metadata: ImageMetadata) -> str | None:
    """Return the inherited flavor of a schema-3 final image when conclusive."""
    labels = metadata.labels
    if labels.get(LABEL_SCHEMA) != PROVENANCE_SCHEMA or labels.get(LABEL_ARTIFACT_ROLE) not in {
        "wheel-overlay",
        "project-overlay",
    }:
        return None
    flavor = _available(metadata.environment.get(ENV_SANDBOX_FLAVOR))
    if flavor is None:
        return "standard"
    return flavor if flavor == "riscv" else None


def _projection_difference(left: Mapping[str, object], right: Mapping[str, object]) -> str:
    left_nodes = left.get("nodes")
    right_nodes = right.get("nodes")
    if not isinstance(left_nodes, list) or not isinstance(right_nodes, list):
        return "logical selection differs"
    left_roles = [node.get("role") for node in left_nodes if isinstance(node, dict)]
    right_roles = [node.get("role") for node in right_nodes if isinstance(node, dict)]
    if left_roles != right_roles:
        return format_differences({"roles": left_roles}, {"roles": right_roles})
    for index, (old, new) in enumerate(zip(left_nodes, right_nodes, strict=True)):
        if old != new and isinstance(old, dict) and isinstance(new, dict):
            return f"node {index} ({left_roles[index]}): {format_differences(old, new)}"
    return "logical selection differs"


def _selection_fingerprint(metadata: ImageMetadata) -> tuple[str | None, bool]:
    raw = metadata.labels.get(LABEL_LOGICAL_SELECTION_FINGERPRINT)
    if raw is None:
        return None, True
    value = _available(raw)
    if value is None or _SHA256_HEX.fullmatch(value) is None:
        return None, False
    return value.lower(), True


def _final_selection_node(metadata: ImageMetadata) -> dict[str, str] | None:
    labels = metadata.labels
    if labels.get(LABEL_SCHEMA) != PROVENANCE_SCHEMA:
        return None
    role = _available(labels.get(LABEL_ARTIFACT_ROLE))
    recipe = _available(labels.get(LABEL_RECIPE_FINGERPRINT))
    if role is None or recipe is None:
        return None
    return {
        "role": role,
        "effective_inputs": ""
        if role == "wheel-overlay"
        else (_available(labels.get(LABEL_EFFECTIVE_INPUTS)) or ""),
        "recipe_fingerprint": recipe,
        "parent_kind": _available(labels.get(LABEL_PARENT_ARTIFACT_KIND)) or "",
        "runtime_base_contract": _available(labels.get(LABEL_RUNTIME_BASE_CONTRACT)) or "",
        "standard_substrate_contract": _available(labels.get(LABEL_STANDARD_SUBSTRATE_CONTRACT))
        or "",
    }


def compare_logical_selection(  # noqa: PLR0911 -- tri-state evidence exits stay explicit
    issued_reference: str,
    configured_reference: str,
    inspect_issued: Callable[[str], ImageMetadata | None],
    inspect_configured: Callable[[str], ImageMetadata | None] | None = None,
) -> Comparison:
    """Compare selection identity while ignoring local IDs and wheel contents."""
    configured_inspector = inspect_configured or inspect_issued
    issued = inspect_issued(issued_reference)
    configured = configured_inspector(configured_reference)
    if issued is None or configured is None:
        return Comparison(Status.UNKNOWN)
    issued_flavor = _sandbox_flavor(issued)
    configured_flavor = _sandbox_flavor(configured)
    if (
        issued_flavor is not None
        and configured_flavor is not None
        and issued_flavor != configured_flavor
    ):
        return Comparison(
            Status.MISMATCH,
            format_differences(
                {"sandbox_flavor": issued_flavor},
                {"sandbox_flavor": configured_flavor},
            ),
        )
    issued_fingerprint, issued_valid = _selection_fingerprint(issued)
    configured_fingerprint, configured_valid = _selection_fingerprint(configured)
    if not issued_valid or not configured_valid:
        return Comparison(Status.UNKNOWN)
    if issued_fingerprint is not None and configured_fingerprint is not None:
        issued_final = _final_selection_node(issued)
        configured_final = _final_selection_node(configured)
        if issued_final is None or configured_final is None:
            return Comparison(Status.UNKNOWN)
        if issued_final != configured_final:
            return Comparison(
                Status.MISMATCH,
                "final image: " + format_differences(issued_final, configured_final),
            )
        if issued_fingerprint == configured_fingerprint:
            return Comparison(Status.MATCH)
        return Comparison(
            Status.MISMATCH,
            format_differences(
                {"selection_fingerprint": issued_fingerprint},
                {"selection_fingerprint": configured_fingerprint},
            ),
        )
    left = _selection_projection(issued_reference, inspect_issued)
    right = _selection_projection(configured_reference, configured_inspector)
    if left is None or right is None:
        return Comparison(Status.UNKNOWN)
    if left == right:
        return Comparison(Status.MATCH)
    return Comparison(Status.MISMATCH, _projection_difference(left, right))

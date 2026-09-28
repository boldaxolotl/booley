"""Read-only identity comparison for issued Sandbox Images."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass
from enum import StrEnum

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


def decode_image_metadata(reference: str, document: object) -> ImageMetadata | None:
    """Decode one ``docker image inspect`` document without guessing."""
    if not isinstance(document, list) or len(document) != 1:
        return None
    record = document[0]
    if not isinstance(record, dict):
        return None
    image_id = record.get("Id")
    config = record.get("Config")
    if not isinstance(image_id, str) or not image_id or not isinstance(config, dict):
        return None
    raw_labels = config.get("Labels")
    if raw_labels is None:
        labels: dict[str, str] = {}
    elif (
        isinstance(raw_labels, dict)
        and len(raw_labels) <= _MAX_METADATA_ENTRIES
        and all(
            isinstance(key, str)
            and isinstance(value, str)
            and len(key) <= _MAX_METADATA_VALUE_LENGTH
            and len(value) <= _MAX_METADATA_VALUE_LENGTH
            for key, value in raw_labels.items()
        )
    ):
        labels = dict(raw_labels)
    else:
        return None
    raw_environment = config.get("Env")
    if raw_environment is None:
        environment: dict[str, str] = {}
    elif (
        isinstance(raw_environment, list)
        and len(raw_environment) <= _MAX_METADATA_ENTRIES
        and all(
            isinstance(item, str) and len(item) <= _MAX_METADATA_VALUE_LENGTH
            for item in raw_environment
        )
    ):
        environment = {
            key: value
            for item in raw_environment
            if "=" in item
            for key, value in (item.split("=", 1),)
        }
    else:
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


def compare_issued_build(image: str, *, executable: str = "docker") -> Comparison:
    """Compare an issued immutable image with the canonical host installation."""
    from booley.runtime.interactive_docker import inspect_image_metadata

    expected = current_host_build_identity()
    observed = inspect_image_metadata(image, executable=executable)
    if expected is None or observed is None:
        return Comparison(Status.UNKNOWN)
    return compare_build_identity(expected, build_identity(observed))


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
) -> str:
    """Fingerprint ordered node fields while deriving parent compatibility keys."""
    projection = []
    parent_key = ""
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
            return None
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
    issued_fingerprint = _available(issued.labels.get(LABEL_LOGICAL_SELECTION_FINGERPRINT))
    configured_fingerprint = _available(configured.labels.get(LABEL_LOGICAL_SELECTION_FINGERPRINT))
    if issued_fingerprint is not None and configured_fingerprint is not None:
        if issued_fingerprint == configured_fingerprint:
            return Comparison(Status.MATCH)
        return Comparison(
            Status.MISMATCH,
            format_differences(
                {"selection_fingerprint": issued_fingerprint},
                {"selection_fingerprint": configured_fingerprint},
            ),
        )
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
    left = _selection_projection(issued_reference, inspect_issued)
    right = _selection_projection(configured_reference, configured_inspector)
    if left is None or right is None:
        return Comparison(Status.UNKNOWN)
    if left == right:
        return Comparison(Status.MATCH)
    return Comparison(Status.MISMATCH, _projection_difference(left, right))


def compare_issued_selection(
    issued_reference: str,
    configured_reference: str,
    *,
    executable: str = "docker",
) -> Comparison:
    """Inspect and compare issued/configured image selection without mutation."""
    from booley.runtime.interactive_docker import inspect_image_metadata

    def inspect(reference: str) -> ImageMetadata | None:
        return inspect_image_metadata(reference, executable=executable)

    return compare_logical_selection(issued_reference, configured_reference, inspect)

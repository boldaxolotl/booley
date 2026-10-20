"""Read-only identity comparison for issued Sandbox Images."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path

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
    """Docker metadata for one exact image reference."""

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


def _available(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized or normalized.lower() == "unknown":
        return None
    return normalized


def decode_image_metadata(reference: str, document: object) -> ImageMetadata | None:
    """Decode one ``docker image inspect`` document without guessing."""
    try:
        records = require_list(document, field="docker image inspect response")
        if len(records) != 1:
            return None
        record = require_dict(records[0], field="docker image inspect record")
        image_id = require_str(record, "Id")
        config = require_dict(record.get("Config"), field="Config")
        raw_labels, raw_env = config.get("Labels"), config.get("Env")
        labels = {} if raw_labels is None else require_dict(raw_labels, field="Config.Labels")
        labels = {
            require_str_value(key, field="label name"): require_str_value(
                value, field=f"label {key!r}", allow_empty=True
            )
            for key, value in labels.items()
        }
        items = [] if raw_env is None else require_list(raw_env, field="Config.Env")
        items = [
            require_str_value(item, field="Config.Env item", allow_empty=True) for item in items
        ]
        environment = {
            key: value for item in items if "=" in item for key, value in (item.split("=", 1),)
        }
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
        node = {
            "role": role,
            "effective_inputs": effective_inputs,
            "recipe_fingerprint": recipe,
            "parent_compatibility_key": parent_key,
            "runtime_base_contract": runtime_contract,
            "standard_substrate_contract": standard_contract,
        }
        projection.append(node)
        parent_key = hashlib.sha256(
            json.dumps(node, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    document = {"selected_reference": selected_reference, "nodes": projection}
    return hashlib.sha256(
        json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _final_selection_node(metadata: ImageMetadata) -> dict[str, str] | None:
    labels = metadata.labels
    if labels.get(LABEL_SCHEMA) != PROVENANCE_SCHEMA:
        return None
    role = _available(labels.get(LABEL_ARTIFACT_ROLE))
    recipe = _available(labels.get(LABEL_RECIPE_FINGERPRINT))
    if role not in {"wheel-overlay", "project-overlay"} or recipe is None:
        return None
    fingerprint = labels.get(LABEL_LOGICAL_SELECTION_FINGERPRINT)
    if fingerprint is not None and _SHA256_HEX.fullmatch(fingerprint.strip()) is None:
        return None
    return {
        "selection_fingerprint": fingerprint.strip().lower() if fingerprint else "",
        "role": role,
        "sandbox_flavor": metadata.environment.get(ENV_SANDBOX_FLAVOR, "standard"),
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
    *,
    project_root: Path | None = None,
    executable: str = "docker",
) -> Comparison:
    """Compare selection identity while ignoring local IDs and wheel contents."""
    issued = _logical_metadata(issued_reference, inspect_issued, project_root, executable)
    configured = _logical_metadata(
        configured_reference, inspect_configured or inspect_issued, project_root, executable
    )
    if issued is None or configured is None:
        return Comparison(Status.UNKNOWN)
    left = _final_selection_node(issued)
    right = _final_selection_node(configured)
    if left is None or right is None:
        return Comparison(Status.UNKNOWN)
    issued_fingerprint = left.pop("selection_fingerprint")
    configured_fingerprint = right.pop("selection_fingerprint")
    if left != right:
        return Comparison(Status.MISMATCH, "final image: " + format_differences(left, right))
    field = "selection_fingerprint"
    left, right = issued_fingerprint, configured_fingerprint
    if not left and not right:
        if issued.labels.get(LABEL_PARENT_ARTIFACT_KIND) != PARENT_ARTIFACT_REGISTRY_DIGEST:
            return Comparison(Status.UNKNOWN)
        field = "registry_parent"
        left, right = (
            issued.labels.get(LABEL_PARENT_ARTIFACT),
            configured.labels.get(LABEL_PARENT_ARTIFACT),
        )
    if not left or not right:
        return Comparison(Status.UNKNOWN)
    if left == right:
        return Comparison(Status.MATCH)
    return Comparison(Status.MISMATCH, format_differences({field: left}, {field: right}))


def _logical_metadata(
    reference: str,
    inspect: Callable[[str], ImageMetadata | None],
    project_root: Path | None,
    executable: str,
) -> ImageMetadata | None:
    """Project a proven layout-only derivative onto its immutable logical parent."""
    metadata = inspect(reference)
    if metadata is None or metadata.labels.get(LABEL_ARTIFACT_ROLE) != "project-data-layout":
        return metadata
    from booley.runtime import project_image

    if project_root is None:
        return None
    try:
        parent_id = _layout_parent_id(metadata, project_root)
    except (OSError, ValueError, RuntimeError):
        parent_id = None
    if parent_id is None:
        return None
    labels = metadata.labels
    parent = inspect(parent_id)
    if (
        parent is None
        or parent.image_id != parent_id
        or parent.labels.get(LABEL_ARTIFACT_ROLE) == "project-data-layout"
        or labels.get(LABEL_LOGICAL_SELECTION_FINGERPRINT)
        != parent.labels.get(LABEL_LOGICAL_SELECTION_FINGERPRINT)
    ):
        return None
    try:
        project_image.verify_layout_parent_identity(metadata.image_id, parent_id, executable)
    except (RuntimeError, OSError, subprocess.SubprocessError):
        return None
    return parent


def _layout_parent_id(metadata: ImageMetadata, project_root: Path) -> str | None:
    from booley.runtime import project_image
    from booley.runtime.image_provenance import resolve_recipe_fingerprint
    from booley.runtime.paths import docker_data_dir

    labels = metadata.labels
    scope = labels.get(project_image.LABEL_LAYOUT_REFERENCE, "")
    inputs = labels.get(LABEL_EFFECTIVE_INPUTS, "")
    recipe = resolve_recipe_fingerprint((docker_data_dir() / "Dockerfile.project-data-layout",))
    parent_id = labels.get(LABEL_PARENT_ARTIFACT, "")
    if (
        labels.get(LABEL_SCHEMA) != PROVENANCE_SCHEMA
        or labels.get(LABEL_PARENT_ARTIFACT_KIND) != PARENT_ARTIFACT_LOCAL_IMAGE_ID
        or labels.get(LABEL_RECIPE_FINGERPRINT) != recipe
        or _SHA256_HEX.fullmatch(inputs) is None
        or not scope.endswith(inputs[:16])
        or re.fullmatch(r"sha256:[0-9a-f]{64}", parent_id) is None
        or re.fullmatch(r"sha256:[0-9a-f]{64}", metadata.image_id) is None
    ):
        return None
    expected_reference, expected_inputs = project_image.layout_identity(
        project_root, parent_id, recipe
    )
    if scope != expected_reference or inputs != expected_inputs:
        return None
    return parent_id

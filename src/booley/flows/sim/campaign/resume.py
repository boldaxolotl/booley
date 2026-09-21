"""Read-only validation of exact Simulation Campaign resume manifests."""

from __future__ import annotations

import os
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from booley.targets.catalog import TargetCatalog
from booley.targets.domain import TargetHandle

from .codec import (
    MANIFEST_MAX_BYTES,
    SimulationCampaignIntegrityError,
    decode_simulation_campaign_manifest,
    encode_simulation_campaign_manifest,
)
from .model import SimulationCampaignManifest
from .planning import manifest_digest

_MAX_PREREQUISITES = 1_000


@dataclass(frozen=True, slots=True)
class ValidatedManifestNode:
    """One canonical decoded manifest in an exact prerequisite graph."""

    path: Path
    manifest: SimulationCampaignManifest
    sha256: str


@dataclass(frozen=True, slots=True)
class ValidatedTargetBinding:
    """One manifest node bound to the catalog snapshot for its exact revision."""

    node: ValidatedManifestNode
    project_root: Path
    handle: TargetHandle


@dataclass(frozen=True, slots=True)
class ValidatedResumeManifest:
    """Bounded read-only value passed unchanged from authorization to campaign."""

    candidate: ValidatedManifestNode
    prerequisites: tuple[ValidatedManifestNode, ...]
    target_handles: tuple[TargetHandle, ...]
    bindings: tuple[ValidatedTargetBinding, ...] = ()

    @property
    def path(self) -> Path:
        return self.candidate.path

    @property
    def manifest(self) -> SimulationCampaignManifest:
        return self.candidate.manifest

    @property
    def sha256(self) -> str:
        return self.candidate.sha256

    def binding_for(self, manifest: SimulationCampaignManifest) -> ValidatedTargetBinding | None:
        """Return the authorization-time binding for one exact manifest value."""
        digest = manifest_digest(manifest)
        return next((item for item in self.bindings if item.node.sha256 == digest), None)

    def prerequisite_for(self, reference: Mapping[str, object]) -> ValidatedManifestNode:
        """Select the already-authenticated prerequisite named by *reference*."""
        expected = cast(str, reference["sha256"])
        invocation = _origin_invocation(self.candidate.path)
        expected_path = (invocation / cast(str, reference["path"])).absolute()
        matches = tuple(
            node
            for node in self.prerequisites
            if node.sha256 == expected and node.path == expected_path
        )
        if len(matches) != 1:
            raise SimulationCampaignIntegrityError(
                "resume prerequisite does not select one authenticated manifest"
            )
        return matches[0]


def validate_resume_manifest(
    manifest_path: Path,
    *,
    project_root: Path,
    revision_root: Callable[[Mapping[str, str]], Path] | None = None,
) -> ValidatedResumeManifest:
    """Decode one manifest exactly once and traverse only authenticated links."""
    canonical = manifest_path.absolute()
    if canonical.name != "manifest.json" or canonical.parent.name != "campaign":
        raise SimulationCampaignIntegrityError(
            "--resume-from must name one exact campaign/manifest.json"
        )
    candidate = _load_node(canonical)
    invocation = _origin_invocation(canonical)
    visited = {canonical}
    prerequisites: list[ValidatedManifestNode] = []
    _walk_prerequisites(candidate, invocation, visited, prerequisites)
    if len(prerequisites) > _MAX_PREREQUISITES:
        raise SimulationCampaignIntegrityError("manifest prerequisite limit exceeded")
    root_for = revision_root or (lambda _target: project_root)
    handles: list[TargetHandle] = []
    bindings: list[ValidatedTargetBinding] = []
    seen: set[tuple[str, str]] = set()
    for node in (candidate, *prerequisites):
        target = cast(Mapping[str, str], node.manifest.document["target"])
        root = Path(root_for(target)).resolve()
        handle = TargetCatalog.build(root).select(target["selector"], for_flow="sim")
        if handle.identity != target["vlnv"] + "#" + target["name"]:
            raise SimulationCampaignIntegrityError(
                f"resolved Target identity disagrees with manifest: {target['selector']}"
            )
        identity = (handle.identity, target["revision"])
        if identity not in seen:
            handles.append(handle)
            seen.add(identity)
        bindings.append(ValidatedTargetBinding(node, root, handle))
    return ValidatedResumeManifest(
        candidate, tuple(prerequisites), tuple(handles), tuple(bindings)
    )


def _walk_prerequisites(
    node: ValidatedManifestNode,
    invocation: Path,
    visited: set[Path],
    result: list[ValidatedManifestNode],
) -> None:
    entries = cast(tuple[Mapping[str, object], ...], node.manifest.document["prerequisites"])
    for entry in entries:
        reference = cast(Mapping[str, object], entry["manifest"])
        linked = (invocation / cast(str, reference["path"])).absolute()
        try:
            linked.relative_to(invocation)
        except ValueError as exc:
            raise SimulationCampaignIntegrityError(
                "prerequisite manifest escapes invocation"
            ) from exc
        if linked in visited:
            raise SimulationCampaignIntegrityError("duplicate or cyclic prerequisite manifest")
        visited.add(linked)
        loaded = _load_node(linked)
        raw = encode_simulation_campaign_manifest(loaded.manifest)
        expected_digest = cast(str, reference["sha256"])
        if len(raw) != reference["bytes"] or loaded.sha256 != expected_digest:
            raise SimulationCampaignIntegrityError(
                f"prerequisite manifest reference does not authenticate {linked}"
            )
        if loaded.manifest.document["campaign_id"] != entry["campaign_id"]:
            raise SimulationCampaignIntegrityError("prerequisite campaign identity disagrees")
        if loaded.manifest.document["target"] != entry["target"]:
            raise SimulationCampaignIntegrityError("prerequisite Target identity disagrees")
        result.append(loaded)
        if len(result) > _MAX_PREREQUISITES:
            raise SimulationCampaignIntegrityError("manifest prerequisite limit exceeded")
        _walk_prerequisites(loaded, invocation, visited, result)


def _origin_invocation(path: Path) -> Path:
    if len(path.parents) < 4 or path.parents[2].name != "targets":
        raise SimulationCampaignIntegrityError("manifest is outside an invocation Target path")
    return path.parents[3]


def _load_node(path: Path) -> ValidatedManifestNode:
    raw = _read_regular(path)
    manifest = decode_simulation_campaign_manifest(raw)
    return ValidatedManifestNode(path, manifest, manifest_digest(manifest))


def _read_regular(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise SimulationCampaignIntegrityError(
            f"cannot read resume manifest {path}: {exc}"
        ) from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MANIFEST_MAX_BYTES:
            raise SimulationCampaignIntegrityError("resume manifest is not a bounded regular file")
        raw = os.read(descriptor, MANIFEST_MAX_BYTES + 1)
        if len(raw) != info.st_size:
            raise SimulationCampaignIntegrityError("resume manifest changed while being read")
        return raw
    finally:
        os.close(descriptor)


__all__ = [
    "ValidatedManifestNode",
    "ValidatedResumeManifest",
    "ValidatedTargetBinding",
    "validate_resume_manifest",
]

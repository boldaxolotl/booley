"""Read-only validation of exact Simulation Record resume manifests."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from booley.core.booley_dir_links import lexical_booley_path
from booley.core.sandbox_layout import canonical_project_alias_path
from booley.flows.artifact_durability import discard_published_staging
from booley.flows.baseline_worktree import git_full_sha
from booley.flows.sim.record_files import read_bounded
from booley.flows.sim.record_layout import RECORD_DIRECTORY, refuse_older_manifest_path
from booley.flows.sim.record_reports import simulation_booley_dir
from booley.targets.catalog import TargetCatalog
from booley.targets.domain import TargetHandle

from .codec import (
    MANIFEST_MAX_BYTES,
    SimulationRecordIntegrityError,
    UnsupportedSimulationRecordFormatError,
    decode_simulation_record_manifest,
)
from .model import SimulationRecordManifest
from .planning import manifest_digest


@dataclass(frozen=True, slots=True)
class ValidatedManifestNode:
    """One canonical decoded manifest."""

    path: Path
    manifest: SimulationRecordManifest
    sha256: str


@dataclass(frozen=True, slots=True)
class ValidatedResumeManifest:
    """Bounded read-only value passed unchanged from authorization to record."""

    candidate: ValidatedManifestNode
    target_handles: tuple[TargetHandle, ...]
    # The one symlinked Booley Directory entry this manifest was read through (#2191).
    booley_dir: Path | None = None

    @property
    def path(self) -> Path:
        return self.candidate.path

    @property
    def manifest(self) -> SimulationRecordManifest:
        return self.candidate.manifest

    @property
    def sha256(self) -> str:
        return self.candidate.sha256


def validate_resume_manifest(
    manifest_path: Path, *, project_root: Path
) -> ValidatedResumeManifest:
    """Decode one manifest exactly once and bind its Target at the recorded revision."""
    try:
        canonical = canonical_project_alias_path(manifest_path.absolute())
    except ValueError as exc:
        raise SimulationRecordIntegrityError(str(exc)) from exc
    refuse_older_manifest_path(canonical)
    if canonical.name != "manifest.json" or canonical.parent.name != RECORD_DIRECTORY:
        raise SimulationRecordIntegrityError(
            "--resume-from must name one exact record/manifest.json"
        )
    booley_dir = simulation_booley_dir(project_root)
    # A copied physical path names the same lexical origin, so receipts stay consistent.
    canonical = lexical_booley_path(booley_dir, canonical)
    candidate = _load_node(canonical, booley_dir)
    if candidate.manifest.document["prerequisites"]:
        raise SimulationRecordIntegrityError(
            "this Simulation Record measured Cycle Counts against a baseline revision, "
            "which resume no longer runs; start a new run instead"
        )
    _origin_invocation(canonical)  # refuses a manifest outside an invocation Target path
    target = cast(Mapping[str, str], candidate.manifest.document["target"])
    root = project_root.resolve()
    _verify_revision(root, target)
    handle = TargetCatalog.build(root).select(target["selector"], for_flow="sim")
    if handle.identity != target["vlnv"] + "#" + target["name"]:
        raise SimulationRecordIntegrityError(
            f"resolved Target identity disagrees with manifest: {target['selector']}"
        )
    return ValidatedResumeManifest(candidate, (handle,), booley_dir)


def _verify_revision(root: Path, target: Mapping[str, str]) -> None:
    actual = git_full_sha("HEAD", root)
    if actual is None:
        raise SimulationRecordIntegrityError(f"cannot verify resume Target revision at {root}")
    if actual != target["revision"]:
        raise SimulationRecordIntegrityError(
            "resume Target checkout revision disagrees with manifest: "
            f"{target['selector']} expected {target['revision']}, got {actual}"
        )


def _origin_invocation(path: Path) -> Path:
    if len(path.parents) < 4 or path.parents[2].name != "targets":
        raise SimulationRecordIntegrityError("manifest is outside an invocation Target path")
    return path.parents[3]


def _load_node(path: Path, booley_dir: Path | None) -> ValidatedManifestNode:
    discard_published_staging(path)
    raw = read_bounded(
        path, limit=MANIFEST_MAX_BYTES, error=SimulationRecordIntegrityError, booley_dir=booley_dir
    )
    try:
        manifest = decode_simulation_record_manifest(raw)
    except UnsupportedSimulationRecordFormatError as exc:
        raise exc.located(_origin_invocation(path)) from None
    return ValidatedManifestNode(path, manifest, manifest_digest(manifest))


__all__ = ["ValidatedManifestNode", "ValidatedResumeManifest", "validate_resume_manifest"]

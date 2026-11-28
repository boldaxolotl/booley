"""Profile-aware mechanics for Simulation Record artifact references."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import cast

from booley.flows.sim.coverage_json import content_digest
from booley.flows.sim.record_files import read_bounded
from booley.flows.sim.record_reports import is_report_link

_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


class ArtifactReferenceError(ValueError):
    """A typed artifact reference is malformed or fails authentication."""


@dataclass(frozen=True, slots=True)
class ArtifactReference:
    """One immutable, typed reference to a digest-bound artifact."""

    path_base: str
    path: str
    bytes: int
    sha256: str
    kind: str
    owner: str


@dataclass(frozen=True, slots=True)
class ResolvedArtifact:
    """One authenticated regular file selected below a named base."""

    path: Path
    raw: bytes


def build_artifact_reference(
    path: Path,
    *,
    base_name: str,
    base: Path,
    kind: str,
    owner: str,
    maximum: int,
    booley_dir: Path | None = None,
) -> ArtifactReference:
    """Build a typed reference after authenticating an already published file."""
    base = base.absolute()
    absolute = path.absolute()
    try:
        relative = absolute.relative_to(base).as_posix()
    except ValueError as exc:
        raise ArtifactReferenceError("artifact is outside its declared base") from exc
    contained = _contained_path(base, relative)
    raw = read_bounded(
        contained, limit=maximum, error=ArtifactReferenceError, booley_dir=booley_dir
    )
    return _artifact_reference(
        path_base=base_name,
        path=relative,
        bytes_=len(raw),
        sha256=content_digest(raw),
        kind=kind,
        owner=owner,
    )


def encode_artifact_reference(reference: ArtifactReference) -> dict[str, object]:
    """Encode one validated artifact reference for JSON persistence."""
    return {
        "path_base": reference.path_base,
        "path": reference.path,
        "bytes": reference.bytes,
        "sha256": reference.sha256,
        "kind": reference.kind,
        "owner": reference.owner,
    }


def resolve_artifact_reference(
    value: object,
    *,
    bases: Mapping[str, Path],
    expected_kind: str,
    expected_owner: str,
    maximum: int,
    booley_dir: Path | None = None,
) -> ResolvedArtifact:
    """Resolve a recorded reference below its named base and match its bytes.

    The reference mapping is trusted as recorded; a malformed one fails at use.
    """
    reference = cast(Mapping[str, object], value)
    if reference["kind"] != expected_kind or reference["owner"] != expected_owner:
        raise ArtifactReferenceError("artifact kind or owner disagrees")
    try:
        base = bases[cast(str, reference["path_base"])].absolute()
    except KeyError as exc:
        raise ArtifactReferenceError("artifact base is unavailable") from exc
    path = base / cast(str, reference["path"])
    raw = read_bounded(path, limit=maximum, error=ArtifactReferenceError, booley_dir=booley_dir)
    if len(raw) != reference["bytes"] or content_digest(raw) != reference["sha256"]:
        raise ArtifactReferenceError("artifact bytes disagree with reference")
    return ResolvedArtifact(path, raw)


def _artifact_reference(
    *, path_base: object, path: object, bytes_: object, sha256: object, kind: object, owner: object
) -> ArtifactReference:
    base = _nonempty(path_base, "path_base")
    relative = str(path) if isinstance(path, str) else ""
    _relative(relative)
    if type(bytes_) is not int or bytes_ < 1:
        raise ArtifactReferenceError("artifact byte count is invalid")
    if not isinstance(sha256, str) or not _DIGEST.fullmatch(sha256):
        raise ArtifactReferenceError("artifact digest is invalid")
    return ArtifactReference(
        base,
        relative,
        bytes_,
        sha256,
        _nonempty(kind, "kind"),
        _nonempty(owner, "owner"),
    )


def _relative(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if (
        not value
        or "\\" in value
        or "\x00" in value
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.as_posix() != value
    ):
        raise ArtifactReferenceError("artifact path is not normalized")
    return path


def _contained_path(base: Path, relative: str) -> Path:
    path = base.joinpath(*_relative(relative).parts)
    try:
        path.resolve().relative_to(base.resolve())
    except ValueError as exc:
        raise ArtifactReferenceError("artifact path escapes its declared base") from exc
    current = path
    while True:
        if is_report_link(current):
            raise ArtifactReferenceError("artifact path contains a link")
        if current == base:
            break
        if current == current.parent:
            raise ArtifactReferenceError("artifact path does not reach its declared base")
        current = current.parent
    return path


def _nonempty(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or len(value.encode()) > 4096:
        raise ArtifactReferenceError(f"artifact {field} is invalid")
    return value


__all__ = [
    "ArtifactReference",
    "ArtifactReferenceError",
    "ResolvedArtifact",
    "build_artifact_reference",
    "encode_artifact_reference",
    "resolve_artifact_reference",
]

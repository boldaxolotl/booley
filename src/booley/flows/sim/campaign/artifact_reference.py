"""Profile-aware mechanics for Campaign-owned relative artifact references."""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Mapping, Set
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from booley.flows.sim.campaign_reports import is_report_link
from booley.runtime.regular_file import open_regular_nofollow

_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_FIELDS = {"path_base", "path", "bytes", "sha256", "kind", "owner"}


class ArtifactReferenceError(ValueError):
    """A typed artifact reference is malformed or fails authentication."""


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
) -> dict[str, object]:
    """Build a typed reference after authenticating an already published file."""
    absolute = path.absolute()
    try:
        relative = absolute.relative_to(base.absolute()).as_posix()
    except ValueError as exc:
        raise ArtifactReferenceError("artifact is outside its declared base") from exc
    raw = _read_regular(absolute, maximum)
    return {
        "path_base": base_name,
        "path": relative,
        "bytes": len(raw),
        "sha256": _digest(raw),
        "kind": _nonempty(kind, "kind"),
        "owner": _nonempty(owner, "owner"),
    }


def resolve_artifact_reference(
    value: object,
    *,
    bases: Mapping[str, Path],
    allowed_bases: Set[str],
    expected_kind: str,
    expected_owner: str,
    maximum: int,
) -> ResolvedArtifact:
    """Resolve and authenticate a reference under one profile's exact bases."""
    reference = _reference(value, allowed_bases)
    if reference["kind"] != expected_kind or reference["owner"] != expected_owner:
        raise ArtifactReferenceError("artifact kind or owner disagrees")
    base_name = str(reference["path_base"])
    try:
        base = bases[base_name].absolute()
    except KeyError as exc:
        raise ArtifactReferenceError("artifact base is unavailable") from exc
    path = _contained_path(base, str(reference["path"]))
    raw = _read_regular(path, maximum)
    if len(raw) != reference["bytes"] or _digest(raw) != reference["sha256"]:
        raise ArtifactReferenceError("artifact bytes disagree with reference")
    return ResolvedArtifact(path, raw)


def resolve_report_artifact_reference(
    report_path: Path,
    value: object,
    *,
    expected_kind: str,
    expected_owner: str,
    maximum: int,
) -> ResolvedArtifact:
    """Resolve a report reference from the location of its containing document."""
    invocation = report_path.absolute().parent
    if report_path.name != "report.json" or not invocation.name.isdecimal():
        raise ArtifactReferenceError("expected a numbered invocation report.json")
    return resolve_artifact_reference(
        value,
        bases={
            "report_invocation": invocation,
            "reports_root": invocation.parent.parent,
        },
        allowed_bases={"report_invocation", "reports_root"},
        expected_kind=expected_kind,
        expected_owner=expected_owner,
        maximum=maximum,
    )


def _reference(value: object, allowed_bases: Set[str]) -> Mapping[str, object]:
    if not isinstance(value, dict) or set(value) != _FIELDS:
        raise ArtifactReferenceError("artifact reference fields are invalid")
    base = _nonempty(value["path_base"], "path_base")
    if base not in allowed_bases:
        raise ArtifactReferenceError("artifact reference base is not allowed")
    _relative(str(value["path"]) if isinstance(value["path"], str) else "")
    size = value["bytes"]
    if type(size) is not int or size < 1:
        raise ArtifactReferenceError("artifact byte count is invalid")
    if not isinstance(value["sha256"], str) or not _DIGEST.fullmatch(value["sha256"]):
        raise ArtifactReferenceError("artifact digest is invalid")
    _nonempty(value["kind"], "kind")
    _nonempty(value["owner"], "owner")
    return value


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


def _read_regular(path: Path, maximum: int) -> bytes:
    try:
        descriptor = open_regular_nofollow(path)
        with os.fdopen(descriptor, "rb") as stream:
            raw = stream.read(maximum + 1)
    except OSError as exc:
        raise ArtifactReferenceError(f"artifact is not a regular file: {path}") from exc
    if len(raw) > maximum:
        raise ArtifactReferenceError("artifact exceeds size limit")
    return raw


def _nonempty(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or len(value.encode()) > 4096:
        raise ArtifactReferenceError(f"artifact {field} is invalid")
    return value


def _digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


__all__ = [
    "ArtifactReferenceError",
    "ResolvedArtifact",
    "build_artifact_reference",
    "resolve_artifact_reference",
    "resolve_report_artifact_reference",
]

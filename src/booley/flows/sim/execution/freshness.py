"""Artifact containment and freshness policy for Simulation attempts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class ArtifactValidationError(RuntimeError):
    """A reported Simulation artifact is unsafe, missing, or stale."""


class ArtifactFailure(StrEnum):
    """Why :func:`validate_fresh_artifact` rejected an artifact."""

    MISSING = "missing"
    OUTSIDE_ROOTS = "outside_roots"
    NOT_REGULAR_FILE = "not_regular_file"
    STALE = "stale"


class ArtifactFreshnessError(ArtifactValidationError):
    """Raised by :func:`validate_fresh_artifact`; ``cause`` names the failed check."""

    def __init__(self, message: str, *, cause: ArtifactFailure) -> None:
        super().__init__(message)
        self.cause = cause


@dataclass(frozen=True)
class ArtifactStamp:
    """Filesystem identity captured before an execution attempt."""

    device: int
    inode: int
    size: int
    modified_ns: int


@dataclass(frozen=True)
class ArtifactEvidence:
    """Validated immutable evidence for one current-attempt artifact."""

    path: Path
    size: int


def snapshot_artifact(path: Path) -> ArtifactStamp | None:
    """Capture *path* without following a missing artifact."""
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return ArtifactStamp(stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def validate_fresh_artifact(
    path: Path,
    *,
    roots: tuple[Path, ...],
    before: ArtifactStamp | None,
    explicitly_allowed: tuple[Path, ...] = (),
) -> ArtifactEvidence:
    """Validate identity, containment, regular-file status, and freshness."""
    try:
        resolved = path.resolve(strict=True)
    except FileNotFoundError as exc:
        raise ArtifactFreshnessError(
            f"Simulation artifact is missing: {path}", cause=ArtifactFailure.MISSING
        ) from exc
    resolved_roots = tuple(root.resolve() for root in roots)
    explicit = tuple(candidate.resolve() for candidate in explicitly_allowed)
    if not any(_within(resolved, root) for root in resolved_roots) and resolved not in explicit:
        raise ArtifactFreshnessError(
            f"Simulation artifact escapes its allowed roots: {path}",
            cause=ArtifactFailure.OUTSIDE_ROOTS,
        )
    if not resolved.is_file():
        raise ArtifactFreshnessError(
            f"Simulation artifact is not a regular file: {path}",
            cause=ArtifactFailure.NOT_REGULAR_FILE,
        )
    current = snapshot_artifact(resolved)
    assert current is not None
    if before is not None and current == before:
        raise ArtifactFreshnessError(
            f"Simulation artifact is stale: {path}", cause=ArtifactFailure.STALE
        )
    return ArtifactEvidence(path=resolved, size=current.size)


__all__ = [
    "ArtifactEvidence",
    "ArtifactFailure",
    "ArtifactFreshnessError",
    "ArtifactStamp",
    "ArtifactValidationError",
    "snapshot_artifact",
    "validate_fresh_artifact",
]

"""Versioned integrity contract for inputs supplied to the triage reviewer."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

REVIEW_EVIDENCE_VERSION = 1
REQUIRED_REVIEW_INPUTS = frozenset({"ticket", "diff", "commits", "files", "status"})


class ReviewEvidenceError(ValueError):
    """Review evidence is incomplete or changed after it was collected."""


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def evidence_digest(path: Path) -> str:
    """Hash one regular file or a directory tree without following symlinks."""
    if path.is_symlink() or not path.exists():
        raise ReviewEvidenceError(f"review evidence path is missing or a symlink: {path}")
    if path.is_file():
        return _file_digest(path)
    if not path.is_dir():
        raise ReviewEvidenceError(f"review evidence path is not a file or directory: {path}")

    digest = hashlib.sha256()
    for child in sorted(path.rglob("*")):
        if child.is_symlink():
            raise ReviewEvidenceError(f"review evidence tree contains a symlink: {child}")
        if not child.is_file():
            continue
        digest.update(child.relative_to(path).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(_file_digest(child).encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


@dataclass(frozen=True)
class ReviewEvidenceItem:
    """One named evidence input and its expected content digest."""

    name: str
    kind: str
    sha256: str

    def as_dict(self) -> dict[str, str]:
        """Return the stable persisted representation."""
        return {"name": self.name, "kind": self.kind, "sha256": self.sha256}


@dataclass(frozen=True)
class ReviewEvidenceManifest:
    """Identity and contents of one immutable reviewer evidence package."""

    slug: str
    base_sha: str
    head_sha: str
    source_sha256: str
    items: tuple[ReviewEvidenceItem, ...]
    version: int = REVIEW_EVIDENCE_VERSION

    def as_dict(self) -> dict[str, Any]:
        """Return the stable JSON-compatible representation."""
        return {
            "version": self.version,
            "slug": self.slug,
            "base_sha": self.base_sha,
            "head_sha": self.head_sha,
            "source_sha256": self.source_sha256,
            "items": [item.as_dict() for item in self.items],
        }


@dataclass(frozen=True)
class ReviewEvidencePackage:
    """A manifest paired with the local sources it authenticates."""

    manifest: ReviewEvidenceManifest
    sources: Mapping[str, Path]

    def verify(self, sources: Mapping[str, Path] | None = None) -> None:
        """Raise when a named input is missing, added, or has changed content."""
        actual = self.sources if sources is None else sources
        expected_names = {item.name for item in self.manifest.items}
        if set(actual) != expected_names:
            raise ReviewEvidenceError(
                "review evidence names changed: "
                f"expected {sorted(expected_names)}, got {sorted(actual)}"
            )
        for item in self.manifest.items:
            path = actual[item.name]
            if evidence_digest(path) != item.sha256:
                raise ReviewEvidenceError(f"review evidence changed after collection: {item.name}")


def build_review_evidence(
    *,
    slug: str,
    base_sha: str,
    head_sha: str,
    source_sha256: str,
    sources: Mapping[str, Path],
) -> ReviewEvidencePackage:
    """Validate named inputs and return their immutable, versioned contract."""
    missing = REQUIRED_REVIEW_INPUTS - set(sources)
    if missing:
        raise ReviewEvidenceError(f"review evidence is missing required inputs: {sorted(missing)}")
    normalized = {name: Path(path) for name, path in sources.items()}
    items = tuple(
        ReviewEvidenceItem(
            name=name,
            kind="file" if path.is_file() else "directory",
            sha256=evidence_digest(path),
        )
        for name, path in sorted(normalized.items())
    )
    manifest = ReviewEvidenceManifest(
        slug=slug,
        base_sha=base_sha,
        head_sha=head_sha,
        source_sha256=source_sha256,
        items=items,
    )
    return ReviewEvidencePackage(manifest, MappingProxyType(normalized))

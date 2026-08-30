"""Shared Session Image provenance schema and canonical recipe hashing."""

from __future__ import annotations

import hashlib
import re
import stat
from collections.abc import Iterable, Mapping
from pathlib import Path

PROVENANCE_SCHEMA = "2"
LEGACY_PROVENANCE_SCHEMA = "1"
LABEL_SCHEMA = "io.booley.provenance.schema"
LABEL_PAYLOAD_FINGERPRINT = "io.booley.payload.fingerprint"
LABEL_RECIPE_FINGERPRINT = "io.booley.build.recipe-fingerprint"
LABEL_PARENT_ARTIFACT = "io.booley.build.parent-artifact"
LABEL_PARENT_ARTIFACT_KIND = "io.booley.build.parent-artifact-kind"
LABEL_BUILD_ORIGIN = "io.booley.build.origin"
LABEL_VERSION = "org.opencontainers.image.version"
LEGACY_FINGERPRINT_LABEL = "booley.build-fingerprint"
PARENT_ARTIFACT_LOCAL_IMAGE_ID = "local-image-id"
PARENT_ARTIFACT_REGISTRY_DIGEST = "registry-digest"

_LOCAL_IMAGE_ID_RE = re.compile(r"sha256:[0-9a-f]{64}")
_REGISTRY_DIGEST_RE = re.compile(r"(?P<repository>[^\s@]+)@(?P<digest>sha256:[0-9a-fA-F]{64})")


class ImageProvenanceError(RuntimeError):
    """A required provenance input could not be read exactly."""


def is_local_image_id(value: str) -> bool:
    """Whether *value* is one full Docker image ID."""
    return _LOCAL_IMAGE_ID_RE.fullmatch(value) is not None


def normalize_registry_digest(value: str) -> str | None:
    """Return one canonical digest-qualified image reference, if valid."""
    match = _REGISTRY_DIGEST_RE.fullmatch(value)
    if match is None:
        return None
    return f"{match.group('repository')}@{match.group('digest').lower()}"


def _read_input(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise ImageProvenanceError(f"could not read provenance input {path}: {exc}") from exc


def resolve_recipe_fingerprint(paths: Iterable[Path]) -> str:
    """Return the canonical fingerprint for one ordered image-build recipe."""
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.name.encode())
        digest.update(b"\0")
        digest.update(_read_input(path))
        digest.update(b"\0")
    return digest.hexdigest()


def resolve_build_context_fingerprint(
    root: Path, overrides: Mapping[str, bytes] | None = None
) -> str:
    """Fingerprint every regular file in a Docker build context."""
    digest = hashlib.sha256()
    if not root.is_dir():
        if overrides is None:
            raise ImageProvenanceError(f"provenance build context is not a directory: {root}")
        contents: dict[str, bytes] = {}
    else:
        try:
            discovered = tuple(root.rglob("*"))
        except OSError as exc:
            raise ImageProvenanceError(
                f"could not enumerate provenance context {root}: {exc}"
            ) from exc
        paths = []
        for path in discovered:
            try:
                mode = path.stat().st_mode
            except OSError as exc:
                raise ImageProvenanceError(
                    f"could not inspect provenance input {path}: {exc}"
                ) from exc
            if stat.S_ISREG(mode):
                paths.append(path)
        contents = {path.relative_to(root).as_posix(): _read_input(path) for path in paths}
    contents.update(overrides or {})
    for relative, content in sorted(contents.items()):
        digest.update(relative.encode())
        digest.update(b"\0")
        digest.update(content)
        digest.update(b"\0")
    return digest.hexdigest()

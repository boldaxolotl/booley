"""Shared Session Image provenance schema and canonical recipe hashing."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from pathlib import Path

PROVENANCE_SCHEMA = "1"
LABEL_SCHEMA = "io.booley.provenance.schema"
LABEL_PAYLOAD_FINGERPRINT = "io.booley.payload.fingerprint"
LABEL_RECIPE_FINGERPRINT = "io.booley.build.recipe-fingerprint"
LABEL_PARENT_ARTIFACT = "io.booley.build.parent-artifact"
LABEL_BUILD_ORIGIN = "io.booley.build.origin"
LABEL_VERSION = "org.opencontainers.image.version"
LEGACY_FINGERPRINT_LABEL = "booley.build-fingerprint"


def resolve_recipe_fingerprint(paths: Iterable[Path]) -> str:
    """Return the canonical fingerprint for one ordered image-build recipe."""
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.name.encode())
        digest.update(b"\0")
        try:
            digest.update(path.read_bytes())
        except OSError:
            digest.update(b"<missing>")
        digest.update(b"\0")
    return digest.hexdigest()


def resolve_build_context_fingerprint(
    root: Path, overrides: Mapping[str, bytes] | None = None
) -> str:
    """Fingerprint every regular file in a Docker build context."""
    digest = hashlib.sha256()
    try:
        contents = {
            path.relative_to(root).as_posix(): path.read_bytes()
            for path in root.rglob("*")
            if path.is_file()
        }
    except OSError:
        contents = {}
    contents.update(overrides or {})
    for relative, content in sorted(contents.items()):
        digest.update(relative.encode())
        digest.update(b"\0")
        digest.update(content)
        digest.update(b"\0")
    return digest.hexdigest()

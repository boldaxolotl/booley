"""Shared file, JSON and digest helpers for Simulation Records and Coverage Records.

Every bounded record read, canonical-JSON object read, sha256 helper and streaming
hash of the record modules lives here so each rule is stated once.  The module
imports nothing from ``booley.flows.sim.record`` (whose package ``__init__`` pulls
in the coordinator), so the Coverage Record reference can use it without a cycle.
Callers name the domain error type each refusal raises.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

from booley.core.boundary import BoundaryError, require_dict
from booley.core.file_hash import file_sha256_and_size
from booley.flows.sim.coverage_json import REFERENCE_JSON, SHA256_PREFIX, content_digest
from booley.flows.sim.record_reports import is_report_link
from booley.runtime.regular_file import open_regular_nofollow

_CHUNK_BYTES = 64 * 1024


def canonical_json(value: object) -> bytes:
    """Encode finite JSON as the canonical record form (sorted, compact, trailing newline).

    Mappings and tuples are thawed to plain objects first.  Raises ``ValueError`` for a
    value that is not JSON (a YAML date, say) or is a non-finite number.
    """
    try:
        return REFERENCE_JSON.encode(value)
    except TypeError as exc:
        raise ValueError(str(exc)) from exc


def document_digest(value: object) -> str:
    """Digest a JSON document as its canonical bytes without the trailing newline."""
    return content_digest(canonical_json(value)[:-1])


def stream_digest(path: Path) -> tuple[int, str]:
    """Return ``(size, evidence digest)`` of a file read in chunks.

    The caller decides beforehand what kind of file *path* may be.
    """
    digest, size = file_sha256_and_size(path)
    return size, SHA256_PREFIX + digest


def read_bounded(
    path: Path,
    *,
    limit: int,
    error: type[Exception],
    booley_dir: Path | None = None,
) -> bytes:
    """Read a regular file of at most *limit* bytes reached without links.

    Only the *booley_dir* entry may be a followed link.  A file that grows past
    *limit* while it is read is refused.  Every refusal raises *error*.
    """
    descriptor: int | None = None
    try:
        # Refuse a link or FIFO before opening: open would follow or block.
        if is_report_link(path) or not stat.S_ISREG(path.lstat().st_mode):
            raise error(f"not a regular file reached without links: {path}")
        descriptor = open_regular_nofollow(path, booley_dir=booley_dir)
        if os.fstat(descriptor).st_size > limit:
            raise error(f"file exceeds size limit: {path}")
        raw = bytearray()
        while len(raw) <= limit:
            chunk = os.read(descriptor, min(_CHUNK_BYTES, limit + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
        if len(raw) > limit:
            raise error(f"file exceeds size limit: {path}")
        return bytes(raw)
    except OSError as exc:
        raise error(f"not a regular file reached without links: {path}: {exc}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def parse_json_object(
    raw: bytes, *, error: type[Exception], canonical: bool = False
) -> dict[str, object]:
    """Parse UTF-8 JSON that must be an object, canonical bytes when *canonical*."""
    try:
        document = require_dict(json.loads(raw.decode("utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError, BoundaryError) as exc:
        raise error(f"not a JSON object: {exc}") from exc
    if canonical:
        try:
            matches = canonical_json(document) == raw
        except ValueError as exc:
            raise error(f"not canonical JSON: {exc}") from exc
        if not matches:
            raise error("not canonical JSON")
    return document


def read_json_object(
    path: Path,
    *,
    limit: int,
    error: type[Exception],
    booley_dir: Path | None = None,
    canonical: bool = False,
) -> dict[str, object]:
    """Read a bounded file with ``read_bounded`` and parse it as one JSON object."""
    raw = read_bounded(path, limit=limit, error=error, booley_dir=booley_dir)
    return parse_json_object(raw, error=error, canonical=canonical)

"""Authenticate Campaign artifact references for standalone QA validators.

Keep this helper standard-library-only so retained evidence can be checked
without installing the Booley build under test.
"""

from __future__ import annotations

import hashlib
from pathlib import Path


def _need(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _file_digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def validate_manifest_reference(
    reference: object,
    *,
    projection_path: Path,
    manifest_path: Path,
    manifest_raw: bytes,
    campaign_id: object,
) -> None:
    """Authenticate the projection's typed ``campaign_manifest`` artifact reference."""
    _need(
        isinstance(reference, dict), "compatibility manifest backlink is not an artifact reference"
    )
    _need(reference.get("path_base") == "origin_target", "manifest backlink base differs")
    _need(
        reference.get("kind") == "simulation_campaign_manifest",
        "manifest backlink kind differs",
    )
    _need(reference.get("owner") == campaign_id, "manifest backlink owner differs")
    path = reference.get("path")
    _need(isinstance(path, str) and bool(path), "manifest backlink path is invalid")
    # ``origin_target`` is the Target directory that holds simulation.json.
    target = projection_path.parent / path
    _need(target.resolve() == manifest_path.resolve(), "compatibility manifest backlink differs")
    _need(
        type(reference.get("bytes")) is int and reference["bytes"] == len(manifest_raw),
        "manifest backlink size differs",
    )
    _need(
        reference.get("sha256") == _file_digest(manifest_raw),
        "manifest backlink digest differs",
    )

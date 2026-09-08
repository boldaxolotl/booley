"""Snapshot source/build provenance before native coverage execution."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .coverage_invocation import CoverageTargetPlan

from booley.targets.domain import TargetInspection


def coverage_digest(value: object) -> str:
    """Fingerprint canonical JSON data independently of mapping order."""
    data = json.dumps(value, sort_keys=True, separators=(",", ":"), default=_json_value)
    return content_digest(data.encode())


def _json_value(value: object) -> object:
    if isinstance(value, Mapping):
        return dict(value)
    raise TypeError(f"Unsupported coverage provenance value: {type(value).__name__}")


def content_digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def coverage_source_closure(inspection: TargetInspection) -> dict[str, list[dict[str, str]]]:
    root = inspection.handle.project_root.resolve()
    closure: dict[str, list[dict[str, str]]] = {"rtl": [], "testbench": []}
    for item in sorted(inspection.inputs, key=lambda item: item.path):
        path = (root / item.path).resolve()
        if not path.is_relative_to(root):
            raise ValueError(f"Coverage source is outside the producing repository: {item.path}")
        category = "testbench" if "tb" in item.tags else "rtl"
        closure[category].append(
            {"path": Path(item.path).as_posix(), "sha256": content_digest(path.read_bytes())}
        )
    return closure


def validate_coverage_sources(plan: CoverageTargetPlan) -> None:
    """Reject source/Target drift rather than publish a Campaign under stale fingerprints."""
    for category in ("rtl", "testbench"):
        records = plan.source_closure[category]
        assert isinstance(records, tuple)
        for record in records:
            assert isinstance(record, Mapping)
            path = plan.handle.project_root / str(record["path"])
            if content_digest(path.read_bytes()) != record["sha256"]:
                raise ValueError(f"Coverage source changed after Preflight: {record['path']}")
    current = coverage_digest(
        {"core": plan.handle.core_file.read_text(), "identity": plan.handle.identity}
    )
    if current != plan.target_fingerprint:
        raise ValueError("Coverage Target definition changed after Preflight")

"""Versioned Simulation Workload Snapshots for Cycle Count evidence."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any

from booley.dev_support.cycle_count import PROVENANCE_LIMITATION
from booley.flows.recipe_evidence import jsonable
from booley.fusesoc.fusesoc_registry import ResolvedFile, ResolvedTarget

WORKLOAD_SNAPSHOT_SCHEMA = 1


def _role(file: ResolvedFile) -> str:
    if file.is_tb:
        return "tb"
    if file.file_type.lower() in {"sdc", "xdc"}:
        return "constraint"
    if file.is_hdl:
        return "rtl"
    return "workload"


def _identity(root: Path, file: ResolvedFile, path: Path) -> str:
    try:
        return path.relative_to(root.resolve()).as_posix()
    except ValueError:
        return PurePosixPath(file.name).as_posix()


def _input_row(root: Path, file: ResolvedFile, build_root: Path) -> dict[str, Any]:
    path = file.absolute(build_root)
    try:
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        size = len(data)
        present = True
    except OSError:
        digest = hashlib.sha256(b"").hexdigest()
        size = 0
        present = False
    return {
        "path": _identity(root, file, path),
        "file_type": file.file_type,
        "tags": list(file.tags),
        "role": _role(file),
        "repository": file.core,
        "sha256": digest,
        "bytes": size,
        "present": present,
    }


def _fingerprint(value: Mapping[str, Any]) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_workload_snapshot(
    work_dir: Path,
    target: str,
    test: str,
    resolved: ResolvedTarget,
    *,
    controls: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one deterministic Target/test workload description."""
    root = Path(work_dir).resolve()
    snapshot: dict[str, Any] = {
        "schema": WORKLOAD_SNAPSHOT_SCHEMA,
        "target": target,
        "test": test,
        "vlnv": resolved.vlnv,
        "toplevel": resolved.toplevel,
        "eda_tool": resolved.eda_tool,
        "parameters": jsonable(resolved.parameters),
        "flow_options": jsonable(resolved.flow_options),
        "controls": jsonable(controls or {}),
        "inputs": sorted(
            (_input_row(root, file, resolved.build_root) for file in resolved.files),
            key=lambda row: (row["path"], row["role"]),
        ),
        "provenance_limitation": PROVENANCE_LIMITATION,
    }
    snapshot["fingerprint"] = _fingerprint(snapshot)
    return snapshot

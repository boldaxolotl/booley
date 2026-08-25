"""Versioned Simulation Workload Snapshots for Cycle Count evidence."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any

from booley.flows.recipe_evidence import jsonable
from booley.fusesoc.fusesoc_registry import ResolvedFile, ResolvedTarget

WORKLOAD_SNAPSHOT_SCHEMA = 1
PROVENANCE_LIMITATION = (
    "Booley fingerprints declared Target inputs and captured controls. It cannot prove "
    "arbitrary transitive files opened by hooks, generated or gitignored inputs, ambient "
    "environment values, or external toolchain state unless they are declared on a captured surface."
)


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


def workload_changes(
    baseline: Mapping[str, Any],
    current: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Return deterministic path-oriented changes between declared inputs."""
    before = {
        str(row.get("path")): row
        for row in baseline.get("inputs", [])
        if isinstance(row, Mapping) and row.get("path")
    }
    after = {
        str(row.get("path")): row
        for row in current.get("inputs", [])
        if isinstance(row, Mapping) and row.get("path")
    }
    changes: list[dict[str, Any]] = []
    for path in sorted(set(before) | set(after)):
        old = before.get(path)
        new = after.get(path)
        if old is not None and new is not None and old.get("sha256") == new.get("sha256"):
            continue
        status = "added" if old is None else "deleted" if new is None else "modified"
        changes.append(
            {
                "path": path,
                "role": (new or old or {}).get("role", "workload"),
                "status": status,
                "baseline_sha256": old.get("sha256") if old is not None else None,
                "current_sha256": new.get("sha256") if new is not None else None,
            }
        )
    return changes

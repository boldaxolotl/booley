"""Shared normalized-recipe evidence for implementation QoR criteria."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

RECIPE_FINGERPRINT_PARAM = "_recipe_fingerprint"
RECIPE_FINGERPRINT_DETAIL = "_recipe_fingerprint"
RECIPE_SNAPSHOT_PARAM = "_recipe_snapshot"
RECIPE_SNAPSHOT_DETAIL = "_recipe_snapshot"
BASELINE_RECIPE_FINGERPRINT_DETAIL = "_baseline_recipe_fingerprint"
BASELINE_RECIPE_SNAPSHOT_DETAIL = "_baseline_recipe_snapshot"
BASELINE_REF_PARAM = "_baseline_ref"
BASELINE_REF_DETAIL = "_baseline_ref"


def recipe_snapshot_fingerprint(snapshot: Mapping[str, Any]) -> str:
    """Hash one normalized implementation-recipe snapshot."""
    encoded = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def recipe_changes(
    baseline: Mapping[str, Any],
    current: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Return deterministic leaf-level changes between two recipe snapshots."""
    changes: list[dict[str, Any]] = []
    _append_recipe_changes(changes, "", baseline, current)
    return changes


def _append_recipe_changes(
    changes: list[dict[str, Any]],
    path: str,
    baseline: Any,
    current: Any,
) -> None:
    """Append recursive snapshot changes using stable dotted/indexed paths."""
    if isinstance(baseline, Mapping) and isinstance(current, Mapping):
        for key in sorted(set(baseline) | set(current), key=str):
            child = f"{path}.{key}" if path else str(key)
            if key not in baseline:
                changes.append({"path": child, "before": None, "after": current[key]})
            elif key not in current:
                changes.append({"path": child, "before": baseline[key], "after": None})
            else:
                _append_recipe_changes(changes, child, baseline[key], current[key])
        return
    if isinstance(baseline, list) and isinstance(current, list):
        for index in range(max(len(baseline), len(current))):
            child = f"{path}[{index}]"
            if index >= len(baseline):
                changes.append({"path": child, "before": None, "after": current[index]})
            elif index >= len(current):
                changes.append({"path": child, "before": baseline[index], "after": None})
            else:
                _append_recipe_changes(changes, child, baseline[index], current[index])
        return
    if baseline != current:
        changes.append({"path": path, "before": baseline, "after": current})


def jsonable(value: Any) -> Any:
    """Convert EDAM/YAML values into a deterministic JSON-safe structure."""
    if isinstance(value, Mapping):
        return {str(key): jsonable(item) for key, item in sorted(value.items(), key=str)}
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    if isinstance(value, Path):
        return value.as_posix()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)

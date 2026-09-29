"""Normalized recipe values used by producers and acceptance policy."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

_SHA256_RE = re.compile(r"[0-9a-fA-F]{64}\Z")
_COMPATIBLE_SCHEMA_PAIRS = {("synth", 3, 4), ("fpga", 2, 3)}


class InvalidRecipeSnapshotError(ValueError):
    """Raised when persisted recipe evidence cannot identify its constraints."""


def constraint_recipe_entry(
    resolved_file: Any,
    digest: str | None,
    *,
    fallback_vlnv: str,
) -> dict[str, Any]:
    """Build path-free provenance for one ordered implementation constraint."""
    return {"core": resolved_file.core or fallback_vlnv, "sha256": digest}


def recipe_snapshot_fingerprint(snapshot: Mapping[str, Any]) -> str:
    """Hash one normalized implementation-recipe snapshot."""
    encoded = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def implementation_comparison_basis(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Project the measurement methodology that a paired comparison must share."""
    if snapshot.get("flow") == "fpga":
        flow_options = snapshot.get("flow_options")
        options = flow_options if isinstance(flow_options, Mapping) else {}
        return {
            "flow": "fpga",
            "vlnv": snapshot.get("vlnv"),
            "toplevel": snapshot.get("toplevel"),
            "eda_tool": snapshot.get("eda_tool"),
            "part": options.get("part"),
            "out_of_context": options.get("out_of_context", False),
            "ppa_profile": jsonable(snapshot.get("ppa_profile")),
            "constraints": _constraint_digests(snapshot),
        }
    return {
        "flow": "synth",
        "vlnv": snapshot.get("vlnv"),
        "toplevel": snapshot.get("toplevel"),
        "recipe_args": jsonable(snapshot.get("recipe_args", [])),
        "constraints": _constraint_digests(snapshot),
        "technology": jsonable(snapshot.get("technology")),
    }


def compatible_recipe_snapshots(
    legacy: Mapping[str, Any],
    current: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Normalize one allowlisted persisted recipe-schema transition."""
    legacy_flow = _recipe_flow(legacy)
    current_flow = _recipe_flow(current)
    pair = (legacy_flow, legacy.get("schema"), current.get("schema"))
    if legacy_flow != current_flow or pair not in _COMPATIBLE_SCHEMA_PAIRS:
        return None
    normalized_legacy = jsonable(legacy)
    normalized_current = jsonable(current)
    normalized_legacy["flow"] = legacy_flow
    normalized_legacy["schema"] = normalized_current["schema"]
    normalized_legacy["constraints"] = _constraint_digests(legacy)
    normalized_current["constraints"] = _constraint_digests(current)
    return normalized_legacy, normalized_current


def validated_recipe_compatibility(
    frozen_snapshot: Any,
    frozen_fingerprint: Any,
    rerun_snapshot: Any,
    rerun_fingerprint: Any,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Validate persisted evidence and adapt one known schema transition."""
    snapshots_present = isinstance(frozen_snapshot, Mapping) and isinstance(
        rerun_snapshot, Mapping
    )
    fingerprints_present = isinstance(frozen_fingerprint, str) and isinstance(
        rerun_fingerprint, str
    )
    if not snapshots_present or not fingerprints_present:
        return None
    fingerprints_match = (
        recipe_snapshot_fingerprint(frozen_snapshot) == frozen_fingerprint
        and recipe_snapshot_fingerprint(rerun_snapshot) == rerun_fingerprint
    )
    if not fingerprints_match:
        return None
    try:
        normalized = compatible_recipe_snapshots(frozen_snapshot, rerun_snapshot)
    except InvalidRecipeSnapshotError:
        return None
    if normalized is None or normalized[0] != normalized[1]:
        return None
    return normalized


def _recipe_flow(snapshot: Mapping[str, Any]) -> str | None:
    flow = snapshot.get("flow")
    if isinstance(flow, str):
        return flow
    if snapshot.get("schema") == 3:
        return "synth"
    return None


def _constraint_digests(snapshot: Mapping[str, Any]) -> list[str]:
    constraints = snapshot.get("constraints")
    if not isinstance(constraints, list):
        raise InvalidRecipeSnapshotError("recipe constraints must be a list")
    digests: list[str] = []
    for index, constraint in enumerate(constraints):
        digest = constraint.get("sha256") if isinstance(constraint, Mapping) else None
        if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
            raise InvalidRecipeSnapshotError(
                f"recipe constraint {index} has no valid SHA-256 digest"
            )
        digests.append(digest.lower())
    return digests


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

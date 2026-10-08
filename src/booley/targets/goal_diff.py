"""Semantic Target changes from the runtime's resolved base/final views."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any, cast

from booley.targets.surface_diff import changed_rows


def semantic_target_changes(
    before: Mapping[str, Any], after: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Plain surface delta plus type-exact comparison; int/float/bool differ."""
    # The shared delta names additions/deletions. Canonical typed equality avoids
    # Python's 1 == 1.0 == True equality when identifying modifications.
    added, _, removed = changed_rows(before, after)
    modified = sorted(
        key for key in before.keys() & after.keys() if typed(before[key]) != typed(after[key])
    )
    result = [
        {"target": key, "change": "added", "after": json_surface(after[key])} for key in added
    ]
    result += [
        {"target": key, "change": "removed", "before": json_surface(before[key])}
        for key in removed
    ]
    for key in modified:
        fields = sorted(
            field
            for field in before[key].keys() | after[key].keys()
            if typed(before[key].get(field)) != typed(after[key].get(field))
        )
        result.append(
            {
                "target": key,
                "change": "modified",
                "fields": fields,
                "before": json_surface(before[key]),
                "after": json_surface(after[key]),
            }
        )
    return result


def typed(value: object) -> object:
    """Collision-free JSON-compatible typed trees, including mixed YAML keys."""
    if value is None or type(value) in (str, bool, int, float):
        return [type(value).__name__, value]
    if isinstance(value, Mapping):
        rows = [
            [typed(key), typed(item)]
            for key, item in cast("Mapping[object, object]", value).items()
        ]
        return [
            "mapping",
            sorted(
                rows,
                key=lambda row: hashlib.sha256(
                    json.dumps(row, sort_keys=True).encode()
                ).hexdigest(),
            ),
        ]
    if isinstance(value, (tuple, list)):
        return [
            "sequence",
            [typed(item) for item in cast("list[object] | tuple[object, ...]", value)],
        ]
    raise ValueError(f"cannot encode Target surface value {type(value).__name__}")


def json_surface(value: object) -> Any:
    """Retain ordinary fields while encoding mixed mapping keys without JSON collisions."""
    if isinstance(value, Mapping):
        mapping = cast("Mapping[object, object]", value)
        if not all(isinstance(key, str) for key in mapping):
            return {"$typed_mapping": typed(mapping)}
        return {str(key): json_surface(item) for key, item in mapping.items()}
    if isinstance(value, (tuple, list)):
        return [json_surface(item) for item in cast("list[object] | tuple[object, ...]", value)]
    typed(value)
    return value

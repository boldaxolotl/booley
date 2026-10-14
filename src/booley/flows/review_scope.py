"""Resolve Goal-owned review files through the same Target closure as evidence."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from booley.flows.endpoint_diff import _classify_files
from booley.flows.source_fingerprint import compute_source_fingerprint


def resolve_review_files(root: Path, category: str, params: Mapping[str, Any]) -> list[str]:
    """Resolve exactly one declared scope, refusing escapes and category mismatches."""
    target, paths = params.get("target"), params.get("paths")
    if (target is None) == (paths is None):
        raise ValueError("Review Goal requires exactly one of target or paths")
    if target is not None:
        fingerprint = compute_source_fingerprint(root, target=target)
        if fingerprint.get("target") != target:
            raise ValueError(f"Review Target {target!r} cannot be resolved")
        selected = list(fingerprint[category]["files"])
    else:
        selected = list(paths)
    if not selected:
        raise ValueError(f"Review Goal has no {category.upper()} files")
    for relative in selected:
        path = root / relative
        if not path.resolve().is_relative_to(root.resolve()):
            raise ValueError(f"Review path {relative!r} escapes the Goal worktree")
        if paths is not None and category not in _classify_files([relative], root):
            raise ValueError(
                f"Review path {relative!r} does not match {category.upper()} category"
            )
    return sorted(set(selected))

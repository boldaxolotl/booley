"""Resolve Goal-owned review files through the same Target closure as evidence."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from booley.flows.endpoint_diff import _classify_files
from booley.flows.source_fingerprint import compute_source_fingerprint
from booley.targets.domain import FuseSocError


class UnresolvedReviewScopeError(ValueError):
    """Declared review files are not available or registered yet."""


def resolve_review_files(root: Path, category: str, params: Mapping[str, Any]) -> list[str]:
    """Resolve exactly one declared scope, refusing escapes and category mismatches."""
    target, paths = params.get("target"), params.get("paths")
    if (target is None) == (paths is None):
        raise ValueError(
            "Review Goal requires exactly one of target or paths. "
            "For an existing Goal, request an approved Goal Change that declares its scope."
        )
    if target is not None:
        try:
            fingerprint = compute_source_fingerprint(root, target=target)
        except FuseSocError as exc:
            raise UnresolvedReviewScopeError(
                f"Review Target {target!r} cannot be resolved: {exc}"
            ) from exc
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
        categories = _classify_files([relative], root) if paths is not None else {category}
        if categories and category not in categories:
            raise ValueError(
                f"Review path {relative!r} does not match {category.upper()} category"
            )
        if not path.is_file() or not categories:
            raise UnresolvedReviewScopeError(
                f"Review path {relative!r} does not exist or is not registered as {category.upper()}; "
                "create it and register it in the .core filesets before review"
            )
    return sorted(set(selected))

"""Goal-local authored-input policy for Target-consumed ignored build data."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from booley.core.boundary import as_dict, is_str_list
from booley.flows.source_fingerprint import (
    campaign_fingerprint,
    compute_source_fingerprint,
    hash_named_files,
)
from booley.goals.target_surface import target_surface_fingerprint
from booley.runtime.git_ignore import ignored_paths as _ignored
from booley.runtime.pinned_history import tree_rows
from booley.targets.catalog import TargetCatalog
from booley.targets.declared_inputs import HDL_SUFFIXES, committed_only_inputs
from booley.targets.domain import UnknownTargetError

if TYPE_CHECKING:
    from booley.goals.input_view import CommittedView
    from booley.goals.model import GoalRecord


def generated_artifact_paths(root: Path) -> frozenset[Path]:
    """Select ignored consumed data, retaining authored HDL and programs."""
    root = root.resolve()
    source = compute_source_fingerprint(root)
    names = {
        name
        for category in ("rtl", "tb", "workload", "campaign")
        for name in source.get(category, {}).get("files", [])
    }
    names.update(target_surface_fingerprint(root, None)["files"])
    candidates = [root / name for name in sorted(names) if _eligible(root, root / name)]
    ignored = _ignored(root, candidates)
    if not ignored:
        return frozenset()
    catalog = TargetCatalog.build(root)
    inputs = tuple(
        root / item.path for handle in catalog.list() for item in catalog.inspect(handle).inputs
    )
    consumed = {path.resolve() for path in inputs}
    unsafe = {path.resolve() for path in inputs if not _eligible(root, path)}
    protected = committed_only_inputs(root, catalog, None, include_missing=True)
    return frozenset(ignored & consumed - protected - unsafe)


def _eligible(root: Path, path: Path) -> bool:
    if path.suffix.casefold() in HDL_SUFFIXES | {".core"}:
        return False
    if not path.is_relative_to(root) or not path.resolve().is_relative_to(root):
        return False
    current = path
    while current != root:
        if current.is_symlink() or (current != path and (current / ".git").exists()):
            return False
        current = current.parent
    return True


def project_source(
    root: Path, source: dict[str, Any], excluded: frozenset[Path]
) -> dict[str, Any]:
    """Project shared source categories without changing shared caller policy."""
    if not excluded:
        return source
    root = root.resolve()
    result = dict(source)
    for category in ("rtl", "tb", "workload", "campaign"):
        entry = source.get(category)
        if not isinstance(entry, dict) or not isinstance(entry.get("files"), list):
            continue
        names = [name for name in entry["files"] if (root / name).resolve() not in excluded]
        if names == entry["files"]:
            continue
        result[category] = (
            campaign_fingerprint(root, None, excluded=excluded)
            if category == "campaign"
            else hash_named_files(root, names)
        )
        if category in ("rtl", "tb"):
            result[f"{category}_dirs"] = sorted(
                {PurePosixPath(name).parent.as_posix() for name in names}
            )
    return result


def goal_target_surface(root: Path, target: str | None) -> dict[str, Any]:
    """Resolve a Goal surface under its authored-input policy."""
    excluded = generated_artifact_paths(root) if target is None else frozenset()
    return target_surface_fingerprint(root, target, artifact_paths=excluded)


@dataclass(frozen=True)
class ArtifactEpoch:
    """Pinned consumption mapped to live identities, without retained export readers."""

    inputs: tuple[tuple[str, Path], ...]
    eligible: frozenset[Path]
    protected: frozenset[Path]
    tracked: frozenset[Path]
    bound: frozenset[Path]


def artifact_epoch(view: CommittedView, record: GoalRecord, *, baseline: bool) -> ArtifactEpoch:
    """Capture literal catalog labels and pinned authored membership for one epoch."""
    catalog = TargetCatalog.build(view.root)
    inputs = tuple(
        (item.path, view.root / item.path)
        for handle in catalog.list()
        for item in catalog.inspect(handle).inputs
    )
    mapped = tuple((label, _live_path(view, path)) for label, path in inputs)
    eligible = frozenset(
        _live_path(view, path)
        for _, path in inputs
        if _eligible(view.root, path) and _eligible(view.selection.rtl, _live_path(view, path))
    )
    protected = frozenset(
        _live_path(view, path)
        for path in committed_only_inputs(view.root, catalog, None, include_missing=True)
    )
    bound: set[Path] = set()
    for goal in record.goals:
        if goal.spec.target is None:
            continue
        try:
            handle = catalog.select(goal.spec.target)
        except UnknownTargetError:
            continue  # a candidate Goal Target may not exist at the baseline
        bound.update(
            _live_path(view, view.root / item.path) for item in catalog.inspect(handle).inputs
        )
    return ArtifactEpoch(
        mapped, eligible, protected, _epoch_tracked(view, baseline, mapped), frozenset(bound)
    )


def _live_path(view: CommittedView, path: Path) -> Path:
    physical = path.resolve()
    for original, exported in sorted(
        view.mappings, key=lambda pair: len(pair[1].parts), reverse=True
    ):
        if physical.is_relative_to(exported):
            return (original / physical.relative_to(exported)).resolve()
    raise ValueError(f"pinned input cannot be mapped to its live participant: {path}")


def _epoch_tracked(
    view: CommittedView, baseline: bool, inputs: tuple[tuple[str, Path], ...]
) -> frozenset[Path]:
    selection = view.selection
    pin = selection.rtl_base if baseline else selection.rtl_pin
    rows = tree_rows(selection.rtl, pin)
    tracked = {selection.rtl / os.fsdecode(name) for name in rows}
    # A gitlink owns every descendant even when the live submodule is missing.
    links = {
        selection.rtl / os.fsdecode(name)
        for name, mode in rows.items()
        if mode.startswith(b"160000 ")
    }
    tracked.update(path for _, path in inputs if any(path.is_relative_to(link) for link in links))
    return frozenset(path.resolve() for path in tracked)


def retained_presentation_inputs(
    view: CommittedView, observations: tuple[dict[str, Any], ...]
) -> frozenset[Path]:
    """Keep mandatory captures and independently authenticated producer/scope proofs."""
    retained = {
        Path(row["path"]).resolve()
        for row in view.observations
        if row["classification"] in {"ProtectedInput", "ProjectSnapshot", "GeneratedBuildInput"}
    }
    for row in observations:
        detail = row["detail"]
        stamp = as_dict(detail.get("_source_fingerprint")) or {}
        if isinstance(stamp.get("target"), str) and stamp["target"]:
            for category in ("rtl", "tb", "workload", "campaign", "target_surface"):
                for name in stamp.get("fingerprint", {}).get(category, {}).get("files", []):
                    retained.add(_inventory_path(view, name))
        contract = as_dict(detail.get("contract")) or {}
        scope = contract.get("scope", detail.get("scope", []))
        if not is_str_list(scope):
            raise ValueError("retained evidence scope must be a list of paths")
        for name in scope:
            retained.add(_inventory_path(view, name))
    return frozenset(retained)


def _inventory_path(view: CommittedView, name: str) -> Path:
    path = Path(name)
    original = path if path.is_absolute() else view.selection.rtl / path
    return _live_path(view, view.mapped(original))


def presentation_exclusions(
    root: Path, epochs: tuple[ArtifactEpoch, ArtifactEpoch], retained: frozenset[Path]
) -> tuple[tuple[frozenset[str], frozenset[str]], frozenset[str]]:
    """Project each pinned epoch using live ignore rules and its tracked protection."""
    bound = frozenset(path for epoch in epochs for path in epoch.bound)
    candidates = tuple(
        epoch.eligible - epoch.protected - epoch.tracked - bound - retained for epoch in epochs
    )
    ignored = _ignored(root, sorted(set().union(*candidates)), no_index=True)
    labels = tuple(
        frozenset(label for label, path in epoch.inputs if path in candidate & ignored)
        for epoch, candidate in zip(epochs, candidates, strict=True)
    )
    omitted = frozenset(str(path) for candidate in candidates for path in candidate & ignored)
    names = frozenset(name for group in labels for name in group)
    return (labels[0], labels[1]), omitted | names

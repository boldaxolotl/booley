"""Predict strict coverage from captured approval bytes in an isolated overlay."""

from __future__ import annotations

import json
import shutil
import tomllib
from collections.abc import Collection, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from booley.config.coverage_waiver_inputs import CoverageWaiverConfig
from booley.flows.sim.coverage_campaign import (
    CoverageCampaign,
    DurableTargetIdentity,
    _freeze_mapping,
)
from booley.flows.sim.coverage_policy import (
    CoverageCriterion,
    EvaluatedCoverageCampaign,
    _derive_rollup,
    evaluate_coverage_campaign,
)
from booley.flows.sim.coverage_provisional import criterion_from_evaluation
from booley.flows.sim.coverage_waivers import (
    ApprovedWaiverSet,
    CoverageRepositoryRoots,
    load_approved_waiver_set,
)


class WaiverPlanError(ValueError):
    """An approval directory cannot be safely copied for prediction."""


def _reject_symlinks(anchor_root: Path, approval_dir: Path) -> None:
    """Never traverse writable links while predicting or promoting approvals."""
    for path in (approval_dir, *approval_dir.parents):
        if path.is_symlink():
            raise WaiverPlanError(f"approval directory contains a symlink: {path}")
        if path == anchor_root:
            break
    if approval_dir.is_dir():
        for path in approval_dir.rglob("*"):
            if path.is_symlink():
                raise WaiverPlanError(f"approval directory contains a symlink: {path}")


def _named_sources(approval_dir: Path) -> set[str]:
    """Every RTL source an approval document in the directory names."""
    sources: set[str] = set()
    for document in approval_dir.rglob("*.toml"):
        try:
            source = tomllib.loads(document.read_text(encoding="utf-8")).get("source")
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
            continue  # the real loader reports it; the overlay only mirrors inputs
        if isinstance(source, str):
            sources.add(source)
    return sources


def load_promoted_waiver_set(
    config: CoverageWaiverConfig,
    roots: CoverageRepositoryRoots,
    overlay: Path,
    known_targets: Collection[DurableTargetIdentity],
    after_bytes: Mapping[Path, bytes],
) -> ApprovedWaiverSet:
    """Load the captured approval images with the real loader, without publishing."""
    anchor_root = Path(getattr(roots, config.anchor))
    source_dir = anchor_root / config.directory
    _reject_symlinks(anchor_root, source_dir)
    if source_dir.is_dir():
        shutil.copytree(source_dir, overlay / config.directory, symlinks=False)
    for relative, content in after_bytes.items():
        destination = overlay / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
    shadow = replace(roots, **{config.anchor: overlay})
    if config.anchor == "rtl_repository":
        for source in _named_sources(overlay / config.directory):
            original = roots.rtl_repository / source
            if original.is_file() and not original.is_symlink():
                (overlay / source).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(original, overlay / source)
    return load_approved_waiver_set(config, shadow, known_targets)


def _unwaived(campaign: CoverageCampaign) -> CoverageCampaign:
    """The Campaign before any Approved Waiver was applied to its points."""
    points = tuple(
        replace(point, disposition=_freeze_mapping({"kind": "eligible"}))
        if point.disposition.get("kind") == "waived" and "waiver_id" in point.disposition
        else point
        for point in campaign.points
    )
    rollups = tuple(_derive_rollup(rollup, points) for rollup in campaign.rollups)
    return replace(campaign, points=points, rollups=rollups)


def strict_reevaluation(
    campaign: CoverageCampaign,
    waivers: ApprovedWaiverSet,
    *,
    criterion: CoverageCriterion | None = None,
) -> EvaluatedCoverageCampaign:
    """Evaluate a persisted Campaign strictly against a new Approved Waiver Set."""
    return evaluate_coverage_campaign(
        _unwaived(campaign), criterion or criterion_from_evaluation(campaign), waivers
    )


def evaluation_json(campaign: EvaluatedCoverageCampaign) -> Mapping[str, Any]:
    """Plain JSON of an evaluation, as Criterion details store it."""
    return json.loads(json.dumps(campaign.evaluation, default=_plain))


def _plain(value: object) -> object:
    if isinstance(value, Mapping):
        return dict(value)
    raise TypeError(f"not JSON data: {type(value).__name__}")


__all__ = [
    "WaiverPlanError",
    "evaluation_json",
    "load_promoted_waiver_set",
    "strict_reevaluation",
]

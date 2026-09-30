"""Apply a Human's accepted Waiver Candidates as Approved Waivers (ADR 0066).

A :class:`WaiverPromotionPlan` is the durable, write-once decision recorded at
``board approve``: the accepted candidates, their approval stamps, and the
configured approval directory. The same plan is applied twice:

- at approval, to an overlay copy of the approval directory, so the strict
  verdict can be predicted with the real loader before anything is published;
- in the Acceptance Journal's merge candidate, where the files are committed
  and reach the destination together with the RTL they justify.

Rendering is deterministic for one plan and one starting directory, which is
what makes the Journal's recompute-on-recovery safe.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import tomllib
from collections.abc import Collection, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from booley.config.coverage_waiver_inputs import CoverageWaiverConfig
from booley.core.boundary import BoundaryError, require_dict, require_list, require_str
from booley.flows.sim.coverage_campaign import (
    CoverageCampaign,
    DurableTargetIdentity,
    _freeze_mapping,
)
from booley.flows.sim.coverage_policy import (
    EvaluatedCoverageCampaign,
    _derive_rollup,
    evaluate_coverage_campaign,
)
from booley.flows.sim.coverage_provisional import criterion_from_evaluation
from booley.flows.sim.coverage_waiver_promotion import (
    ApprovalStamps,
    WaiverCandidateEvidence,
    WaiverPromotionError,
    build_promotion,
    render_approval_document,
    review_proof_path,
    waiver_file_path,
)
from booley.flows.sim.coverage_waivers import (
    ApprovedWaiverSet,
    CoverageRepositoryRoots,
    CoverageWaiverValidationError,
    load_approved_waiver_set,
)

PLAN_SCHEMA = 1


class WaiverPlanError(ValueError):
    """A promotion plan is malformed or cannot be applied to its directory."""


@dataclass(frozen=True)
class WaiverPromotionPlan:
    """The accepted candidates of one approval, bound to its approval directory."""

    anchor: str
    directory: str
    stamps: ApprovalStamps
    candidates: tuple[WaiverCandidateEvidence, ...]

    def __post_init__(self) -> None:
        if not self.candidates:
            raise WaiverPlanError("a promotion plan needs at least one accepted candidate")
        ids = [item.waiver_id for item in self.candidates]
        if len(set(ids)) != len(ids):
            raise WaiverPlanError("a promotion plan names one candidate twice")
        object.__setattr__(
            self, "candidates", tuple(sorted(self.candidates, key=lambda i: i.waiver_id))
        )

    @property
    def config(self) -> CoverageWaiverConfig:
        return CoverageWaiverConfig(self.anchor, self.directory)

    @property
    def waiver_ids(self) -> tuple[str, ...]:
        return tuple(item.waiver_id for item in self.candidates)

    def to_json(self) -> dict[str, Any]:
        return {
            "schema": PLAN_SCHEMA,
            "anchor": self.anchor,
            "directory": self.directory,
            "stamps": {
                "approved_by": self.stamps.approved_by,
                "approved_at": self.stamps.approved_at,
                "approval_ref": self.stamps.approval_ref,
            },
            "candidates": [_candidate_json(item) for item in self.candidates],
        }

    def to_bytes(self) -> bytes:
        return (json.dumps(self.to_json(), sort_keys=True, separators=(",", ":")) + "\n").encode()

    def sha256(self) -> str:
        return "sha256:" + hashlib.sha256(self.to_bytes()).hexdigest()

    @classmethod
    def from_json(cls, value: object) -> WaiverPromotionPlan:
        try:
            document = require_dict(value, field="waiver promotion plan")
            if document.get("schema") != PLAN_SCHEMA:
                raise WaiverPlanError(
                    f"unsupported promotion plan schema {document.get('schema')!r}"
                )
            stamps = require_dict(document.get("stamps"), field="stamps")
            return cls(
                anchor=require_str(document, "anchor"),
                directory=require_str(document, "directory"),
                stamps=ApprovalStamps(
                    require_str(stamps, "approved_by"),
                    require_str(stamps, "approved_at"),
                    require_str(stamps, "approval_ref"),
                ),
                candidates=tuple(
                    _candidate_from_json(item)
                    for item in require_list(document.get("candidates"), field="candidates")
                ),
            )
        except (BoundaryError, WaiverPromotionError, TypeError) as exc:
            raise WaiverPlanError(f"invalid waiver promotion plan: {exc}") from exc


_CANDIDATE_FIELDS = (
    "waiver_id",
    "campaign_id",
    "manifest_sha256",
    "point_store_sha256",
    "target",
    "point_id",
    "source",
    "source_sha256",
    "reason",
    "justification",
    "proposal_count",
)


def _candidate_json(item: WaiverCandidateEvidence) -> dict[str, Any]:
    return {
        **{name: getattr(item, name) for name in _CANDIDATE_FIELDS},
        "evidence_refs": list(item.evidence_refs),
    }


def _candidate_from_json(value: object) -> WaiverCandidateEvidence:
    row = require_dict(value, field="promoted candidate")
    if set(row) != {*_CANDIDATE_FIELDS, "evidence_refs"}:
        raise WaiverPlanError("promoted candidate fields are closed")
    return WaiverCandidateEvidence(
        **{name: row[name] for name in _CANDIDATE_FIELDS},
        evidence_refs=tuple(require_list(row["evidence_refs"], field="evidence_refs")),
    )


def _by_source(plan: WaiverPromotionPlan) -> dict[str, list[WaiverCandidateEvidence]]:
    grouped: dict[str, list[WaiverCandidateEvidence]] = {}
    for item in plan.candidates:
        grouped.setdefault(item.source, []).append(item)
    for source, items in grouped.items():
        if len({item.source_sha256 for item in items}) != 1:
            raise WaiverPlanError(f"accepted candidates disagree on the digest of {source}")
    return grouped


def _write_new_or_equal(path: Path, content: bytes) -> None:
    """Write a proof once; an identical existing file is an idempotent retry."""
    if path.exists():
        if path.read_bytes() != content:
            raise WaiverPlanError(f"{path.name} already exists with different content")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def apply_promotion_plan(plan: WaiverPromotionPlan, anchor_root: Path) -> tuple[str, ...]:
    """Append the plan's records under ``anchor_root/<directory>``; return written paths.

    Paths are relative to ``anchor_root``. Raises :class:`WaiverPlanError` or a
    :class:`WaiverPromotionError` when the existing directory cannot take them.
    """
    approval_dir = anchor_root / plan.directory
    _reject_symlinks(anchor_root, approval_dir)
    written: list[str] = []
    for source, items in sorted(_by_source(plan).items()):
        promotions = [build_promotion(item, plan.stamps) for item in items]
        document = approval_dir / waiver_file_path(source)
        existing = document.read_bytes() if document.is_file() else None
        rendered = render_approval_document(
            existing,
            source=source,
            source_sha256=items[0].source_sha256,
            records=[promotion.record for promotion in promotions],
        )
        document.parent.mkdir(parents=True, exist_ok=True)
        document.write_bytes(rendered)
        written.append((Path(plan.directory) / waiver_file_path(source)).as_posix())
        for promotion in promotions:
            if promotion.proof is None:
                continue
            relative = review_proof_path(promotion.record.waiver_id)
            _write_new_or_equal(approval_dir / relative, promotion.proof)
            written.append((Path(plan.directory) / relative).as_posix())
    return tuple(written)


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


def _overlay_roots(
    plan: WaiverPromotionPlan, roots: CoverageRepositoryRoots, overlay: Path
) -> CoverageRepositoryRoots:
    """Copy the approval directory (and, for an RTL anchor, its sources) to *overlay*."""
    anchor_root = Path(getattr(roots, plan.anchor))
    source_dir = anchor_root / plan.directory
    _reject_symlinks(anchor_root, source_dir)
    if source_dir.is_dir():
        shutil.copytree(source_dir, overlay / plan.directory, symlinks=False)
    if plan.anchor == "project_data_repository":
        return replace(roots, project_data_repository=overlay)
    for source in _named_sources(overlay / plan.directory) | {i.source for i in plan.candidates}:
        original = roots.rtl_repository / source
        if original.is_file() and not original.is_symlink():
            (overlay / source).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(original, overlay / source)
    return replace(roots, rtl_repository=overlay)


def load_promoted_waiver_set(
    plan: WaiverPromotionPlan,
    roots: CoverageRepositoryRoots,
    overlay: Path,
    known_targets: Collection[DurableTargetIdentity],
) -> ApprovedWaiverSet:
    """Load, with the real loader, the Approved Waiver Set the plan would produce."""
    shadow = _overlay_roots(plan, roots, overlay)
    apply_promotion_plan(plan, Path(getattr(shadow, plan.anchor)))
    return load_approved_waiver_set(plan.config, shadow, known_targets)


def _named_targets(approval_dir: Path) -> set[str]:
    """Every Target an approval document in the directory names."""
    targets: set[str] = set()
    for document in approval_dir.rglob("*.toml"):
        try:
            approvals = tomllib.loads(document.read_text(encoding="utf-8")).get("approval", [])
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
            continue
        for record in approvals if isinstance(approvals, list) else ():
            if isinstance(record, dict) and isinstance(record.get("target"), str):
                targets.add(record["target"])
    return targets


def validate_promoted_directory(
    plan: WaiverPromotionPlan, roots: CoverageRepositoryRoots
) -> ApprovedWaiverSet:
    """Load a directory the plan was applied to; raise if the result is invalid.

    Target identities are taken from the documents themselves: this checks the
    merge of destination and promotion (collisions, source digests, proofs),
    not the Target catalog, which Simulation validates on its next run.
    """
    approval_dir = Path(getattr(roots, plan.anchor)) / plan.directory
    known = _named_targets(approval_dir) | {item.target for item in plan.candidates}
    try:
        return load_approved_waiver_set(
            plan.config, roots, tuple(DurableTargetIdentity(item) for item in sorted(known))
        )
    except CoverageWaiverValidationError as exc:
        details = "; ".join(f"{item.code} {item.pointer}" for item in exc.findings[:5])
        raise WaiverPlanError(f"promoted approval directory is invalid: {details}") from exc


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
    campaign: CoverageCampaign, waivers: ApprovedWaiverSet
) -> EvaluatedCoverageCampaign:
    """Evaluate a persisted Campaign strictly against a new Approved Waiver Set."""
    return evaluate_coverage_campaign(
        _unwaived(campaign), criterion_from_evaluation(campaign), waivers
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
    "WaiverPromotionPlan",
    "apply_promotion_plan",
    "evaluation_json",
    "load_promoted_waiver_set",
    "strict_reevaluation",
    "validate_promoted_directory",
]

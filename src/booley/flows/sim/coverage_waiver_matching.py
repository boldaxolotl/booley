"""Pure transactional matching of approved waivers to one Coverage Campaign."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from booley.flows.sim.coverage_campaign import (
    CoverageCampaign,
    CoverageFinding,
    DurableTargetIdentity,
    FrozenJson,
)


@dataclass(frozen=True)
class ApprovedWaiver:
    """One validated exact Target-and-point exclusion."""

    target: DurableTargetIdentity
    point_id: str
    reason: str
    waiver_id: str
    waiver_file: str
    waiver_fingerprint: str
    provenance: Mapping[str, FrozenJson]
    source: str


@dataclass(frozen=True)
class ApprovedWaiverMatch:
    """All applicable waivers, or findings and no waivers."""

    waivers: tuple[ApprovedWaiver, ...]
    findings: tuple[CoverageFinding, ...]


@dataclass(frozen=True)
class ApprovedWaiverSet:
    """Immutable project-wide approved waiver input."""

    configuration: Mapping[str, FrozenJson]
    digest: str
    waivers: tuple[ApprovedWaiver, ...]

    def match(self, campaign: CoverageCampaign) -> ApprovedWaiverMatch:
        """Resolve applicable exact matches transactionally for one Campaign."""
        return _match_campaign(self.waivers, campaign)


def _error(code: str, pointer: str, message: str) -> CoverageFinding:
    return CoverageFinding(severity="error", code=code, pointer=pointer, message=message)


def _match_one(
    waiver: ApprovedWaiver, campaign: CoverageCampaign
) -> tuple[ApprovedWaiver | None, CoverageFinding | None]:
    pointer = f"/approved_waivers/{waiver.waiver_file}#{waiver.waiver_id}"
    points = [point for point in campaign.points if point.id == waiver.point_id]
    if not points:
        return None, _error(
            "COV_WAIVER_POINT_STALE", pointer, "Approval no longer matches a point."
        )
    if len(points) > 1:
        return None, _error(
            "COV_WAIVER_MATCH_AMBIGUOUS", pointer, "Approval matches multiple points."
        )
    point = points[0]
    if point.identity.location.get("source") != waiver.source:
        return None, _error(
            "COV_WAIVER_POINT_SOURCE_MISMATCH",
            pointer,
            "Matched point belongs to a different RTL source.",
        )
    if point.disposition.get("kind") != "eligible":
        return None, _error(
            "COV_WAIVER_POINT_UNSCORABLE",
            pointer,
            "Only V1-scored eligible RTL points can be waived.",
        )
    return waiver, None


def _match_campaign(
    waivers: tuple[ApprovedWaiver, ...], campaign: CoverageCampaign
) -> ApprovedWaiverMatch:
    if campaign.normalization.get("status") not in {
        "complete",
        "complete_with_unknown_records",
    }:
        return ApprovedWaiverMatch(waivers=(), findings=())
    applicable = [item for item in waivers if str(item.target) == campaign.target.identity]
    resolved = [_match_one(waiver, campaign) for waiver in applicable]
    findings = tuple(finding for _, finding in resolved if finding is not None)
    if findings:
        return ApprovedWaiverMatch(waivers=(), findings=findings)
    matched = tuple(waiver for waiver, _ in resolved if waiver is not None)
    return ApprovedWaiverMatch(waivers=matched, findings=())

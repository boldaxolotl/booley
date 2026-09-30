"""File-level coverage advisories, independent of scoring and point eligibility."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath

from .coverage_campaign import CoverageCampaign, CoverageFinding, FrozenJson
from .verilator_declarations import DeclarationInventory

GAP_CODE = "COV_RTL_SOURCE_WITHOUT_POINTS"
INCOMPLETE_CODE = "COV_RTL_SOURCE_DISCOVERY_INCOMPLETE"
_POINTER = re.compile(r"^/source_closure/rtl/(0|[1-9][0-9]*)/path$")


@dataclass(frozen=True)
class SourceGapSummary:
    status: str
    paths: tuple[str, ...]
    unusable_findings: int = 0
    comparison_status: str = "complete"


def _canonical_path(value: object) -> str | None:
    if not isinstance(value, str) or not value or "\\" in value:
        return None
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or path.as_posix() != value:
        return None
    return value


def source_gap_findings(
    inventory: DeclarationInventory | None,
    closure: Mapping[str, FrozenJson],
    present: tuple[str, ...],
    presence_diagnostics: tuple[str, ...],
) -> tuple[CoverageFinding, ...]:
    """Compare exhaustive declarations with all native records, including unknown types."""
    if inventory is None or inventory.status != "complete" or presence_diagnostics:
        return (
            CoverageFinding(
                "warning",
                INCOMPLETE_CODE,
                "/artifacts",
                "RTL source discovery is incomplete; sources without coverage points could not be determined.",
            ),
        )
    records = closure["rtl"]
    assert isinstance(records, tuple)
    eligible = {s.path: s for s in inventory.sources if s.eligible}
    modules = {d.source for d in inventory.declarations if d.kind == "MODULE"}
    candidates = modules & eligible.keys()
    indices = {}
    for index, record in enumerate(records):
        assert isinstance(record, Mapping)
        path = str(record["path"])
        if path in candidates:
            if (
                _canonical_path(path) is None
                or eligible[path].sha256 != record["sha256"]
                or path in indices
            ):
                raise ValueError("Declaration inventory differs from the producing source closure")
            indices[path] = index
    if candidates - indices.keys():
        raise ValueError("Module-bearing source is absent from the producing source closure")
    return tuple(
        CoverageFinding(
            "warning",
            GAP_CODE,
            f"/source_closure/rtl/{indices[path]}/path",
            f"RTL source {path} produced no coverage points.",
        )
        for path in sorted(candidates - set(present))
    )


def source_gap_summary(campaign: CoverageCampaign) -> SourceGapSummary:
    """Defensively resolve durable advisory pointers without parsing human messages."""
    artifacts = [a for a in campaign.artifacts if a.kind == "declaration_inventory"]
    status = (
        str(artifacts[0].attributes.get("discovery_status", "incomplete"))
        if len(artifacts) == 1
        else "not_recorded"
    )
    if any(f.code == INCOMPLETE_CODE for f in campaign.findings):
        status = "incomplete"
    records = campaign.source_closure.get("rtl", ())
    measurement_complete = (
        campaign.collection["status"] == "complete"
        and campaign.collector.native_format.get("compatibility") == "compatible"
    )
    paths = set()
    invalid = 0
    for finding in campaign.findings:
        if finding.code != GAP_CODE:
            continue
        match = _POINTER.fullmatch(finding.pointer)
        index = int(match[1]) if match else -1
        record = (
            records[index] if isinstance(records, tuple) and 0 <= index < len(records) else None
        )
        path = _canonical_path(record.get("path")) if isinstance(record, Mapping) else None
        if path is None or path in paths or status != "complete" or not measurement_complete:
            invalid += 1
        else:
            paths.add(path)
    comparison = "complete" if status == "complete" and measurement_complete else "unavailable"
    return SourceGapSummary(status, tuple(sorted(paths)), invalid, comparison)


def source_gap_overview(campaign: CoverageCampaign, *, preview: int = 50) -> dict[str, object]:
    summary = source_gap_summary(campaign)
    return {
        "discovery_status": summary.status,
        "comparison_status": summary.comparison_status,
        "total_sources": len(summary.paths),
        "paths": list(summary.paths[:preview]),
        "omitted": max(0, len(summary.paths) - preview),
        "unusable_findings": summary.unusable_findings,
        "query": {"view": "zero_point_sources", "limit": 50},
    }


def source_gap_report_lines(campaign: CoverageCampaign) -> list[str]:
    summary = source_gap_summary(campaign)
    if summary.status == "not_recorded":
        return []
    if summary.status != "complete":
        return ["RTL source discovery incomplete; sources without coverage points unavailable."]
    if summary.comparison_status != "complete":
        return []
    lines = [f"RTL sources without coverage points: {len(summary.paths)}"]
    lines.extend("  " + json.dumps(path, ensure_ascii=True)[:240] for path in summary.paths[:3])
    if len(summary.paths) > 3:
        lines.append(
            f"  {len(summary.paths) - 3} more; see coverage.json or query zero_point_sources."
        )
    if summary.unusable_findings:
        lines.append("Some RTL source advisory evidence is unusable.")
    return lines

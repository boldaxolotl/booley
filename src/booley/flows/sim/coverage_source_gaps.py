"""File-level coverage advisories, independent of scoring and point eligibility.

Candidates come from a lexical module scan at Preflight; a candidate is present
when any native record of any type maps to it through point source matching.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path

from booley.core.hdl import declares_module
from booley.targets.domain import TargetInspection

from .coverage_campaign import CoverageCampaign, CoverageFinding, FrozenJson

GAP_CODE = "COV_RTL_SOURCE_WITHOUT_POINTS"
_POINTER = re.compile(r"^/source_closure/rtl/(0|[1-9][0-9]*)/path$")
_VERILOG = frozenset({"verilogSource", "systemVerilogSource"})


def rtl_module_candidates(inspection: TargetInspection) -> tuple[str, ...]:
    """Sorted non-TB, non-include Verilog inputs whose text declares a module."""
    root = inspection.handle.project_root
    return tuple(
        sorted(
            Path(item.path).as_posix()
            for item in inspection.inputs
            if item.file_type in _VERILOG
            and not item.is_include
            and "tb" not in item.tags
            and declares_module((root / item.path).read_text("utf-8", errors="replace"))
        )
    )


def source_gap_findings(
    candidates: tuple[str, ...],
    closure: Mapping[str, FrozenJson],
    present: tuple[str, ...],
) -> tuple[CoverageFinding, ...]:
    """One warning per candidate that no native record of any type maps to."""
    records = closure["rtl"]
    assert isinstance(records, tuple)
    index = {record["path"]: i for i, record in enumerate(records) if isinstance(record, Mapping)}
    return tuple(
        CoverageFinding(
            "warning",
            GAP_CODE,
            f"/source_closure/rtl/{index[path]}/path",
            f"RTL source {path} produced no coverage points.",
        )
        for path in candidates
        if path not in present
    )


def source_gap_paths(campaign: CoverageCampaign) -> tuple[str, ...]:
    """Sorted source-closure paths named by published gap findings."""
    records = campaign.source_closure.get("rtl", ())
    assert isinstance(records, tuple)
    paths = set()
    for finding in campaign.findings:
        match = _POINTER.fullmatch(finding.pointer) if finding.code == GAP_CODE else None
        if match and int(match[1]) < len(records):
            record = records[int(match[1])]
            assert isinstance(record, Mapping)
            paths.add(str(record["path"]))
    return tuple(sorted(paths))


def source_gap_overview(campaign: CoverageCampaign, *, preview: int = 50) -> dict[str, object]:
    paths = source_gap_paths(campaign)
    return {
        "total_sources": len(paths),
        "paths": list(paths[:preview]),
        "omitted": max(0, len(paths) - preview),
        "query": {"view": "zero_point_sources", "limit": 50},
    }


def source_gap_report_lines(campaign: CoverageCampaign) -> list[str]:
    paths = source_gap_paths(campaign)
    if not paths:
        return []
    lines = [f"RTL sources without coverage points: {len(paths)}"]
    lines.extend("  " + json.dumps(path, ensure_ascii=True)[:240] for path in paths[:3])
    if len(paths) > 3:
        lines.append(f"  {len(paths) - 3} more; see coverage.json or query zero_point_sources.")
    return lines

"""Read canonical Campaign identity and verify optional source snapshots."""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from booley.core.boundary import BoundaryError, require_dict
from booley.flows.sim.campaign_reports import is_report_link, target_report_directory
from booley.flows.sim.coverage_campaign import (
    CoverageCampaign,
    FrozenJson,
    freeze_coverage_mapping,
)
from booley.flows.sim.coverage_campaign_store import (
    LoadedCoverageCampaign,
    load_coverage_campaign,
)


class CoverageAnalysisError(ValueError):
    """Campaign evidence cannot support advisory Coverage Analysis."""


@dataclass(frozen=True)
class CoverageSourceClosure:
    """Verified text snapshot of the producing Target's complete source closure."""

    target_identity: str
    files: Mapping[str, FrozenJson]


def read_coverage_campaign(path: Path) -> LoadedCoverageCampaign:
    """Require an exact canonical path and a matching completed Target projection."""
    path = path.absolute()
    try:
        _safe_path(path)
        if path.name != "coverage.json" or path.parent.parent.name != "targets":
            raise CoverageAnalysisError("Supply the exact canonical Target coverage.json path")
        invocation = path.parent.parent.parent
        if invocation.parent.name != "sim" or not invocation.name.isdecimal():
            raise CoverageAnalysisError(
                "Campaign must belong to one numbered Simulation invocation"
            )
        loaded = load_coverage_campaign(path)
        campaign = loaded.campaign
        if str(
            campaign.invocation["id"]
        ) != invocation.name or path.parent != target_report_directory(
            invocation, campaign.target.selector
        ):
            raise CoverageAnalysisError("Campaign identity disagrees with its exact path")
        _projection(path.parent / "simulation.json", campaign)
        return loaded
    except (OSError, ValueError, BoundaryError) as exc:
        raise CoverageAnalysisError(
            f"Cannot analyze Campaign at {path}: {exc}. Use an exact retained, completed Target Campaign; fully pruned invocations cannot be analyzed."
        ) from exc


def _safe_path(path: Path) -> None:
    if ".." in path.parts or any(is_report_link(item) for item in (path, *path.parents)):
        raise CoverageAnalysisError(
            "Campaign and source paths must not contain links or traversal"
        )
    if not path.is_file():
        raise CoverageAnalysisError(f"Expected a retained regular file: {path}")


def _projection(path: Path, campaign: CoverageCampaign) -> None:
    _safe_path(path)
    projection = require_dict(json.loads(path.read_text(encoding="utf-8")))
    expected = {
        "flow": "sim",
        "target": campaign.target.selector,
        "target_identity": campaign.target.identity,
        "collection": campaign.collection["status"],
        "evaluation": campaign.evaluation["status"],
    }
    if projection.get("complete") is not True or any(
        projection.get(key) != value for key, value in expected.items()
    ):
        raise CoverageAnalysisError("Campaign lacks a matching completed Simulation projection")


def coverage_sources(
    campaign: CoverageCampaign, project_root: Path
) -> CoverageSourceClosure | None:
    """Return the complete verified closure, or report-only on any mismatch."""
    from booley.flows.sim.coverage_provenance import content_digest, coverage_digest
    from booley.fusesoc.core_projection import projection_enabled
    from booley.targets.catalog import TargetCatalog
    from booley.targets.domain import FuseSocError

    try:
        # FuseSoC inspection reconciles projected/isolated cores in stealth mode.
        # Advisory source access must not prepare or change that project state.
        if projection_enabled(project_root):
            raise CoverageAnalysisError("Source inspection requires project reconciliation")
        catalog = TargetCatalog.build(project_root)
        handle = catalog.select(campaign.target.identity)
        _safe_path(handle.core_file)
        fingerprint = coverage_digest(
            {"core": handle.core_file.read_text(), "identity": handle.identity}
        )
        if fingerprint != campaign.fingerprints["target_definition"]:
            return None
        inspection = catalog.inspect(handle)
        expected = {"rtl": [], "testbench": []}
        files = {}
        for item in sorted(inspection.inputs, key=lambda item: item.path):
            path = project_root / item.path
            if path.suffix.lower() in {".fst", ".vcd"}:
                return None
            _safe_path(path)
            if not path.resolve().is_relative_to(project_root.resolve()):
                return None
            category = "testbench" if "tb" in item.tags else "rtl"
            data = path.read_bytes()
            digest = content_digest(data)
            name = Path(item.path).as_posix()
            expected[category].append({"path": name, "sha256": digest})
            files[name] = {"category": category, "sha256": digest, "text": data.decode("utf-8")}
        for category in ("rtl", "testbench"):
            if (
                coverage_digest(expected[category]) != campaign.fingerprints[category + "_sources"]
                or tuple(expected[category]) != campaign.source_closure[category]
            ):
                return None
        return CoverageSourceClosure(handle.identity, freeze_coverage_mapping(files))
    except (OSError, ValueError, FuseSocError):
        return None


def verified_source_snapshot(
    campaign: CoverageCampaign, sources: CoverageSourceClosure | None
) -> CoverageSourceClosure | None:
    """Return a source snapshot only when every Campaign-bound digest agrees."""
    from booley.flows.sim.coverage_provenance import content_digest, coverage_digest

    if sources is None or sources.target_identity != campaign.target.identity:
        return None
    expected = {}
    for category in ("rtl", "testbench"):
        records = cast(tuple[Mapping[str, str], ...], campaign.source_closure[category])
        if coverage_digest(records) != campaign.fingerprints[category + "_sources"]:
            return None
        for record in records:
            expected[record["path"]] = (category, record["sha256"])
    if set(expected) != set(sources.files):
        return None
    for path, (category, digest) in expected.items():
        item = sources.files[path]
        text = item.get("text") if isinstance(item, Mapping) else None
        if not isinstance(item, Mapping) or not isinstance(text, str):
            return None
        if (
            item.get("category") != category
            or item.get("sha256") != digest
            or content_digest(text.encode("utf-8")) != digest
        ):
            return None
    return CoverageSourceClosure(sources.target_identity, freeze_coverage_mapping(sources.files))

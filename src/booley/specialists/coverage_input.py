"""Read canonical Campaign identity and optional verified source snapshots."""

import json
from pathlib import Path

from booley.core.boundary import BoundaryError, require_dict, require_str
from booley.flows.sim.campaign_reports import is_report_link, target_report_directory
from booley.flows.sim.coverage_campaign import (
    CoverageCampaign,
    DurableTargetIdentity,
    decode_coverage_campaign,
)

from .coverage_analysis import CoverageAnalysisError, CoverageSourceClosure


def read_coverage_campaign(path: Path) -> CoverageCampaign:
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
        document = require_dict(json.loads(path.read_text(encoding="utf-8")))
        identity = require_str(require_dict(document.get("target")), "identity")
        campaign = decode_coverage_campaign(document, DurableTargetIdentity(identity))
        if str(
            campaign.invocation["id"]
        ) != invocation.name or path.parent != target_report_directory(
            invocation, campaign.target.selector
        ):
            raise CoverageAnalysisError("Campaign identity disagrees with its exact path")
        _projection(path.parent / "simulation.json", campaign)
        return campaign
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
    from booley.flows.sim.coverage_campaign import freeze_coverage_mapping
    from booley.flows.sim.coverage_provenance import content_digest, coverage_digest
    from booley.targets.catalog import TargetCatalog
    from booley.targets.domain import FuseSocError

    try:
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

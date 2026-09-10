"""Compatibility imports for the Simulation-owned Coverage Analysis input seam."""

from booley.flows.sim.coverage_analysis_input import (
    CoverageAnalysisError,
    CoverageSourceClosure,
    coverage_sources,
    read_coverage_campaign,
    verified_source_snapshot,
)

__all__ = [
    "CoverageAnalysisError",
    "CoverageSourceClosure",
    "coverage_sources",
    "read_coverage_campaign",
    "verified_source_snapshot",
]

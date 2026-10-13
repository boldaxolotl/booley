"""Simulator family names, shared by the Simulation Flow and Flow-neutral callers."""

from __future__ import annotations

__all__ = ["normalize_eda_tool"]


def normalize_eda_tool(eda_tool: str | None) -> str:
    """Normalize a Target's ``flow_options.tool`` to an EDA family name."""
    lowered = str(eda_tool or "").strip().lower()
    if lowered in {"icarus", "iverilog"}:
        return "icarus"
    if lowered == "verilator":
        return "verilator"
    if lowered in {"xcelium", "xrun"}:
        return "xcelium"
    if lowered == "vcs":
        return "vcs"
    raise ValueError(f"unknown simulator EDA tool {eda_tool!r}")

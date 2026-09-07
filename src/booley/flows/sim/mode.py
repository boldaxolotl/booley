"""Canonical mode vocabulary for the built-in Simulation Flow."""

from __future__ import annotations

import argparse
from enum import StrEnum


class SimulationMode(StrEnum):
    """One valid Simulation execution shape."""

    SIMULATE = "simulate"
    ELAB_ONLY = "elab_only"
    ELAB_ONLY_STANDALONE = "elab_only_standalone"

    @property
    def elaborates_only(self) -> bool:
        """Whether this mode stops before running Simulation tests."""
        return self is not SimulationMode.SIMULATE

    @property
    def includes_standalone(self) -> bool:
        """Whether this mode includes the reusable-module sweep."""
        return self is SimulationMode.ELAB_ONLY_STANDALONE


def parse_simulation_mode(value: str) -> SimulationMode:
    """Accept MCP underscore values and their hyphenated CLI spellings."""
    normalized = value.strip().lower().replace("-", "_")
    try:
        return SimulationMode(normalized)
    except ValueError as exc:
        choices = ", ".join(mode.value.replace("_", "-") for mode in SimulationMode)
        raise argparse.ArgumentTypeError(f"must be one of {choices}") from exc

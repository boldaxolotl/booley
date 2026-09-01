"""Canonical synthesis modes and their mode-owned reporting policy."""

from __future__ import annotations

from enum import StrEnum


class SynthMode(StrEnum):
    """Depth of an ASIC synthesis run.

    ``StrEnum`` keeps configuration, argparse, and JSON interfaces compatible
    with their public string values while giving internal callers a closed
    domain with mode-owned policy.
    """

    PHYSICAL = "physical"
    LOGICAL = "logical"

    @property
    def runs_openroad(self) -> bool:
        """Whether the run must produce OpenROAD area and timing evidence."""
        return self is SynthMode.PHYSICAL

    @property
    def area_source(self) -> str:
        """Canonical provenance label for this mode's final area."""
        if self is SynthMode.PHYSICAL:
            return "openroad_post_optimization"
        return "yosys_mapped"


SYNTH_MODE_CHOICES = tuple(mode.value for mode in SynthMode)


def runs_openroad(mode: SynthMode | str) -> bool:
    """Return physical-mode policy for typed or legacy string callers."""
    return SynthMode(mode).runs_openroad

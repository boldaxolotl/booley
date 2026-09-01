"""OpenROAD translation of generic synthesis PPA profiles."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from booley.flows.synth.profiles import validate_ppa_profile


@dataclass(frozen=True)
class OpenRoadPpaSettings:
    """OpenROAD-specific placement and timing-repair controls."""

    utilization_pct: float
    placement_density: float
    repair_setup: bool = True
    repair_hold: bool = False
    gate_cloning: bool = False
    setup_margin_ns: float = 0.0
    repair_tns_percent: float | None = None


_PROFILES = {
    "compact": OpenRoadPpaSettings(utilization_pct=40.0, placement_density=0.65),
    "balanced": OpenRoadPpaSettings(utilization_pct=50.0, placement_density=0.75),
    "max_frequency": OpenRoadPpaSettings(utilization_pct=50.0, placement_density=0.75),
}


def openroad_profile(profile: str) -> OpenRoadPpaSettings:
    """Translate a generic profile into OpenROAD defaults."""
    return _PROFILES[validate_ppa_profile(profile)]


def with_openroad_overrides(
    base: OpenRoadPpaSettings,
    **overrides: Any,
) -> OpenRoadPpaSettings:
    """Apply already boundary-validated OpenROAD expert overrides."""
    return replace(base, **{key: value for key, value in overrides.items() if value is not None})

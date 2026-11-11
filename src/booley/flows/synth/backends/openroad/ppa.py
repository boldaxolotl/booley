"""OpenROAD translation of generic synthesis PPA profiles."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from booley.flows.implementation_profiles import validate_ppa_profile
from booley.flows.synth.timing import OpenRoadPpaSettings

_PROFILES = {
    "compact": OpenRoadPpaSettings(utilization_pct=40.0, placement_density=0.65),
    "balanced": OpenRoadPpaSettings(utilization_pct=50.0, placement_density=0.80),
    "max_frequency": OpenRoadPpaSettings(utilization_pct=50.0, placement_density=0.80),
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

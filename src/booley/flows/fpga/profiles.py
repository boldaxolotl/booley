"""Vivado mappings for portable FPGA optimization intent."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from booley.core.boundary import BoundaryError
from booley.flows.implementation_profiles import DEFAULT_PPA_PROFILE, validate_ppa_profile


@dataclass(frozen=True)
class VivadoProfile:
    """One validated portable profile and its characterized Vivado mapping."""

    name: str
    synthesis_strategy: str
    implementation_strategy: str
    apply_strategy: bool = True

    def as_dict(self) -> dict[str, Any]:
        """Return stable recipe evidence for plans, reports, and fingerprints."""
        return {
            "name": self.name,
            "adapter": "vivado",
            "mapping": {
                "synthesis_strategy": self.synthesis_strategy,
                "implementation_strategy": self.implementation_strategy,
                "applied": self.apply_strategy,
            },
        }


VIVADO_PROFILES: dict[str, VivadoProfile] = {
    "compact": VivadoProfile(
        "compact",
        "Flow_AreaOptimized_high",
        "Area_Explore",
    ),
    "balanced": VivadoProfile(
        "balanced",
        "Vivado Synthesis Defaults",
        "Vivado Implementation Defaults",
        apply_strategy=False,
    ),
    "max_frequency": VivadoProfile(
        "max_frequency",
        "Flow_PerfOptimized_high",
        "Performance_ExplorePostRoutePhysOpt",
    ),
}


def resolve_fpga_profile(
    flow_options: Mapping[str, Any],
    *,
    override: Any = None,
    target: str,
) -> VivadoProfile:
    """Resolve call override, Target setting, then the portable default."""
    configured = flow_options.get("ppa_profile", DEFAULT_PPA_PROFILE)
    value = override if override is not None else configured
    field = (
        "ppa_profile" if override is not None else f"Target {target!r} flow_options.ppa_profile"
    )
    return VIVADO_PROFILES[validate_ppa_profile(value, field=field)]


def validate_vivado_profile(profile: VivadoProfile) -> VivadoProfile:
    """Reject an uncharacterized or altered adapter mapping before Tcl output."""
    if VIVADO_PROFILES.get(profile.name) != profile:
        raise BoundaryError(f"unsupported Vivado mapping for ppa_profile {profile.name!r}")
    return profile

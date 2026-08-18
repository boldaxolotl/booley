"""Yosys and OpenROAD translations of generic synthesis PPA profiles."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from booley.core.boundary import BoundaryError
from booley.synthesis_profiles import validate_ppa_profile

ABC_RECIPE_CHOICES = ("default", "balanced", "fast")


@dataclass(frozen=True)
class YosysPpaSettings:
    """Yosys-specific mapping controls resolved for one run."""

    abc_recipe: str
    abc_script: str | None = None
    generic_abc_before_mapping: bool = False
    abc_delay_ps: int | None = None


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


_YOSYS_PROFILES = {
    "compact": YosysPpaSettings(abc_recipe="default"),
    "balanced": YosysPpaSettings(abc_recipe="balanced"),
    "max_frequency": YosysPpaSettings(abc_recipe="fast"),
}

_OPENROAD_PROFILES = {
    "compact": OpenRoadPpaSettings(utilization_pct=40.0, placement_density=0.65),
    "balanced": OpenRoadPpaSettings(utilization_pct=50.0, placement_density=0.75),
    "max_frequency": OpenRoadPpaSettings(utilization_pct=50.0, placement_density=0.75),
}


def yosys_profile(profile: str) -> YosysPpaSettings:
    """Translate a generic profile into Yosys mapping defaults."""
    return _YOSYS_PROFILES[validate_ppa_profile(profile)]


def openroad_profile(profile: str) -> OpenRoadPpaSettings:
    """Translate a generic profile into OpenROAD defaults."""
    return _OPENROAD_PROFILES[validate_ppa_profile(profile)]


def validate_abc_recipe(value: Any, *, field: str = "abc_recipe") -> str:
    """Return a supported named ABC recipe or raise."""
    if not isinstance(value, str) or value not in ABC_RECIPE_CHOICES:
        choices = ", ".join(ABC_RECIPE_CHOICES)
        raise BoundaryError(f"{field} must be one of {choices}; got {value!r}")
    return value


def validate_abc_script(value: Any, *, field: str = "abc_script") -> str:
    """Validate Yosys's raw ``abc -script +...`` form."""
    if not isinstance(value, str) or len(value) < 2 or not value.startswith("+"):
        raise BoundaryError(f"{field} must be a non-empty ABC script beginning with '+'")
    return value


def with_yosys_overrides(
    base: YosysPpaSettings,
    *,
    abc_recipe: str | None = None,
    abc_script: str | None = None,
    generic_abc_before_mapping: bool | None = None,
    abc_delay_ps: int | None = None,
) -> YosysPpaSettings:
    """Apply validated expert overrides to profile-derived Yosys settings."""
    if abc_recipe is not None and abc_script is not None:
        raise BoundaryError("abc_recipe and abc_script are mutually exclusive")
    recipe = validate_abc_recipe(abc_recipe) if abc_recipe is not None else base.abc_recipe
    script = validate_abc_script(abc_script) if abc_script is not None else base.abc_script
    if abc_recipe is not None:
        script = None
    return replace(
        base,
        abc_recipe=recipe,
        abc_script=script,
        generic_abc_before_mapping=(
            generic_abc_before_mapping
            if generic_abc_before_mapping is not None
            else base.generic_abc_before_mapping
        ),
        abc_delay_ps=abc_delay_ps if abc_delay_ps is not None else base.abc_delay_ps,
    )


def with_openroad_overrides(
    base: OpenRoadPpaSettings,
    **overrides: Any,
) -> OpenRoadPpaSettings:
    """Apply already boundary-validated OpenROAD expert overrides."""
    return replace(base, **{key: value for key, value in overrides.items() if value is not None})

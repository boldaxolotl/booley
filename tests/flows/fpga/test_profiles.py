"""Portable FPGA profile resolution and characterized Vivado mappings."""

from __future__ import annotations

import pytest

from booley.core.boundary import BoundaryError
from booley.flows.fpga.profiles import (
    VIVADO_PROFILES,
    VivadoProfile,
    resolve_fpga_profile,
    validate_vivado_profile,
)


@pytest.mark.parametrize(
    ("flow_options", "override", "expected"),
    [
        ({}, None, "balanced"),
        ({"ppa_profile": "compact"}, "max_frequency", "max_frequency"),
        # Engine fields are not profile overrides.
        ({"synth": "vivado", "pnr": "vivado"}, None, "balanced"),
    ],
    ids=["default", "call-overrides-target", "engine-fields"],
)
def test_profile_resolution(flow_options: dict, override: str | None, expected: str) -> None:
    profile = resolve_fpga_profile(flow_options, override=override, target="fpga_core")

    assert profile is VIVADO_PROFILES[expected]


@pytest.mark.parametrize("value", ["speed", "area", "", 1, True, None])
def test_invalid_target_profile_fails_at_boundary(value: object) -> None:
    with pytest.raises(BoundaryError, match=r"Target 'fpga_core' flow_options\.ppa_profile"):
        resolve_fpga_profile({"ppa_profile": value}, target="fpga_core")


def test_uncharacterized_adapter_mapping_is_rejected() -> None:
    with pytest.raises(BoundaryError, match="unsupported Vivado mapping"):
        validate_vivado_profile(VivadoProfile("compact", "arbitrary", "Area_Explore"))

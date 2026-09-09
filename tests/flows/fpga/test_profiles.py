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


def test_default_profile_preserves_existing_vivado_defaults() -> None:
    profile = resolve_fpga_profile({}, target="fpga_core")

    assert profile is VIVADO_PROFILES["balanced"]
    assert profile.apply_strategy is False
    assert profile.synthesis_strategy == "Vivado Synthesis Defaults"
    assert profile.implementation_strategy == "Vivado Implementation Defaults"


def test_call_profile_overrides_target_profile() -> None:
    profile = resolve_fpga_profile(
        {"ppa_profile": "compact"},
        override="max_frequency",
        target="fpga_core",
    )

    assert profile is VIVADO_PROFILES["max_frequency"]


def test_call_override_is_symmetric_for_candidate_and_baseline() -> None:
    candidate = resolve_fpga_profile(
        {"ppa_profile": "compact"},
        override="balanced",
        target="candidate",
    )
    baseline = resolve_fpga_profile(
        {"ppa_profile": "max_frequency"},
        override="balanced",
        target="baseline",
    )

    assert candidate is baseline is VIVADO_PROFILES["balanced"]


def test_synth_and_pnr_engine_fields_are_not_profile_overrides() -> None:
    profile = resolve_fpga_profile(
        {"synth": "vivado", "pnr": "vivado"},
        target="fpga_core",
    )

    assert profile is VIVADO_PROFILES["balanced"]


@pytest.mark.parametrize("value", ["speed", "area", "", 1, True, None])
def test_invalid_target_profile_fails_at_boundary(value: object) -> None:
    with pytest.raises(BoundaryError, match=r"Target 'fpga_core' flow_options\.ppa_profile"):
        resolve_fpga_profile({"ppa_profile": value}, target="fpga_core")


def test_uncharacterized_adapter_mapping_is_rejected() -> None:
    with pytest.raises(BoundaryError, match="unsupported Vivado mapping"):
        validate_vivado_profile(VivadoProfile("compact", "arbitrary", "Area_Explore"))

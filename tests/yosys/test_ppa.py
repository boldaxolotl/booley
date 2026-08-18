"""Profile translations and expert-override validation."""

from __future__ import annotations

import pytest

from booley.core.boundary import BoundaryError
from booley.synthesis_profiles import validate_ppa_profile
from booley.yosys import ppa


@pytest.mark.parametrize(
    ("profile", "abc", "util", "density"),
    [
        ("compact", "default", 40.0, 0.65),
        ("balanced", "balanced", 50.0, 0.75),
        ("max_frequency", "fast", 50.0, 0.75),
    ],
)
def test_profile_backend_translations(profile, abc, util, density):
    assert ppa.yosys_profile(profile).abc_recipe == abc
    openroad = ppa.openroad_profile(profile)
    assert openroad.utilization_pct == util
    assert openroad.placement_density == density


def test_invalid_profile_rejected():
    with pytest.raises(BoundaryError, match="must be one of"):
        validate_ppa_profile("small")


def test_raw_script_and_named_recipe_are_mutually_exclusive():
    with pytest.raises(BoundaryError, match="mutually exclusive"):
        ppa.with_yosys_overrides(
            ppa.yosys_profile("balanced"),
            abc_recipe="fast",
            abc_script="+strash;map",
        )


def test_raw_abc_script_must_not_be_empty():
    with pytest.raises(BoundaryError, match="non-empty"):
        ppa.validate_abc_script("+")

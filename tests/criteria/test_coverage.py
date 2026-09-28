from __future__ import annotations

import math

import pytest

from booley.criteria.coverage import validate_coverage_metrics


def test_unexpected_policy_keys_are_safe_and_deterministic() -> None:
    with pytest.raises(
        ValueError,
        match=r"unexpected policy keys include 1, 'max_pct'; only 'min_pct' is allowed",
    ):
        validate_coverage_metrics(
            {"line": {"min_pct": 90, "max_pct": 95, 1: "unexpected"}},
            field="COVERAGE",
        )


@pytest.mark.parametrize("threshold", [math.nan, math.inf, -math.inf])
def test_nonfinite_threshold_is_reported_as_a_finiteness_error(threshold: float) -> None:
    with pytest.raises(ValueError, match=r"line\.min_pct must be finite"):
        validate_coverage_metrics(
            {"line": {"min_pct": threshold}},
            field="COVERAGE",
        )

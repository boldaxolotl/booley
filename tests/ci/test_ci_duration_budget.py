from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[2] / ".github/scripts/ci_duration_budget.py"
SPEC = importlib.util.spec_from_file_location("ci_duration_budget", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
ci_duration_budget = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ci_duration_budget)


def test_duration_within_budget_passes() -> None:
    result = ci_duration_budget.evaluate("bwave-smoke", started_at=100, now=1_179, budget=1_080)

    assert result.elapsed_seconds == 1_079
    assert result.passed is True
    assert "PASS" in ci_duration_budget.summary_line(result)


def test_duration_over_budget_fails() -> None:
    result = ci_duration_budget.evaluate("bwave-smoke", started_at=100, now=1_181, budget=1_080)

    assert result.elapsed_seconds == 1_081
    assert result.passed is False
    assert "FAIL" in ci_duration_budget.summary_line(result)


@pytest.mark.parametrize(
    ("started_at", "now", "budget", "message"),
    [
        (0, 100, 10, "started_at"),
        (100, 99, 10, "precedes"),
        (100, 101, 0, "budget"),
    ],
)
def test_duration_rejects_invalid_boundaries(
    started_at: int, now: int, budget: int, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        ci_duration_budget.evaluate("bwave-smoke", started_at=started_at, now=now, budget=budget)

"""Unit tests for the shared test time-budget policy."""

from __future__ import annotations

import pytest

from tests.time_budget import SCALE_VARIABLE, TimeScaleError, hang_guard_s, scaled


def test_unset_time_scale_keeps_the_base_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(SCALE_VARIABLE, raising=False)

    assert scaled(1.5) == 1.5
    assert hang_guard_s() == 30


def test_time_scale_multiplies_both_budget_kinds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SCALE_VARIABLE, "2")
    assert scaled(1.5) == 3.0
    assert hang_guard_s() == 60

    monkeypatch.setenv(SCALE_VARIABLE, "1.25")
    assert hang_guard_s() == 38


@pytest.mark.parametrize("raw", ["abc", "0", "nan", "1e308"])
def test_invalid_time_scale_is_refused(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv(SCALE_VARIABLE, raw)

    with pytest.raises(
        TimeScaleError, match=rf"{SCALE_VARIABLE}={raw!r} is not a positive finite"
    ):
        scaled(1)

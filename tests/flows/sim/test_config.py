"""Simulation Project-configuration boundary tests."""

from __future__ import annotations

import pytest

from booley.core.boundary import BoundaryError
from booley.flows.sim import config


def test_build_timeout_defaults_to_one_hour(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "_sim_config", lambda _work_dir: {})

    assert config.resolve_sim_build_timeout_ms() == 3_600_000


def test_build_timeout_accepts_positive_integer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        config,
        "_sim_config",
        lambda _work_dir: {"build_timeout_ms": 1_234_567},
    )

    assert config.resolve_sim_build_timeout_ms() == 1_234_567


@pytest.mark.parametrize("value", [True, 1.5, "1000", 0, -1])
def test_build_timeout_rejects_non_positive_integer(
    monkeypatch: pytest.MonkeyPatch,
    value: object,
) -> None:
    monkeypatch.setattr(
        config,
        "_sim_config",
        lambda _work_dir: {"build_timeout_ms": value},
    )

    with pytest.raises(BoundaryError, match=r"\[flows\.sim\]\.build_timeout_ms"):
        config.resolve_sim_build_timeout_ms()

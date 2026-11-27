"""The one fake of ``compute_memory_budget``: tests replace it only through this module.

It is patched where Memory Admission (``ADMISSION``) and the Booley Dashboard's web snapshot
(``DASHBOARD``) import it. ``tests/conftest.py`` calls ``measure_or_fall_back`` for every test
and wraps ``pin_budget`` as the ``pin_memory_budget`` fixture.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from booley.runtime.memory_budget import MemoryBudget

MIB = 1024**2
ADMISSION = "booley.flows.memory_admission"
DASHBOARD = "booley.harness.dashboard.web_snapshot"


def budget_of(byte_count: int, *, swap_bytes: int = 0) -> MemoryBudget:
    """A computed-rule ``MemoryBudget`` of exactly *byte_count* bytes.

    Without swap it is the limit minus a quarter of that limit as headroom; with *swap_bytes* the
    Sandbox holds no headroom back.
    """
    # Imported here: conftest imports this module before it puts ``src/`` on the path.
    from booley.runtime.memory_budget import (
        BudgetRule,
        MemoryBudget,
        ReservePart,
        ReserveRole,
        ReserveSource,
    )

    if swap_bytes:
        return MemoryBudget(byte_count, BudgetRule.LIMIT, byte_count, None, (), swap_bytes)
    # The limit is byte_count plus its quarter-of-the-limit headroom (a third of byte_count).
    headroom_bytes = byte_count // 3
    headroom = ReservePart(ReserveRole.HEADROOM, headroom_bytes, ReserveSource.FIXED, None)
    return MemoryBudget(
        byte_count, BudgetRule.LIMIT, byte_count + headroom_bytes, None, (headroom,)
    )


def measure_or_fall_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let admission measure the host, with a fixed 64 GiB budget where it cannot (Windows)."""
    from booley.runtime import memory_budget

    real_compute = memory_budget.compute_memory_budget

    def compute_or_fixed(**kwargs: Any) -> MemoryBudget:
        try:
            return real_compute(**kwargs)
        except OSError:
            return budget_of(64 * 1024**3)

    module = importlib.import_module(ADMISSION)
    monkeypatch.setattr(module, "compute_memory_budget", compute_or_fixed)


def pin_budget(
    monkeypatch: pytest.MonkeyPatch, mib: int, *, swap_mib: int = 0, importer: str = ADMISSION
) -> None:
    """Make *importer* see a Memory Budget of *mib* MiB."""
    budget = budget_of(mib * MIB, swap_bytes=swap_mib * MIB)
    module = importlib.import_module(importer)
    monkeypatch.setattr(module, "compute_memory_budget", lambda **_: budget)


def fail_budget(monkeypatch: pytest.MonkeyPatch, *, importer: str = ADMISSION) -> None:
    """Make the Memory Budget uncomputable for *importer*."""

    def unreadable(**_: object) -> MemoryBudget:
        raise OSError("MemTotal unreadable")

    module = importlib.import_module(importer)
    monkeypatch.setattr(module, "compute_memory_budget", unreadable)

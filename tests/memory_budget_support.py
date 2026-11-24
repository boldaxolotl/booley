"""A fixed Memory Budget for admission tests, set at the budget-computation seam.

No ``booley.toml`` key sets the budget and the host's memory must not matter, so
tests pin what ``booley.flows.memory_admission.compute_memory_budget`` returns.
The ``pin_memory_budget`` fixture wraps ``pin_budget`` for test functions;
helpers that already hold a ``monkeypatch`` call ``pin_budget`` directly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from booley.runtime.memory_budget import MemoryBudget

MIB = 1024**2


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


def pin_budget(monkeypatch: pytest.MonkeyPatch, mib: int) -> None:
    """Make admission see a Memory Budget of *mib* MiB."""
    from booley.flows import memory_admission

    monkeypatch.setattr(
        memory_admission, "compute_memory_budget", lambda **_: budget_of(mib * MIB)
    )


def fail_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the Memory Budget uncomputable, so every admitted Job carries a warning."""
    from booley.flows import memory_admission

    def unreadable(**_: object) -> MemoryBudget:
        raise OSError("MemTotal unreadable")

    monkeypatch.setattr(memory_admission, "compute_memory_budget", unreadable)

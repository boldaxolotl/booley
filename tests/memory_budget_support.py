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


def budget_of(byte_count: int) -> MemoryBudget:
    """A computed-rule ``MemoryBudget`` of exactly *byte_count* bytes (limit minus headroom)."""
    # Imported here: conftest imports this module before it puts ``src/`` on the path.
    from booley.audit.resource_policy import MEMORY_HEADROOM_BYTES
    from booley.runtime.memory_budget import (
        BudgetRule,
        MemoryBudget,
        ReservePart,
        ReserveRole,
        ReserveSource,
    )

    headroom = ReservePart(ReserveRole.HEADROOM, MEMORY_HEADROOM_BYTES, ReserveSource.FIXED, None)
    return MemoryBudget(
        byte_count, BudgetRule.LIMIT, byte_count + MEMORY_HEADROOM_BYTES, None, (headroom,)
    )


def pin_budget(monkeypatch: pytest.MonkeyPatch, mib: int) -> None:
    """Make admission see a Memory Budget of *mib* MiB."""
    from booley.flows import memory_admission

    monkeypatch.setattr(
        memory_admission, "compute_memory_budget", lambda **_: budget_of(mib * MIB)
    )

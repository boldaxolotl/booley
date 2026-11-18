"""The default pass threshold of a mutation run (ADR 0088)."""

from __future__ import annotations


def default_min_detected(count: int) -> int:
    """Return ``ceil(0.8 * count)``, in integers so that float rounding cannot move it."""
    return (4 * count + 4) // 5

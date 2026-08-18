"""Shared policies for concise human-facing Booley Flow output."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TypeVar

_T = TypeVar("_T")

MAX_VISIBLE_TARGETS = 3


def cap_target_items(items: Sequence[_T]) -> tuple[Sequence[_T], str | None]:
    """Return the visible target prefix and an omission marker, if needed."""
    hidden = len(items) - MAX_VISIBLE_TARGETS
    marker = f"... and {hidden} more target{'s' if hidden != 1 else ''}" if hidden > 0 else None
    return items[:MAX_VISIBLE_TARGETS], marker

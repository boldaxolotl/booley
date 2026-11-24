"""Fixtures shared by the feedback tests."""

from __future__ import annotations

import itertools

import pytest

from booley.core import file_lock


@pytest.fixture
def lock_wait_expires(monkeypatch):
    """Make a contended file-lock wait time out after one attempt, with no real wait.

    ``wait_for_file_lock`` reads ``file_lock.monotonic``; a clock that jumps far
    past any deadline on every reading ends the wait at once. Starting at 0.0
    means a lone first reading still looks like the start of the wait.
    """
    ticks = itertools.count(step=1_000_000.0)
    monkeypatch.setattr(file_lock, "monotonic", lambda: next(ticks))

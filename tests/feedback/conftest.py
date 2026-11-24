"""Fixtures shared by the feedback tests."""

from __future__ import annotations

import itertools

import pytest

from booley.core import file_lock


@pytest.fixture
def lock_wait_expires(monkeypatch):
    """Make a contended file-lock wait time out after one attempt and one short sleep.

    ``wait_for_file_lock`` reads ``file_lock.monotonic``. A clock that steps 3
    seconds per reading puts the first contended reading inside the findings
    deadline and the next loop check past it, whatever reading the wait starts on.
    """
    ticks = itertools.count(step=3.0)
    monkeypatch.setattr(file_lock, "monotonic", lambda: next(ticks))

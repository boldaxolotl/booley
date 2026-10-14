"""Isolation fixtures for review-domain tests."""

from __future__ import annotations

import pytest

from booley.runtime.booley_dir import reset_cache


@pytest.fixture(autouse=True)
def _reset_booley_dir_cache() -> None:
    reset_cache()
    yield
    reset_cache()

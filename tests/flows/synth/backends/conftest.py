"""Shared fixtures for yosys unit tests."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _set_booley_dir(tmp_path, monkeypatch):
    """Prevent resolve_booley_dir() from failing in yosys tests."""
    from booley.runtime.booley_dir import reset_cache

    reset_cache()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BOOLEY_DIR", str(tmp_path))

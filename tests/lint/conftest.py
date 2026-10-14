"""Shared fixtures for lint unit tests."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _set_booley_dir(tmp_path, monkeypatch):
    """Prevent resolve_booley_dir() from failing in lint tests."""
    from booley.runtime.booley_dir import reset_cache

    reset_cache()
    proj_dir = tmp_path / ".booley"
    proj_dir.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BOOLEY_DIR", str(proj_dir))

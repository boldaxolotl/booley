"""Shared fixtures for lint unit tests."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _set_project_dir(tmp_path, monkeypatch):
    """Prevent resolve_project_dir() from failing in lint tests."""
    from booley.runtime.project_dir import reset_cache

    reset_cache()
    proj_dir = tmp_path / ".booley_project"
    proj_dir.mkdir()
    monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(proj_dir))

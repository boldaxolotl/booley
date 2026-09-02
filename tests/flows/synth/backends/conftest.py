"""Shared fixtures for yosys unit tests."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _set_project_dir(tmp_path, monkeypatch):
    """Prevent resolve_project_dir() from failing in yosys tests."""
    from booley.runtime.project_dir import reset_cache

    reset_cache()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(tmp_path))

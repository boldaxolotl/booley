"""Fixtures for Booley Flow tests."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _set_project_dir(tmp_path, monkeypatch):
    """Keep Flow tests independent of a locally initialized project."""
    from booley.runtime.project_dir import reset_cache

    reset_cache()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(tmp_path))

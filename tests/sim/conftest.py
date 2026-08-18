"""Shared fixtures for sim unit tests."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _set_project_dir(tmp_path, monkeypatch):
    """Prevent resolve_project_dir() from failing in sim tests."""
    from booley.runtime.project_dir import reset_cache

    reset_cache()
    proj_dir = tmp_path / ".booley_project"
    proj_dir.mkdir()
    (proj_dir / "booley.toml").write_text('[flows.sim]\nsandbox = "sandboxed"\n', encoding="utf-8")
    monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(proj_dir))

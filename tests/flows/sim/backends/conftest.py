"""Shared fixtures for sim unit tests."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _set_booley_dir(tmp_path, monkeypatch):
    """Prevent resolve_booley_dir() from failing in sim tests."""
    from booley.runtime.booley_dir import reset_cache

    reset_cache()
    proj_dir = tmp_path / ".booley"
    proj_dir.mkdir()
    (proj_dir / "booley.toml").write_text('[flows.sim]\nsandbox = "sandboxed"\n', encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BOOLEY_DIR", str(proj_dir))

"""Fixtures for Booley Flow tests."""

from __future__ import annotations

import pytest


@pytest.fixture
def _flow_project_root(tmp_path):
    """Select the synthetic checkout; override with None for real discovery."""
    return tmp_path


@pytest.fixture(autouse=True)
def _set_project_dir(tmp_path, monkeypatch):
    """Keep Flow tests independent of a locally initialized project."""
    from booley.runtime.project_dir import reset_cache

    reset_cache()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(tmp_path))


@pytest.fixture(autouse=True)
def _set_flow_project_root(monkeypatch, _flow_project_root):
    """Supply fake checkout ownership only for data-config fallback."""
    from booley.runtime import shared_infra

    # The fixture's flattened data path does not prove checkout ownership.
    if _flow_project_root is not None:
        monkeypatch.setattr(
            shared_infra, "resolve_project_root", lambda fallback_dir=None: _flow_project_root
        )
    monkeypatch.setattr(shared_infra, "_TOML_CACHE", None)

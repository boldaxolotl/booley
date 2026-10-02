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
    """Bind source-root consumers even when a backend overrides data setup."""
    from booley.runtime import shared_infra

    # The fixture's flattened data path does not prove checkout ownership.
    if _flow_project_root is not None:
        from booley.flows.synth.backends import configure
        from booley.flows.synth.backends.yosys import core

        # These consumers imported the resolver by value; keep real discovery intact.
        for consumer in (shared_infra, configure, core):
            monkeypatch.setattr(
                consumer, "resolve_project_root", lambda fallback_dir=None: _flow_project_root
            )
    monkeypatch.setattr(shared_infra, "_TOML_CACHE", None)

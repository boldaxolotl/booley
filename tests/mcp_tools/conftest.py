"""Test fixtures for endpoints tests.

``_set_booley_dir`` fakes Project root discovery.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Ensure package is importable (fallback when not installed via pip install -e .)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))


@pytest.fixture(autouse=True)
def _set_booley_dir(tmp_path, monkeypatch):
    """Prevent resolve_booley_dir() from failing in endpoints tests."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BOOLEY_DIR", str(tmp_path))
    # Bind implicit source discovery to this fixture's selected checkout.
    from booley.runtime import shared_infra

    monkeypatch.setattr(shared_infra, "resolve_project_root", lambda fallback_dir=None: tmp_path)
    monkeypatch.setattr(shared_infra, "_TOML_CACHE", None)


@pytest.fixture
def fresh_evidence_session(monkeypatch):
    """Start with no bound evidence session: the tool binds one module-global per process."""
    from booley.mcp import coverage_evidence

    # No public reset: the module binds one session per process.
    monkeypatch.setattr(coverage_evidence, "_ACTIVE_SESSION", None)
    monkeypatch.setattr(coverage_evidence, "_TERMINAL_ERROR", None)  # the failed-session latch

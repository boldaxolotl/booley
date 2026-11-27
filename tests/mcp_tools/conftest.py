"""Test fixtures for endpoints tests."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Ensure package is importable (fallback when not installed via pip install -e .)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))


@pytest.fixture(autouse=True)
def _set_booley_dir(pin_project_root):
    """Run each endpoint test in tmp_path as the Booley Directory and Project Root."""


@pytest.fixture
def fresh_evidence_session(monkeypatch):
    """Start with no bound evidence session: the tool binds one module-global per process."""
    from booley.mcp import coverage_evidence

    # No public reset: the module binds one session per process.
    monkeypatch.setattr(coverage_evidence, "_ACTIVE_SESSION", None)
    monkeypatch.setattr(coverage_evidence, "_TERMINAL_ERROR", None)  # the failed-session latch

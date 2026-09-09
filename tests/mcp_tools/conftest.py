"""Test fixtures for endpoints tests."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Ensure package is importable (fallback when not installed via pip install -e .)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))


@pytest.fixture(autouse=True)
def _set_project_dir(tmp_path, monkeypatch):
    """Prevent resolve_project_dir() from failing in endpoints tests."""
    from booley.runtime.project_dir import reset_cache

    reset_cache()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(tmp_path))
    # Legacy callers retain the root selected at import time, before fixtures run.
    # Keep discovery inside this test's Project even when collection imported it
    # from the framework checkout (which contains operator-owned QA .core files).
    from booley.runtime import shared_infra

    monkeypatch.setattr(shared_infra, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(shared_infra, "_TOML_CACHE", None)

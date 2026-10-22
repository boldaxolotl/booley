"""Explicit Booley Directory selection for Harness tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from booley.runtime.booley_dir import reset_cache


def select_booley_dir(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, root: Path
) -> Path:
    """Run one test from ``root`` with ``root/.booley`` as its Booley Directory."""
    reset_cache()
    request.addfinalizer(reset_cache)
    monkeypatch.chdir(root)
    booley_dir = root / ".booley"
    monkeypatch.setenv("BOOLEY_DIR", str(booley_dir))
    return booley_dir

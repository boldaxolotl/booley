"""Fixtures for Booley Flow tests."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def _flow_project_root(tmp_path):
    """Select the synthetic checkout; override with None for real discovery."""
    return tmp_path


@pytest.fixture(autouse=True)
def _set_booley_dir(tmp_path, monkeypatch):
    """Keep Flow tests independent of a locally initialized project."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BOOLEY_DIR", str(tmp_path))


@pytest.fixture(autouse=True)
def _set_flow_project_root(monkeypatch, _flow_project_root):
    """Supply fake checkout ownership only for data-config fallback."""
    from booley.runtime import shared_infra

    # The fixture's flattened data path does not prove checkout ownership.
    if _flow_project_root is not None:
        monkeypatch.setattr(
            shared_infra, "resolve_project_root", lambda fallback_dir=None: _flow_project_root
        )
    # Private seam: the project-config cache is a module global with no public
    # reset, and every Flow test needs it cleared.
    monkeypatch.setattr(shared_infra, "_TOML_CACHE", None)


@pytest.fixture
def session_runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A session runtime with no ambient state, identity or log variables."""
    from booley.runtime import runtime_context

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(runtime_context, "inside_session_runtime", lambda: True)
    for name in ("BOOLEY_STATE_FILE", "BOOLEY_TICKET_FILE", "BOOLEY_LOGS_DIR", "BOOLEY_DIR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("BOOLEY_RUNTIME_DIR", str(tmp_path / "runtime"))
    return tmp_path

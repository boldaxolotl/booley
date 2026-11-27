"""Fixtures for Booley Flow tests.

Stand-in: the Sandbox venue check.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _set_flow_project_root(pin_project_root):
    """Keep Flow tests independent of a locally initialized project and checkout."""


@pytest.fixture
def session_runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A session runtime with no ambient state, identity or log variables."""
    from booley.runtime import runtime_context

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(runtime_context, "inside_session_runtime", lambda: True)
    for name in (
        "BOOLEY_STATE_FILE",
        "BOOLEY_TICKET_FILE",
        "BOOLEY_LOGS_DIR",
        "BOOLEY_DIR",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("BOOLEY_RUNTIME_DIR", str(tmp_path / "runtime"))
    return tmp_path

"""Keep bwave tests off the real temporary-directory session store.

Tests run with the Booley source checkout (or a plain directory) as working
directory, so ``current_session_store()`` falls back to the temporary-directory
store. These helpers point that fallback at a test directory.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from booley.bwave import sessions


def use_store(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    """Make *path* the session file of the temporary-directory store."""
    monkeypatch.setattr(sessions, "_temporary_store", lambda: sessions.SessionStore(path, None))


def isolate_temporary_store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Move the temporary directory to a fresh one, for this process and its children.

    Returns the session file of the resulting temporary-directory store.
    """
    private = tmp_path / "session-tmp"
    private.mkdir()
    for name in ("TMPDIR", "TEMP", "TMP"):
        monkeypatch.setenv(name, str(private))
    monkeypatch.setattr(tempfile, "tempdir", str(private))
    return sessions._temporary_store().path

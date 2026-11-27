"""Keep bwave tests off the real temporary-directory session store.

Tests run with the Booley source checkout (or a plain directory) as working
directory, so ``current_session_store()`` falls back to the temporary-directory
store. These helpers point that fallback at a test directory.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

import pytest

from booley.bwave import sessions


def isolate_temporary_store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Move the temporary directory to a fresh one, for this process and its children.

    Returns the session file of the resulting temporary-directory store.
    """
    private = tmp_path / "session-tmp"
    private.mkdir()
    for name in ("TMPDIR", "TEMP", "TMP"):
        monkeypatch.setenv(name, str(private))
    monkeypatch.setattr(tempfile, "tempdir", str(private))
    return sessions.session_store(None).path


def stored_sessions() -> dict[str, Any]:
    """The persisted session file of the current store, ``{}`` when there is none."""
    path = sessions.current_session_store().path
    if not path.exists():
        return {}
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def write_sessions(entries: dict[str, Any]) -> None:
    """Write *entries* as the current store's session file."""
    path = sessions.current_session_store().path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries, indent=2) + "\n", encoding="utf-8", newline="\n")

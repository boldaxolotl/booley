"""Booley — ticket-driven development harness.

Migrates ticket-execute from Claude skill to Python package using
claude-agent-sdk for creative work, with all deterministic logic in Python.
"""

from pathlib import Path as _Path

_version_file = _Path(__file__).resolve().parents[2] / "VERSION"
__version__ = (
    _version_file.read_text(encoding="utf-8").strip() if _version_file.is_file() else "0.0.0-dev"
)

"""Dependency-light locations for user-owned Booley configuration."""

from __future__ import annotations

import os
from pathlib import Path


def config_dir() -> Path:
    """Return Booley's per-user config directory, honoring XDG_CONFIG_HOME."""
    base = os.environ.get("XDG_CONFIG_HOME", "").strip()
    root = Path(base) if base else Path.home() / ".config"
    return root / "booley"

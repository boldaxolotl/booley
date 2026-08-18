"""Tests for packaged console entry points."""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path


def test_bwave_console_script_is_packaged():
    """Manual B-Wave use needs a Windows launcher from pip."""
    root = Path(__file__).resolve().parent.parent
    pyproject = tomllib.loads((root / "pyproject.toml").read_text())

    scripts = pyproject["project"]["scripts"]
    assert scripts["bwave"] == "booley.bwave.cli:main"

    sys.path.insert(0, str(root / "src"))
    from booley.bwave.cli import main

    assert callable(main)

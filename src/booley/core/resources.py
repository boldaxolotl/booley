"""Dependency-light access to Booley's installed package data."""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path


def package_data_dir() -> Path:
    """Return path to the installed package's data/ directory.

    Falls back to __file__-relative resolution if importlib.resources
    resolves to a shadow booley/ directory (e.g. agent-created artifacts
    in the worktree) that lacks the expected data/ subtree.
    """
    resolved = Path(str(files("booley").joinpath("data")))
    if (resolved / "refs").is_dir():
        return resolved
    # Shadow package — fall back to this file's location
    fallback = Path(__file__).resolve().parent.parent / "data"
    if (fallback / "refs").is_dir():
        return fallback
    return resolved

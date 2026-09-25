"""Shared Git subprocess helper for Harness tests."""

from __future__ import annotations

import subprocess
from pathlib import Path


def git_stdout(repository: Path, *args: str) -> str:
    """Run Git successfully in ``repository`` and return stripped stdout."""
    result = subprocess.run(
        ["git", *args],
        cwd=repository,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return result.stdout.strip()

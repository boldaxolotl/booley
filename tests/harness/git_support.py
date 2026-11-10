"""Shared Git subprocess helper for Harness tests."""

from __future__ import annotations

from pathlib import Path

from tests.subprocess_budget import run_bounded_child


def git_stdout(repository: Path, *args: str) -> str:
    """Run Git successfully in ``repository`` and return stripped stdout."""
    result = run_bounded_child(["git", *args], cwd=repository, timeout=30, check=True)
    return result.stdout.strip()

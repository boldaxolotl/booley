"""Shared Git subprocess helper for Harness tests."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.subprocess_budget import run_bounded_child


def git_stdout(repository: Path, *args: str) -> str:
    """Run Git successfully in ``repository`` and return stripped stdout."""
    result = run_bounded_child(["git", *args], cwd=repository, check=True)
    return result.stdout.strip()


def fake_powershell(directory: Path, monkeypatch: pytest.MonkeyPatch, script: str) -> None:
    """Put a ``powershell`` script running *script* first on PATH; an empty one is not executable."""
    fake = directory / "bin" / "powershell"
    fake.parent.mkdir()
    fake.write_text(f"#!/bin/sh\n{script}\n", encoding="utf-8")
    fake.chmod(0o755 if script else 0o644)
    monkeypatch.setenv("PATH", f"{fake.parent}{os.pathsep}{os.environ['PATH']}")

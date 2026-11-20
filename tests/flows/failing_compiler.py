"""A stand-in ``iverilog`` that prints fixed lines and fails, for build-failure tests."""

from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path

import pytest


def put_failing_iverilog_on_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, printed: Sequence[str]
) -> None:
    """Put an ``iverilog`` first on ``PATH`` that echoes each of *printed*, then exits 1."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    iverilog = fake_bin / "iverilog"
    iverilog.write_bytes(
        ("#!/bin/sh\n" + "".join(f"echo '{x}'\n" for x in printed) + "exit 1\n").encode()
    )
    iverilog.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")

"""Tests for host-side recovery of abandoned runtime executions."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from booley.runtime.execution_recovery import _matches_execution


def test_import_does_not_require_sigkill() -> None:
    """Non-POSIX hosts may not expose SIGKILL through Python's signal module."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import signal, sys; "
                "sys.path.insert(0, 'src'); "
                "hasattr(signal, 'SIGKILL') and delattr(signal, 'SIGKILL'); "
                "import booley.runtime.execution_recovery"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_unreadable_kernel_thread_cannot_match_execution(tmp_path: Path) -> None:
    proc = tmp_path / "2"
    proc.mkdir()
    (proc / "environ").mkdir()
    (proc / "stat").write_text(
        "2 (kworker/0:0) S 0 0 0 0 0 2097152\n",
        encoding="utf-8",
    )

    assert _matches_execution(proc, b"BOOLEY_RUNTIME_EXECUTION_ID=") is False

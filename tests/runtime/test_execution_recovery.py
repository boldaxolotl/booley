"""Tests for host-side recovery of abandoned runtime executions."""

from __future__ import annotations

import subprocess
import sys


def test_import_does_not_require_sigkill() -> None:
    """Non-POSIX hosts may not expose SIGKILL through Python's signal module."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import signal, sys; "
                "sys.path.insert(0, 'src'); "
                "del signal.SIGKILL; "
                "import booley.runtime.execution_recovery"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr

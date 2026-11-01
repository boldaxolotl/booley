"""Contract for the bare-``python`` PATH helper."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from tests.python_command_support import python_path_entry


def test_bare_python_runs_the_running_interpreter(tmp_path: Path) -> None:
    """A bare ``python`` on the helper's PATH entry is this interpreter, venv included."""
    # PATH is the shim entry only, so a broken shim fails here instead of
    # falling through to some other ``python`` on the host.
    result = subprocess.run(
        ["python", "-c", "import sys; print(sys.executable); print(sys.prefix)"],
        env={**os.environ, "PATH": python_path_entry(tmp_path / "shim")},
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )

    assert result.stdout.splitlines() == [sys.executable, sys.prefix]

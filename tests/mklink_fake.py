"""A fake of the Windows ``cmd /c mklink /J`` command boundary for POSIX hosts.

``install`` puts a ``cmd`` launcher first on ``PATH``. It accepts only
``cmd /c mklink /J <link> <absolute target>``, creates a directory symlink in place of the
junction, and exits 1 with ``MKLINK_FAKE_ERROR`` on stderr when the test set that variable.
Any other call exits 1, so a wrong command shows up as a failed link. Windows resolves
``cmd`` to the real ``cmd.exe`` before ``PATH``, so there the real junction is made.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_CMD = """\
import os, sys

args = sys.argv[1:]
error = os.environ.get("MKLINK_FAKE_ERROR")
if len(args) != 5 or args[:3] != ["/c", "mklink", "/J"] or not os.path.isabs(args[4]):
    error = "mklink fake: unexpected command " + " ".join(args)
if error:
    sys.stderr.write(error + "\\n")
    raise SystemExit(1)
os.symlink(args[4], args[3], target_is_directory=True)
print("Junction created for " + args[3] + " <<===>> " + args[4])
"""


def install(directory: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Answer ``cmd /c mklink /J`` from *directory* for the rest of the test."""
    directory.mkdir(parents=True, exist_ok=True)
    launcher = directory / "cmd"
    launcher.write_text(f"#!{sys.executable}\n{_CMD}", encoding="utf-8")
    launcher.chmod(0o755)
    monkeypatch.setenv("PATH", os.pathsep.join((str(directory), os.environ.get("PATH", ""))))


def fail(monkeypatch: pytest.MonkeyPatch, stderr: str) -> None:
    """Make every later ``mklink`` call fail with *stderr*."""
    monkeypatch.setenv("MKLINK_FAKE_ERROR", stderr)

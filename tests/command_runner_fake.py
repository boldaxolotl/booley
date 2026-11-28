"""Stand-ins for the command runner and command lookup that Booley's host probes accept.

Probes such as ``probe_container_runtime`` and ``audit_riscv_toolchain`` take ``run`` (a
``booley.audit.contracts.CommandRunner``) and ``which`` as arguments, so a test hands these
in instead of patching ``subprocess`` or ``shutil.which``.
"""

from __future__ import annotations

import subprocess
from typing import NoReturn


class CannedRunner:
    """Answer every command with one canned result (or exception) and record each argv."""

    def __init__(
        self, returncode: int = 0, stderr: str = "", *, raises: Exception | None = None
    ) -> None:
        self.returncode = returncode
        self.stderr = stderr
        self.raises = raises
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        if self.raises is not None:
            raise self.raises
        return subprocess.CompletedProcess(argv, self.returncode, "", self.stderr)


def refusing_runner(*_args: object, **_kwargs: object) -> NoReturn:
    """A runner for probes that must not start a command."""
    raise AssertionError("the probe ran a command it must not run")


class FixedLookup:
    """Command lookup that finds every name at one path (or nowhere) and records the names."""

    def __init__(self, found: str | None) -> None:
        self.found = found
        self.names: list[str] = []

    def __call__(self, name: str) -> str | None:
        self.names.append(name)
        return self.found

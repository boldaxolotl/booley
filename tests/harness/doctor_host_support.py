"""A healthy Doctor host for a Project that lives in a real Git repository."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from tests.git_command_fake import REAL_RUN
from tests.harness.test_doctor import _patch_environment

_VIVADO_STARTUP = re.compile(r"BOOLEY_VIVADO_STARTUP_[0-9a-f]{32}")
_SANDBOX_MCP_TOOLS = ["synth", "bwave", "coverage_analyst", "lint", "mutation", "reviewer", "sim"]


def serve_healthy_doctor_host(monkeypatch: pytest.MonkeyPatch, root: Path) -> list[list[str]]:
    """Answer Doctor's host and Sandbox commands as healthy, but run Git for real.

    ``_patch_environment`` answers Git for a Project with no repository; this Project
    has one. Vivado's startup probe gets back the marker it asked the Sandbox to print.
    """
    calls = _patch_environment(
        monkeypatch, root, root / ".booley", mcp_tools=[*_SANDBOX_MCP_TOOLS, "fpga"]
    )
    host_run = subprocess.run

    def run(command, **kwargs):
        if command[0] == "git":
            return REAL_RUN(command, **kwargs)
        marker = _VIVADO_STARTUP.search(" ".join(map(str, command)))
        if marker:
            return subprocess.CompletedProcess(command, 0, marker[0] + "\n", "")
        return host_run(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    return calls

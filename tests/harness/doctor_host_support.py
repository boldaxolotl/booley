"""A healthy Doctor host for a Project that lives in a real Git repository."""

from __future__ import annotations

import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from booley.harness import host_diagnostics
from tests.git_command_fake import REAL_RUN
from tests.harness.test_doctor import _patch_environment

_VIVADO_STARTUP = re.compile(r"BOOLEY_VIVADO_STARTUP_[0-9a-f]{32}")
_SANDBOX_MCP_TOOLS = ["synth", "bwave", "coverage_analyst", "lint", "mutation", "reviewer", "sim"]


def serve_healthy_doctor_host(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    """Answer Doctor's host and Sandbox commands as healthy, but run Git for real.

    ``_patch_environment`` answers Git for a Project with no repository; this Project
    has one. Vivado's startup probe gets back the marker it asked the Sandbox to print.
    """
    _patch_environment(
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


def answer_doctor_commands(
    monkeypatch: pytest.MonkeyPatch,
    answer: Callable[[list[str]], subprocess.CompletedProcess[str] | None],
) -> None:
    """Give the commands *answer* replies to its reply; pass every other one on.

    "Every other one" goes to the fake already installed, such as the one
    ``_patch_environment`` sets up. *answer* receives the command as strings.
    """
    other_commands = subprocess.run

    def run(command, **kwargs):
        reply = answer([str(part) for part in command])
        return other_commands(command, **kwargs) if reply is None else reply

    monkeypatch.setattr(subprocess, "run", run)


def hide_container_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run Doctor as on a host where no container CLI is installed.

    Doctor learns the CLI from the host inspection and from a command lookup; both answer
    "none". The host inspection otherwise keeps the fake already installed.
    """
    inspect_host = host_diagnostics.inspect_host
    monkeypatch.setattr(
        host_diagnostics,
        "inspect_host",
        lambda root: replace(inspect_host(root), docker_exe=None),
    )
    monkeypatch.setattr(shutil, "which", lambda _name: None)

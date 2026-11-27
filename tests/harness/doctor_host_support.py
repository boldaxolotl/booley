"""A healthy Doctor host for a Project that lives in a real Git repository.

It fakes Doctor's host and Sandbox commands, host inspection and child-process accounting.
"""

from __future__ import annotations

import itertools
import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.harness import developer_probe, host_diagnostics
from tests.git_command_fake import REAL_RUN
from tests.harness.doctor_environment_support import patch_doctor_environment

_VIVADO_STARTUP = re.compile(r"BOOLEY_VIVADO_STARTUP_[0-9a-f]{32}")
_SANDBOX_MCP_TOOLS = ["synth", "bwave", "coverage_analyst", "lint", "mutation", "reviewer", "sim"]


def serve_healthy_doctor_host(monkeypatch: pytest.MonkeyPatch, root: Path) -> list[list[str]]:
    """Answer Doctor's host and Sandbox commands as healthy, but run Git for real.

    ``patch_doctor_environment`` answers Git for a Project with no repository; this Project
    has one. Vivado's startup probe gets back the marker it asked the Sandbox to print.
    """
    calls = patch_doctor_environment(
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


def answer_doctor_commands(
    monkeypatch: pytest.MonkeyPatch,
    answer: Callable[[list[str], dict[str, str]], subprocess.CompletedProcess[str] | None],
) -> None:
    """Answer Doctor's commands with ``answer(argv, env)``; ``None`` passes one on.

    Layers over the fake already installed (``serve_healthy_doctor_host`` or
    ``patch_doctor_environment``), so a test answers only the commands it is about.
    """
    other_commands = subprocess.run

    def run(command, **kwargs):
        argv = [str(part) for part in command]
        env = dict(kwargs.get("env") or {})
        answered = answer(argv, env)
        return answered if answered is not None else other_commands(command, **kwargs)

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


def raise_child_peak_rss(monkeypatch: pytest.MonkeyPatch) -> None:
    """Child-process accounting reads a rising peak, so Doctor's memory probe measures.

    ``resource`` is POSIX-only, so Windows would skip the probe's measurement.
    """
    peak = itertools.count(1024, 1024)
    monkeypatch.setattr(
        developer_probe,
        "resource",
        SimpleNamespace(
            RUSAGE_CHILDREN=0, getrusage=lambda _who: SimpleNamespace(ru_maxrss=next(peak))
        ),
    )

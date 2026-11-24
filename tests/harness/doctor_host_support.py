"""A healthy Doctor host for a Project that lives in a real Git repository."""

from __future__ import annotations

import itertools
import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.core.models import AgentCallParams, AgentResult
from booley.harness import developer_probe
from booley.runtime import agent
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


def answer_doctor_commands(
    monkeypatch: pytest.MonkeyPatch,
    answer: Callable[[list[str], dict[str, str]], subprocess.CompletedProcess[str] | None],
) -> None:
    """Answer Doctor's commands with ``answer(argv, env)``; ``None`` passes one on.

    Layers over the fake already installed (``serve_healthy_doctor_host`` or
    ``_patch_environment``), so a test answers only the commands it is about.
    """
    other_commands = subprocess.run

    def run(command, **kwargs):
        argv = [str(part) for part in command]
        env = dict(kwargs.get("env") or {})
        answered = answer(argv, env)
        return answered if answered is not None else other_commands(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)


def answer_agent_calls(
    monkeypatch: pytest.MonkeyPatch, answer: Callable[[AgentCallParams], AgentResult]
) -> list[AgentCallParams]:
    """Answer each agent call Doctor's probes make with ``answer(params)``.

    ``answer`` returns an ``AgentResult`` or raises what the backend would raise.
    Child-process accounting reads a rising peak so the memory probe measures.
    Returns every call's params.
    """
    calls: list[AgentCallParams] = []

    async def call_agent(params: AgentCallParams) -> AgentResult:
        calls.append(params)
        return answer(params)

    peak = itertools.count(1024, 1024)
    monkeypatch.setattr(agent, "call_agent", call_agent)
    # ``resource`` is POSIX-only, so Windows would skip the probe's measurement.
    monkeypatch.setattr(
        developer_probe,
        "resource",
        SimpleNamespace(
            RUSAGE_CHILDREN=0, getrusage=lambda _who: SimpleNamespace(ru_maxrss=next(peak))
        ),
    )
    return calls

"""The sim MCP tool refuses contradictory seed requests at dispatch (ADR 0072).

The MCP server hands a tool call to a Flow child process. These tests go
through the real dispatch (argument transport, command construction, watchdog
sizing) and run the real child, replacing only the job manager that would
keep it in the background: the refusal must come from the tool call itself,
not only from constructing a ``SimRequest`` in-process.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from booley.flows.sim.flow import SimulateFlow
from booley.mcp import server
from booley.mcp.flow_adapter import flow_schema

_SRC = Path(server.__file__).resolve().parents[2]


def _sim_tool_definition() -> dict:
    """The sim tool exactly as the server registers it."""
    tools, _errors = server._discover_booley_mcp_tools()
    (definition,) = (tool for tool in tools if tool["name"] == "sim")
    return definition


def _call_sim_tool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arguments: dict):
    commands: list[list[str]] = []

    async def run_child_now(name, cmd, _timeout, _jobs, *, context, caller=None):
        del context, caller
        assert name == "sim" and cmd[0] == "python"
        commands.append(cmd)
        child = subprocess.run(
            [sys.executable, *cmd[1:]],
            cwd=tmp_path,
            env={**os.environ, "PYTHONPATH": str(_SRC)},
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        text = SimpleNamespace(type="text", text=child.stdout + child.stderr)
        return server._dispatch_result([text], is_error=child.returncode != 0)

    monkeypatch.setattr(server, "_dispatch_async_job", run_child_now)
    monkeypatch.setenv("BOOLEY_RUNTIME_DIR", str(tmp_path / "runtime"))
    definition = _sim_tool_definition()
    result = asyncio.run(
        server._dispatch_booley_mcp_tool(
            "sim", {"work_dir": str(tmp_path), **arguments}, definition, {}, MagicMock()
        )
    )
    assert len(commands) == 1
    return result, commands[0]


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (
            {"target": "sim", "seed": 5, "test": ["stress@3"]},
            "--seed cannot be combined with a --test that names a seed",
        ),
        (
            {"target": "sim", "seed": "random", "test": ["stress+soak@x2"]},
            "--seed cannot be combined with a --test that names a seed",
        ),
        (
            {"target": "sim", "seed": 5, "test": ["reset", "stress@x3"]},
            "--seed cannot be combined with a --test that names a seed",
        ),
        ({"target": "sim", "test": ["stress@3@x2"]}, "has more than one '@'"),
        ({"target": "sim", "test": ["stress@x2@3"]}, "has more than one '@'"),
    ],
)
def test_sim_tool_dispatch_refuses_contradictory_seed_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arguments: dict, message: str
):
    result, command = _call_sim_tool(tmp_path, monkeypatch, arguments)

    assert isinstance(result, server.McpDispatchResult)
    assert result.is_error is True
    assert message in result.value[0].text
    # The refused request reached the child as the tool call wrote it.
    assert all(f"--test={name}" in command or name in command for name in arguments["test"])
    # Nothing ran: the request was refused before any report directory existed.
    assert not (tmp_path / "reports").exists()


def test_sim_tool_schema_offers_the_seed_the_dispatch_accepts():
    seed = flow_schema(SimulateFlow())["properties"]["seed"]
    assert seed["oneOf"] == [
        {"type": "integer", "minimum": 1, "maximum": 2**31 - 1},
        {"type": "string", "enum": ["random"]},
    ]

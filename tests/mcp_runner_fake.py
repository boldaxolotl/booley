"""The one fake of the Booley MCP server's endpoint child process, and the application it serves."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from booley.mcp import catalog, server
from booley.mcp.application import McpApplication, McpToolPayload
from booley.mcp.registry import discover_mcp_tools


@dataclass
class RunnerCall:
    cmd: list[str]
    kwargs: dict[str, Any]


def install(monkeypatch: pytest.MonkeyPatch, answer: Any = (0, "ok", "", False)):
    """Replace the server's child-process runner; record each call; answer it.

    *answer* is a result tuple, an exception to raise, or an async callable
    ``(cmd, **kwargs) -> tuple`` (a gated child, a child that writes a report).
    """
    calls: list[RunnerCall] = []

    async def run(cmd: list[str], **kwargs: Any) -> tuple[int, str, str, bool]:
        calls.append(RunnerCall(list(cmd), kwargs))
        if isinstance(answer, BaseException):
            raise answer
        if callable(answer):
            return await answer(cmd, **kwargs)
        return answer

    # Boundary patch: `_run_subprocess` is the one function that starts an endpoint child.
    monkeypatch.setattr(server, "_run_subprocess", run)
    return calls


def application(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    definitions: Sequence[dict[str, Any]] | None = None,
) -> McpApplication:
    """The in-process MCP application over the real catalog (or *definitions*).

    Developer exposure with debug tools on; Job records and reports land under *tmp_path*.
    """
    for name in ("BOOLEY_NESTED_AGENT", "BOOLEY_NESTED_MCP_TOOLS", "BOOLEY_MCP_TOOLS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("BOOLEY_STATE_FILE", raising=False)
    monkeypatch.delenv("BOOLEY_DIR", raising=False)
    monkeypatch.setenv("BOOLEY_MCP_DEVELOPER_AGENT", "1")
    monkeypatch.setenv("BOOLEY_MCP_DEBUG_TOOLS", "1")
    monkeypatch.setenv("BOOLEY_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("BOOLEY_LOGS_DIR", str(tmp_path / "logs"))
    monkeypatch.chdir(tmp_path)
    if definitions is None:
        definitions, errors = catalog.discover_builtin_definitions(
            discover_mcp_tools(), catalog.Exposure.from_mapping(os.environ)
        )
        assert errors == []
    # Private seam: no public constructor builds the application without serving it;
    # `main()` serves stdio or HTTP until the client leaves.
    return server._build_mcp_application(list(definitions), [], server._McpLifetime(None, None))


def call(
    app: McpApplication, name: str, arguments: dict[str, Any], **kwargs: Any
) -> McpToolPayload:
    return asyncio.run(app.call_tool(name, arguments, **kwargs))

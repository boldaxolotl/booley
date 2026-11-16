"""Endpoint commands launch their own module."""

from __future__ import annotations

import collections
from pathlib import Path

import pytest

try:
    from booley.mcp import server as mcp_server
except ImportError:
    pytest.skip("mcp package not installed", allow_module_level=True)

FLOW = {"module": "lint", "is_flow": True}
CUSTOM_FLOW = {
    "module": "probe",
    "is_flow": True,
    "is_custom": True,
    "custom_path": "/project/flows/probe.py",
}
SPECIALIST = {"module": "reviewer", "is_specialist": True}
TOOL = {"module": "probe_tool"}
CUSTOM_TOOL = {"module": "probe", "is_custom": True, "custom_path": "/project/tools/probe.py"}


@pytest.fixture
def server_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Server environment without a Ticket and with a pinned transcript root."""
    monkeypatch.delenv("BOOLEY_TICKET_FILE", raising=False)
    monkeypatch.delenv("BOOLEY_LOGS_DIR", raising=False)
    monkeypatch.setenv("BOOLEY_RUNTIME_DIR", str(tmp_path / "rt"))
    return tmp_path


def _command(name: str, definition: dict[str, object]) -> list[str]:
    return mcp_server._endpoint_command(
        name,
        {"target": "lint_uart"},
        definition,
        collections.defaultdict(int),
    )


class TestEndpointCommand:
    """Command lines equal what the pre-selection ``_endpoint_command`` produced."""

    def test_standalone_commands(self, server_env: Path) -> None:
        transcripts = server_env / "rt" / "transcripts" / "reviewer" / "1"

        assert _command("lint", FLOW) == [
            "python",
            "-m",
            "booley.mcp.lint",
            "--target",
            "lint_uart",
        ]
        assert _command("probe", CUSTOM_FLOW) == [
            "python",
            "/project/flows/probe.py",
            "--target",
            "lint_uart",
        ]
        assert _command("reviewer", SPECIALIST) == [
            "python",
            "-m",
            "booley.mcp.reviewer",
            "--target",
            "lint_uart",
            "--transcript-dir",
            str(transcripts),
        ]
        assert _command("probe_tool", TOOL) == [
            "python",
            "-m",
            "booley.mcp.probe_tool",
            "--target",
            "lint_uart",
        ]
        assert _command("probe", CUSTOM_TOOL) == [
            "python",
            "/project/tools/probe.py",
            "--target",
            "lint_uart",
        ]

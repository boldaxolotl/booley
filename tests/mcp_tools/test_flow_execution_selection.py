"""One selection decides the Flow runner and the MCP tool recorder, exactly as before."""

from __future__ import annotations

import collections
from pathlib import Path

import pytest

from booley.mcp.call_context import resolve_call_context
from booley.mcp.flow_execution_selection import select_flow_execution
from booley.mcp.unbound_recorder import UnboundAcceptanceRecorder

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
        resolve_call_context({}),
    )


class TestSelection:
    @pytest.mark.parametrize("ticket", [False, True])
    def test_mcp_tool_records_through_the_ticket_recorder(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ticket: bool
    ) -> None:
        from tests.mcp_tools.test_base import ConcreteMcpTool

        if ticket:
            monkeypatch.setenv("BOOLEY_TICKET_FILE", str(tmp_path / "ticket.md"))
        else:
            monkeypatch.delenv("BOOLEY_TICKET_FILE", raising=False)

        assert type(ConcreteMcpTool()._acceptance_recorder) is UnboundAcceptanceRecorder


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


class TestGoalSelection:
    """A Goal run binding selects the Goal adapter for every endpoint kind."""

    def test_binding_selects_the_goal_adapter(self, tmp_path: Path) -> None:
        from booley.goals.binding import GoalRunBinding
        from booley.goals.flow_execution import GoalFlowExecution
        from booley.goals.model import WorktreeIdentity

        binding = GoalRunBinding(
            project_dir=tmp_path,
            record_id="evidence-20261006T120000Z",
            record_revision=3,
            worktree=WorktreeIdentity("12345678-1234-4234-9234-123456789abc", "worktrees/wt"),
            worktree_root=tmp_path / "wt",
            goal_branch="goal/evidence-20261006",
            invocation_id="run-1",
            spec_revisions=(("lint_clean_top", 1),),
            protected_paths=(),
            start_digest="sha256:" + "0" * 64,
            start_head_digest="sha256:" + "1" * 64,
            eligible=True,
        )

        selection = select_flow_execution(binding)

        adapter = selection.acceptance_recorder()
        assert isinstance(adapter, GoalFlowExecution)
        assert adapter.binding == binding
        assert selection.flow_runner_module == "booley.mcp.goal_flow_runner"
        assert adapter.acceptance_identity()["goal_keys"] == ["lint_clean_top"]

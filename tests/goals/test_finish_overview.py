"""The goal_finish reply and post-finish status are a short summary, not the audit JSON."""

from __future__ import annotations

import asyncio
import re
from types import SimpleNamespace
from typing import cast
from uuid import uuid4

import pytest
from mcp.types import TextContent

from booley.goals.paths import record_paths
from booley.mcp.application import McpDispatchResult
from booley.mcp.goal_tools import dispatch_goal_tool
from tests.goals.conftest import enter_goals, git
from tests.goals.test_finish import publication_layout
from tests.goals.test_status import publish

pytestmark = pytest.mark.timeout(120)

# Lines of an indented JSON audit section: a bare brace or an indented quoted key.
_JSON_LINE = re.compile(r'^(\{|\[|\s+")', re.MULTILINE)


def goal_tool(name: str, layout: SimpleNamespace, **arguments: object) -> str:
    """The text one Goal tool call returns through the real MCP edge."""
    result = asyncio.run(
        dispatch_goal_tool(
            name, {"work_dir": str(layout.worktree), **arguments}, booley_dir=layout.control
        )
    )
    assert isinstance(result, McpDispatchResult) and result.is_error is False
    # Status carries structured content; Finish returns the bare text list.
    value = result.value[0] if isinstance(result.value, tuple) else result.value
    return cast("list[TextContent]", value)[0].text


def finish_reply(layout: SimpleNamespace) -> str:
    return goal_tool(
        "goal_finish",
        layout,
        record_id=layout.record.id,
        operation_id=str(uuid4()),
        summary="Verified lint for the final committed design.",
    )


def test_published_finish_reply_is_concise_over_several_target_changes(layout):
    complete = publication_layout(layout)
    core = complete.worktree / "top.core"
    core.write_bytes(
        core.read_bytes().replace(b"toplevel: top}", b"toplevel: top, parameters: [WIDTH]}")
        + b"  alt: {filesets: [rtl], toplevel: top}\n"
        + b"parameters:\n  WIDTH: {datatype: int, paramtype: vlogparam, default: 1}\n"
    )
    git(complete.worktree, "add", "top.core")
    git(complete.worktree, "commit", "-qm", "change and add Targets")
    publish(complete)

    reply = finish_reply(complete)

    paths = record_paths(complete.control, complete.record.id)
    history = f".booley/goals/history/{complete.record.id}.md"
    assert f"Goal Mode {complete.record.id} finished." in reply.splitlines()
    assert "Session Summary\nVerified lint for the final committed design." in reply
    assert "- LINT:top (no unwaived findings): met and fresh; clean" in reply
    assert "Diff summary: 1 file changed (0 added, 1 modified, 0 removed)" in reply
    assert "- modified rtl/top.core" in reply
    assert "Target changes: 2" in reply
    assert "::top:0#top: modified (" in reply and "::top:0#alt: added" in reply
    assert "Goal Changes: 0" in reply and "Decisions: 0 (0 applied" in reply
    assert not _JSON_LINE.search(reply) and "Input proof" not in reply
    assert f"Full briefing: {paths.summary_file}" in reply
    assert f"Published: {history} on Goal Branch " in reply
    assert f"Review Package data: {paths.review_package_file}" in reply
    # The published record keeps every audit section, byte for byte (AC1).
    published = git(complete.worktree, "show", f"HEAD:{history}")
    briefing = paths.summary_file.read_text()
    assert published == briefing.strip() and "Input proof" in briefing
    assert git(complete.worktree, "status", "--porcelain") == ""


def test_stealth_finish_reply_without_target_changes_and_post_finish_status(layout):
    layout.record = enter_goals(layout, [{"family": "LINT", "target": "top"}])
    publish(layout)

    reply = finish_reply(layout)

    paths = record_paths(layout.control, layout.record.id)
    assert "Target changes: none" in reply
    assert not _JSON_LINE.search(reply) and "Input proof" not in reply
    assert f"Full briefing: {paths.summary_file}" in reply
    assert "Not published: " in reply and "Stealth" in reply
    assert f"Review Package data: {paths.review_package_file}" in reply
    # The saved briefing keeps every audit section; the reply never replaces it (AC1).
    assert "Input proof" in paths.summary_file.read_text()
    # Status of the finished record shows the same text.
    status = goal_tool("goal_status", layout)
    assert reply in status
    assert "Session Summary:" not in status


def _briefing_row(detail: dict) -> dict:
    """The smallest Review Package Goal row the briefing renders."""
    return {
        "spec": "REVIEW:top",
        "metric": "clean",
        "modified_target": False,
        "observation_sha256": "ab12",
        "spec_revision": 1,
        "original_observations": [],
        "selected_observation": {
            "sequence": 3,
            "producer": "reviewer",
            "invocation_id": "inv-1",
            "recorded_at": "2026-11-06T00:00:00Z",
            "detail": detail,
        },
    }


def test_briefing_shows_specialist_model_fallback_note():
    from booley.goals.finish_presentation import _goal_lines

    fell_back = {
        "model": "gpt-6-luna",
        "agent_cli": "codex",
        "fallback": {
            "first_choice": "claude-opus-5-5",
            "skipped": [
                {"model": "claude-opus-5-5", "reason": "Agent CLI claude is not logged in"}
            ],
        },
    }

    lines = _goal_lines(_briefing_row({"specialist_model": fell_back}), str)
    plain = _goal_lines(
        _briefing_row({"specialist_model": {"model": "claude-opus-5-5", "agent_cli": "claude"}}),
        str,
    )

    assert (
        "  Model: ran on gpt-6-luna instead of claude-opus-5-5 "
        "(claude-opus-5-5: Agent CLI claude is not logged in)." in lines
    )
    # Running the first choice (or no recorded model) adds no note.
    assert not any("Model:" in line for line in plain)

"""Modern MCP lifecycle binding composed with production freshness and waiver ports."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from booley.core.boundary import require_bool_value, require_str_value
from booley.goals.apply import ChangeEnvironment
from booley.goals.entry import EntryEnvironment
from booley.goals.finish import FinishEnvironment, finish_goal
from booley.goals.lifecycle import LifecycleRequest
from booley.mcp.goal_freshness import GOAL_FRESHNESS_RESOLVERS
from booley.mcp.goal_generated_inputs import generated_inputs
from booley.mcp.goal_waivers import GoalWaiverService


def finish_schema(work_dir: Mapping[str, Any]) -> dict[str, Any]:
    """Expected record plus stable operation ID are required even on retries."""
    return {
        "type": "object",
        "properties": {
            "work_dir": dict(work_dir),
            "record_id": {"type": "string", "minLength": 1},
            "operation_id": {"type": "string", "format": "uuid"},
            "summary": {"type": "string"},
            "abandon": {"type": "boolean"},
            "instruction_quote": {"type": "string"},
            "explain_html": {"type": "boolean"},
        },
        "required": ["work_dir", "record_id", "operation_id"],
        "additionalProperties": False,
    }


def completion_environment(entry: EntryEnvironment) -> FinishEnvironment:
    """One composition seam shared by CLI and MCP; the Goal domain imports neither."""
    return FinishEnvironment(
        ChangeEnvironment(entry),
        GOAL_FRESHNESS_RESOLVERS,
        waiver_factory=lambda record: GoalWaiverService(entry.project_dir, record),
        generated_inputs=lambda record, state, root: generated_inputs(
            entry.project_dir, record, state, root
        ),
    )


def complete_goal(
    arguments: Mapping[str, Any], entry: EntryEnvironment, *, session_key: str | None = None
) -> dict[str, Any]:
    """Boundary coercion never reads SDK request envelopes as lifecycle authority."""
    request = LifecycleRequest(
        Path(require_str_value(arguments.get("work_dir"), field="work_dir")),
        require_str_value(arguments.get("record_id"), field="record_id"),
        require_str_value(arguments.get("operation_id"), field="operation_id"),
        require_str_value(arguments.get("summary", ""), field="summary", allow_empty=True),
        require_bool_value(arguments.get("abandon", False), field="abandon"),
        require_str_value(
            arguments.get("instruction_quote", ""), field="instruction_quote", allow_empty=True
        ),
        require_bool_value(arguments.get("explain_html", False), field="explain_html"),
        session_key=session_key,
    )
    return finish_goal(request, completion_environment(entry))

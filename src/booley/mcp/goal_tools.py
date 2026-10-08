"""The Goal Mode MCP tools (ADR 0067): definitions, visibility, and dispatch.

``goal_enter``, ``goal_status``, ``goal_propose_change``, and ``goal_finish``
are synthetic MCP tools served in-process by the MCP server. They exist only
in an Interactive Mode tab, and only while ``BOOLEY_GOAL_MODE_PREVIEW=1`` is
set (D13): with the switch unset they are neither listed nor callable, so a
release exposes exactly one development surface.

``goal_enter`` runs the entry transaction from :mod:`booley.goals.entry` in
one worker thread, which takes and releases every Goal lock itself; no lock
crosses the hand-off. Status reads the record without changing it. Proposals use durable decisions and
recoverable application; finish freezes evidence and publishes pinned history.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from mcp.types import TextContent

from booley.criteria.evidence_ledger import AcceptanceLedgerError
from booley.goals.binding import GoalBindingError
from booley.goals.entry import (
    EntryEnvironment,
    GoalEntryError,
    RecipeFamily,
    enter_goal_mode,
    parse_entry_request,
)
from booley.goals.format import render_status
from booley.goals.lifecycle import LifecycleError
from booley.goals.model import goal_arg_json_schema
from booley.goals.paths import SLUG_MAX_LENGTH, SLUG_PATTERN
from booley.goals.preview import goal_mode_preview_enabled
from booley.goals.proposals import ProposalError
from booley.goals.rules import goal_mode_rules
from booley.goals.session_key import session_key
from booley.goals.state_store import GoalStateError
from booley.goals.status import status_views
from booley.goals.store import GoalStore, GoalStoreError
from booley.mcp.application import McpDispatchResult, McpInputRequired, McpRequestContext
from booley.mcp.goal_changes import proposal_schema, propose_change
from booley.mcp.goal_completion import complete_goal, finish_schema

GOAL_ENTER = "goal_enter"
GOAL_STATUS = "goal_status"
GOAL_PROPOSE_CHANGE = "goal_propose_change"
GOAL_FINISH = "goal_finish"
GOAL_TOOL_NAMES: tuple[str, ...] = (GOAL_ENTER, GOAL_STATUS, GOAL_PROPOSE_CHANGE, GOAL_FINISH)

_WORK_DIR = {
    "type": "string",
    "minLength": 1,
    "description": (
        "Absolute root of the linked worktree this Goal Mode belongs to (create one with "
        "`booley worktree new <name>`). Required on every Goal call."
    ),
}
_ENTER_DESCRIPTION = (
    "Enter Goal Mode in one linked worktree: create the Goal Branch goal/<slug>-<date> at "
    "the current clean HEAD, record the Goals (every Goal is mandatory and starts unmet), "
    "and return the rules for working in Goal Mode. Translate the chosen Goalsets from "
    "the Project directory's goalsets/ into concrete Goals yourself: every per-Target Goal names "
    "its Target. If the Project has a default Goalset, apply it (list 'default' in "
    "goalsets_used) unless the human explicitly skips it, with their reason."
)
_NOT_YET_DESCRIPTION = "Not available yet in this Booley version."


def goal_tools_visible(*, interactive: bool) -> bool:
    """Whether the Goal tools are listed and callable on this server (D13)."""
    return interactive and goal_mode_preview_enabled()


def goal_tool_defs() -> list[dict[str, Any]]:
    """The four Goal tool definitions, in the MCP server's catalog shape."""
    return [
        {"name": GOAL_ENTER, "description": _ENTER_DESCRIPTION, "schema": goal_enter_schema()},
        {
            "name": GOAL_STATUS,
            "description": "Read the current Goal status; rules=true repeats the Goal Mode rules.",
            "schema": {
                "type": "object",
                "properties": {"work_dir": _WORK_DIR, "rules": {"type": "boolean"}},
            },
        },
        {
            "name": GOAL_PROPOSE_CHANGE,
            "description": "Propose add/relax/retarget/waiver; resume saved IDs; approve/reject only with the human's exact instruction and reason.",
            "schema": proposal_schema(_WORK_DIR),
        },
        {
            "name": GOAL_FINISH,
            "description": "Finish every met/fresh Goal with a Session Summary; open done findings remain visible. Pass the exact record_id and caller-stable operation_id on every retry. Abandon only on explicit human instruction (abandon=true, instruction_quote).",
            "schema": finish_schema(_WORK_DIR),
        },
    ]


def goal_enter_schema() -> dict[str, Any]:
    """The ``goal_enter`` input schema; Goal shapes come from :func:`goal_arg_json_schema`."""
    return {
        "type": "object",
        "properties": {
            "work_dir": _WORK_DIR,
            "slug": {
                "type": "string",
                "pattern": f"^{SLUG_PATTERN.pattern}$",
                "maxLength": SLUG_MAX_LENGTH,
                "description": "Short lowercase name for the work, agreed with the human.",
            },
            "goals": {"type": "array", "items": goal_arg_json_schema(), "minItems": 1},
            "goalsets_used": {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
                "description": "Names of the Goalsets the Goals came from, without '.md'.",
            },
            "default_skipped": {
                "type": "boolean",
                "description": "The human explicitly skipped the default Goalset.",
            },
            "skip_reason": {
                "type": "string",
                "minLength": 1,
                "description": "The human's reason for skipping the default Goalset.",
            },
        },
        "required": ["work_dir", "slug", "goals"],
        "additionalProperties": False,
    }


async def dispatch_goal_tool(  # noqa: PLR0911 — four independent public Goal tools have distinct responses
    name: str,
    arguments: Mapping[str, Any],
    *,
    project_dir: Path,
    request_context: McpRequestContext | None = None,
) -> McpDispatchResult | McpInputRequired:
    """Serve one visible Goal tool call; the caller checked visibility."""
    from booley.mcp.call_context import require_goal_work_dir

    store = GoalStore(project_dir)
    try:
        require_goal_work_dir(arguments, store)
        if name == GOAL_FINISH:
            return await _finish(arguments, project_dir, request_context)
        if name == GOAL_PROPOSE_CHANGE:
            env = _entry_environment(project_dir, request_context)
            return await asyncio.to_thread(propose_change, arguments, env, request_context)
        if name == GOAL_STATUS:
            root = Path(str(arguments.get("work_dir") or Path.cwd()))
            views = await asyncio.to_thread(status_views, store, root)
            text = render_status(views) if views else "No active Goal Mode."
            if arguments.get("rules") is True:
                text += "\n\n" + goal_mode_rules()
            return _text(text, is_error=False)
    except (
        LifecycleError,
        GoalBindingError,
        GoalStoreError,
        GoalStateError,
        AcceptanceLedgerError,
        ProposalError,
        OSError,
        ValueError,
    ) as exc:
        return _text(f"ERROR: {exc}", is_error=True)
    if name != GOAL_ENTER:
        return _text(f"{name} is not available yet in this Booley version.", is_error=True)
    try:
        request = parse_entry_request(
            arguments,
            session_key=_entry_key(arguments, store, request_context),
        )
        env = _entry_environment(project_dir, request_context)
        result = await asyncio.to_thread(enter_goal_mode, request, env)
    except (GoalEntryError, GoalStoreError) as exc:
        return _text(f"ERROR: Goal Mode was not entered: {exc}", is_error=True)
    return _text(result.render(), is_error=False)


def _entry_key(
    arguments: Mapping[str, Any], store: GoalStore, context: McpRequestContext | None
) -> str:
    return (
        context.attribution.key
        if context and context.attribution
        else _session_key(arguments, store)
    )


async def _finish(
    arguments: Mapping[str, Any],
    project_dir: Path,
    request_context: McpRequestContext | None = None,
) -> McpDispatchResult:
    env = _entry_environment(project_dir, request_context)
    result = await asyncio.to_thread(
        complete_goal,
        arguments,
        env,
        session_key=(
            request_context.attribution.key
            if request_context and request_context.attribution
            else None
        ),
    )
    message = result.get("message", result.get("reason", ""))
    if result.get("reason") and result["reason"] not in message:
        message += "\nReason: " + result["reason"]
    if result.get("publication_note"):
        message += "\n" + result["publication_note"]
    if result.get("running_jobs"):
        message += "\nRunning Jobs (not cancelled): " + str(result["running_jobs"])
    return _text(message, is_error=result["status"] == "revalidation_required")


def _session_key(arguments: Mapping[str, Any], store: GoalStore) -> str | None:
    """Worktree identity audit key until Phase 6 adds the process registry."""
    work_dir = arguments.get("work_dir")
    return session_key(store, Path(work_dir)) if isinstance(work_dir, str) and work_dir else None


def _recipe_families() -> tuple[RecipeFamily, ...]:
    """The synthesis and FPGA recipe snapshots entry freezes, as Ticket intake does."""
    return (
        RecipeFamily("synthesis_ok_", "Synthesis", _synthesis_snapshot),
        RecipeFamily("fpga_impl_ok_", "FPGA implementation", _fpga_snapshot),
    )


def _synthesis_snapshot(resolved: Any, target: str) -> dict[str, Any]:
    # Imported per call, like Ticket intake: entry is rare and the recipe
    # modules pull in their Flow, which the MCP server does not need at start.
    from booley.flows.synth.recipe import default_recipe_args, synthesis_recipe_snapshot

    return synthesis_recipe_snapshot(resolved, default_recipe_args(), target=target)


def _fpga_snapshot(resolved: Any, target: str) -> dict[str, Any]:
    from booley.flows.fpga.recipe import fpga_recipe_snapshot

    return fpga_recipe_snapshot(resolved, target=target)


def _text(message: str, *, is_error: bool) -> McpDispatchResult:
    return McpDispatchResult(value=[TextContent(type="text", text=message)], is_error=is_error)


def _entry_environment(project_dir: Path, context: McpRequestContext | None) -> EntryEnvironment:
    own = context.attribution.key if context and context.attribution else None
    keys = tuple(key for key in context.other_session_keys if key != own) if context else ()
    return EntryEnvironment(
        project_dir=project_dir,
        recipe_families=_recipe_families(),
        other_sessions=lambda _identity: keys,
    )

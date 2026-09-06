"""Shared data models used across more than one Booley layer.

These types depend only on the standard library and ``booley.core`` boundary
helpers.  They sit below ``booley.dev_support`` and ``booley.ticket_board`` so
those layers do not import upward into ``booley.harness``. ``harness.models``
re-exports them for backward compatibility.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from booley.core.boundary import BoundaryError, is_str_list, require_list


def _remove_target_tuple(value: Any) -> tuple[str, ...] | Any:
    """Normalize a valid external list while preserving invalid input for diagnostics."""
    try:
        items = require_list(value, field="on_success.remove_targets")
    except BoundaryError:
        return value
    if not is_str_list(items):
        return value
    return tuple(items)


@dataclass(frozen=True)
class OnSuccess:
    """Per-ticket completion behavior after all criteria pass.

    destination: when terminal actions fire — "done" skips review, "review" is default.
    merge: whether to merge the feature branch into the base branch.
    cleanup: whether to delete the worktree and branch after a successful merge.
    triage_report: whether to prepare the rich HTML explanation before handoff.
        remove_targets: recorded Targets to delete from the accepted merge candidate.
    """

    destination: str = "review"  # "review" | "done"
    merge: bool = True
    cleanup: bool = True
    triage_report: bool = True
    remove_targets: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, d: dict | None) -> OnSuccess:
        if not d:
            return cls()
        return cls(
            destination=d.get("destination", "review"),
            merge=d.get("merge", True),
            cleanup=d.get("cleanup", True),
            triage_report=d.get("triage_report", True),
            remove_targets=_remove_target_tuple(d.get("remove_targets", [])),
        )

    def validate(self) -> list[str]:
        errors = []
        if self.destination not in ("review", "done"):
            errors.append(
                f"on_success.destination must be 'review' or 'done', got '{self.destination}'"
            )
        if not isinstance(self.triage_report, bool):
            errors.append("on_success.triage_report must be true or false")
        if not isinstance(self.merge, bool):
            errors.append("on_success.merge must be true or false")
        if not isinstance(self.cleanup, bool):
            errors.append("on_success.cleanup must be true or false")
        elif self.cleanup and self.merge is False:
            errors.append("on_success.cleanup requires on_success.merge: true")
        if not (
            isinstance(self.remove_targets, tuple)
            and is_str_list(list(self.remove_targets))
            and all(item.strip() for item in self.remove_targets)
            and len(set(self.remove_targets)) == len(self.remove_targets)
        ):
            errors.append("on_success.remove_targets must contain unique non-empty strings")
        elif self.remove_targets and self.merge is not True:
            errors.append("on_success.remove_targets requires on_success.merge: true")
        return errors


@dataclass
class AgentCallParams:
    """Parameters for invoking an agent, shared across all backends."""

    prompt: str
    model: str
    cwd: str | Path
    allowed_agent_capabilities: list[str] | None = None
    disallowed_agent_capabilities: list[str] | None = None
    system_prompt: str | None = None
    output_format: dict[str, Any] | None = None
    max_turns: int | None = None
    timeout_seconds: int = 1800
    max_budget_usd: float | None = None
    needs_skills: bool = False
    transcript_path: Path | None = None
    label: str | None = None
    reasoning_effort: str | None = None
    session_id: str | None = None
    resume_session: bool = False

    # Names of sub-agent capability calls whose ``input`` dicts should be captured
    # and surfaced on ``AgentResult.captured_agent_capability_calls``. Used when a
    # Specialist's contract is a native agent capability instead of
    # printing structured text — e.g. the reviewer capturing ``ReportFindings``
    # findings that never land in the agent's final text (see reviewer.py).
    # None/[] -> capture nothing (default). Backends that cannot observe capability
    # calls (e.g. Codex) simply leave ``captured_agent_capability_calls`` empty.
    capture_agent_capability_calls: list[str] | None = None

    # MCP tools exposed to a nested agent (Codex-only).
    # None  -> developer-level call (no filtering, full MCP).
    # []    -> nested call with zero MCP servers visible.
    # [...] -> nested call, only the named MCP tools exposed.
    # Recursion safety: specialists must never appear in the allowlist.
    nested_mcp_tools: list[str] | None = None

    # ADR 0028 (container-only Ticket Mode): marks an DEVELOPER-level
    # in-container call and carries its MCP-exposure allowlist.
    # None  -> not an developer launch (nested_mcp_tools semantics apply).
    # [...] -> Codex routes the call through a per-ticket HOME whose
    #          config.toml bakes the current BOOLEY_* env and exposes exactly
    #          these MCP tools (BOOLEY_MCP_TOOLS), WITHOUT the nested-agent
    #          markers — the developer must see the Specialist MCP tools.
    #          Claude ignores this field: its stdio MCP server inherits the
    #          parent env, where the harness already exported BOOLEY_MCP_TOOLS.
    developer_mcp_tools: list[str] | None = None


@dataclass
class AgentResult:
    """Result from an agent call."""

    output: str = ""
    structured: dict[str, Any] | None = None
    input_tokens: int = 0  # inclusive prompt total: uncached + cache reads + cache writes
    output_tokens: int = 0
    cached_tokens: int = 0  # cache reads, billed at ~0.1x input
    cache_create_tokens: int = 0  # cache writes, billed at ~1.25x input
    cost_usd: float = 0.0
    structured_fallback: bool = (
        False  # True when SDK returned no structured_output and we fell back to JSON extraction
    )
    timed_out: bool = False
    max_turns_exhausted: bool = False  # True when the agent hit the max_turns limit
    session_id: str | None = None

    # Captured sub-agent capability-call inputs, keyed by capability name, for the capabilities
    # named in ``AgentCallParams.capture_agent_capability_calls``. Each value is the list
    # of ``input`` dicts from every call the agent made to that capability (in
    # arrival order). A key is present iff the agent invoked that capability at
    # least once — so ``"ReportFindings" in captured_agent_capability_calls`` distinguishes
    # "agent reported zero findings via the capability" from "agent never used it".
    captured_agent_capability_calls: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

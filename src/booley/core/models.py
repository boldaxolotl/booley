"""Shared data models used across more than one Booley layer.

These types depend only on the standard library and ``booley.core`` boundary
helpers.  They sit below ``booley.dev_support`` and ``booley.ticket_board`` so
those layers do not import upward into ``booley.harness``. ``harness.models``
re-exports them for backward compatibility.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from booley.core.boundary import BoundaryError, require_dict, require_list, require_str


class TargetPlanError(ValueError):
    """A Ticket Target Plan is malformed."""


class TargetPlanRole(StrEnum):
    """The accepted-project disposition of one Ticket-authored Target."""

    PERSISTENT = "persistent"
    REPLACEMENT = "replacement"
    EPHEMERAL = "ephemeral"


@dataclass(frozen=True, order=True)
class TargetPlanEntry:
    """One strictly parsed Target transition authored by a Ticket."""

    target: str
    role: TargetPlanRole
    replaces: str = ""

    @classmethod
    def from_mapping(cls, value: Any, *, index: int) -> TargetPlanEntry:
        field_name = f"target_plan[{index}]"
        try:
            mapping = require_dict(value, field=field_name)
            target = require_str(mapping, "target").strip()
            raw_role = require_str(mapping, "role").strip()
        except BoundaryError as exc:
            raise TargetPlanError(str(exc)) from exc
        if not target:
            raise TargetPlanError(f"{field_name}.target must be a non-empty string")
        try:
            role = TargetPlanRole(raw_role)
        except ValueError as exc:
            choices = ", ".join(repr(item.value) for item in TargetPlanRole)
            raise TargetPlanError(f"{field_name}.role must be one of {choices}") from exc
        expected = (
            {"target", "role", "replaces"}
            if role is TargetPlanRole.REPLACEMENT
            else {
                "target",
                "role",
            }
        )
        if set(mapping) != expected:
            missing = sorted(expected - set(mapping))
            unknown = sorted(set(mapping) - expected)
            details = []
            if missing:
                details.append("missing " + ", ".join(missing))
            if unknown:
                details.append("unknown " + ", ".join(unknown))
            raise TargetPlanError(f"{field_name} has invalid keys: {'; '.join(details)}")
        replaces = ""
        if role is TargetPlanRole.REPLACEMENT:
            try:
                replaces = require_str(mapping, "replaces").strip()
            except BoundaryError as exc:
                raise TargetPlanError(str(exc)) from exc
            if not replaces:
                raise TargetPlanError(f"{field_name}.replaces must be a non-empty string")
            if replaces == target:
                raise TargetPlanError(f"{field_name} cannot replace itself")
        return cls(target=target, role=role, replaces=replaces)

    def as_dict(self) -> dict[str, str]:
        result = {"target": self.target, "role": self.role.value}
        if self.role is TargetPlanRole.REPLACEMENT:
            result["replaces"] = self.replaces
        return result


@dataclass(frozen=True)
class TargetPlan:
    """A non-empty, canonical collection of Ticket-authored Target transitions."""

    entries: tuple[TargetPlanEntry, ...]

    @classmethod
    def from_value(cls, value: Any) -> TargetPlan:
        try:
            items = require_list(value, field="target_plan")
        except BoundaryError as exc:
            raise TargetPlanError(str(exc)) from exc
        if not items:
            raise TargetPlanError("target_plan must be a non-empty list when present")
        entries = tuple(
            TargetPlanEntry.from_mapping(item, index=index) for index, item in enumerate(items)
        )
        cls._validate_relationships(entries)
        return cls(tuple(sorted(entries)))

    @staticmethod
    def _validate_relationships(entries: tuple[TargetPlanEntry, ...]) -> None:
        targets = [entry.target for entry in entries]
        if len(set(targets)) != len(targets):
            raise TargetPlanError("target_plan targets must be unique")
        baselines = [entry.replaces for entry in entries if entry.replaces]
        if len(set(baselines)) != len(baselines):
            raise TargetPlanError("target_plan replacement baselines must be unique")
        edges = {entry.target: entry.replaces for entry in entries if entry.replaces}
        for origin in edges:
            seen = {origin}
            cursor = edges.get(origin, "")
            while cursor:
                if cursor in seen:
                    raise TargetPlanError("target_plan replacement graph must be acyclic")
                seen.add(cursor)
                cursor = edges.get(cursor, "")

    def as_list(self) -> list[dict[str, str]]:
        return [entry.as_dict() for entry in self.entries]


@dataclass(frozen=True)
class OnSuccess:
    """Per-ticket completion behavior after all criteria pass.

    destination: when terminal actions fire — "done" skips review, "review" is default.
    merge: whether to merge the feature branch into the base branch.
    cleanup: whether to delete the worktree and branch after a successful merge.
    triage_report: whether to prepare the rich HTML explanation before handoff.
    """

    destination: str = "review"  # "review" | "done"
    merge: bool = True
    cleanup: bool = True
    triage_report: bool = True
    _unsupported_keys: tuple[str, ...] = field(default=(), repr=False, compare=False)

    @classmethod
    def from_dict(cls, d: dict | None) -> OnSuccess:
        if not d:
            return cls()
        allowed = {"destination", "merge", "cleanup", "triage_report"}
        return cls(
            destination=d.get("destination", "review"),
            merge=d.get("merge", True),
            cleanup=d.get("cleanup", True),
            triage_report=d.get("triage_report", True),
            _unsupported_keys=tuple(sorted(set(d) - allowed)),
        )

    def validate(self) -> list[str]:
        errors = []
        if "remove_targets" in self._unsupported_keys:
            errors.append(
                "on_success.remove_targets is unsupported after the Target Plan hard cutoff; "
                "recreate the Ticket"
            )
        unknown = [key for key in self._unsupported_keys if key != "remove_targets"]
        if unknown:
            errors.append("on_success has unknown field(s): " + ", ".join(unknown))
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
        return errors


@dataclass(frozen=True)
class AgentArtifactPaths:
    """Resolved output destinations for one backend attempt."""

    prompt_json: Path | None = None
    prompt_markdown: Path | None = None
    transcript_markdown: Path | None = None


ArtifactPathResolver = Callable[[Path | None], AgentArtifactPaths]
RateLimitNotifier = Callable[[str | None, float, int | None], None]


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
    # Composition resolves paths after the backend selects its attempt transcript.
    artifact_paths: ArtifactPathResolver | None = None
    notify_rate_limit: RateLimitNotifier | None = None
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

    # Text-only calls expose no execution, filesystem, or nested MCP capabilities.
    text_only: bool = False


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

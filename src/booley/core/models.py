"""Shared data models used across more than one Booley layer.

These types depend only on the standard library and ``booley.core`` boundary
helpers.  They sit below agent-facing packages so
those layers do not import upward into ``booley.harness``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class AgentArtifactPaths:
    """Resolved output destinations for one backend attempt."""

    prompt_json: Path | None = None
    prompt_markdown: Path | None = None
    transcript_markdown: Path | None = None


ArtifactPathResolver = Callable[[Path | None], AgentArtifactPaths]


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
    label: str | None = None
    reasoning_effort: str | None = None
    session_id: str | None = None
    resume_session: bool = False
    workspace_guard_fds: tuple[int, ...] = ()
    session_state_dir: Path | None = None

    # Names of sub-agent capability calls whose ``input`` dicts should be captured
    # and surfaced on ``AgentResult.captured_agent_capability_calls``. Used when a
    # Specialist's contract is a native agent capability instead of
    # printing structured text — e.g. the reviewer capturing ``ReportFindings``
    # findings that never land in the agent's final text (see reviewer.py).
    # None/[] -> capture nothing (default). Backends that cannot observe capability
    # calls (e.g. Codex) simply leave ``captured_agent_capability_calls`` empty.
    capture_agent_capability_calls: list[str] | None = None

    # MCP tools exposed to a nested agent.
    # None  -> developer-level call (no filtering, full MCP).
    # []    -> nested call with zero MCP servers visible.
    # [...] -> nested call, only the named MCP tools exposed.
    # Recursion safety: specialists must never appear in the allowlist.
    nested_mcp_tools: list[str] | None = None

    # Extra environment supplied only to the nested Booley MCP server. This is
    # trusted composition data, not model input; adapters must not merge it into
    # the agent process environment itself.
    nested_mcp_env: dict[str, str] | None = None

    # Text-only calls expose no built-in execution or filesystem capabilities.
    # A non-empty nested_mcp_tools list may expose an explicitly scoped read-only tool.
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

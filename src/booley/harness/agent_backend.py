"""Agent backend protocol and implementations.

Thin facade — re-exports from focused submodules:
  - _retry: transient error detection, backoff computation
  - _cost: model pricing, cost estimation, usage formatting
  - _claude_backend: ClaudeSDKBackend (Agent SDK + CLI shim)
  - _codex_backend: CodexBackend (Codex CLI subprocess)
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from .models import AgentCallParams, AgentResult

# --- Protocol (the interface) ---


@runtime_checkable
class AgentBackend(Protocol):
    """Minimal interface every agent backend must satisfy."""

    @property
    def name(self) -> str:
        """Short human-readable label (used in logs)."""
        ...

    async def call(
        self,
        params: AgentCallParams,
        **kwargs: Any,
    ) -> AgentResult:
        """Run an agent with the given params and return the result."""
        ...

    def health_check(self) -> str | None:
        """Quick availability probe.

        Returns None if healthy, or a human-readable warning string.
        Must be fast (no network calls).
        """
        ...


# --- Re-exports (preserve all existing import paths) ---

from ._claude_backend import (  # noqa: F401 — re-exported as public API of harness.agent_backend
    ClaudeSDKBackend,
    _handle_rate_limit_event,
    _notify_rate_limit,
    _write_transcript_turn,
)
from ._codex_backend import (  # noqa: F401 — re-exported as public API of harness.agent_backend
    CodexBackend,
    _codex_build_prompt,
    _codex_ensure_additional_properties,
    _codex_extract_structured,
    _codex_parse_events,
    _codex_sandbox_mode,
    _codex_write_transcript,
)
from ._cost import estimate_cost as _estimate_cost  # noqa: F401 — re-exported for callers
from ._cost import format_usage_log as _format_usage_log  # noqa: F401 — re-exported for callers

# Helpers that other modules import by name
from ._retry import _is_transient_error  # noqa: F401 — re-exported for callers
from ._retry import (
    transcript_path_for_attempt as _transcript_path_for_attempt,  # noqa: F401 — re-exported for callers
)

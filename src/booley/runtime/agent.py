"""Wrapper around agent backends with token tracking and dispatch.

Public API:
  - call_agent()           — dispatch to the configured backend
  - extract_json()         — JSON extraction from agent prose (shared utility)

The actual SDK interaction lives in the backend modules (agent_backend.py).
"""

from __future__ import annotations

import json
import logging
import re
import sys
from typing import Any

from booley.core.models import AgentCallParams, AgentResult

from .agent_config import get_backend_config
from .agent_errors import TransientAPIError  # noqa: F401 (re-export for importers)

logger = logging.getLogger(__name__)


def announce_agent_dispatch(params: AgentCallParams) -> None:
    """Disclose an actual Booley-owned agent call before backend dispatch."""
    role = params.label or "specialist"
    purpose = {
        "developer": "execute the Ticket",
        "triage-report": "prepare a review package",
        "blocked-triage-report": "diagnose a blocked Ticket",
        "doctor-developer-probe": (
            "estimate the provider CLI's memory and check Specialist provider authorization for Doctor"
        ),
    }.get(role, "perform the requested Specialist analysis")
    actor = "provider probe" if role == "doctor-developer-probe" else "Specialist"
    if role in {"developer", "triage-report", "blocked-triage-report"}:
        actor = "agent"  # Retained Ticket callers keep their historical terminology.
    print(f"Booley is starting the {role} {actor} to {purpose}.", file=sys.stderr)


async def call_agent(
    params: AgentCallParams,
    *,
    on_event: Any = None,
) -> AgentResult:
    """Call an agent via the configured backend.

    Routes through BackendConfig: uses the active backend with the
    caller-provided model.

    The backend runs the agent CLI/SDK as a plain subprocess — Booley is
    container-only (ADR 0028), so specialists already execute inside the
    Sandbox and the old per-call DockerSandboxBackend wrap is gone.
    """
    cfg = get_backend_config()

    announce_agent_dispatch(params)
    return await cfg.active_backend.call(params, on_event=on_event)


def extract_json(text: str) -> dict[str, Any] | None:
    """Extract first JSON object from agent output text.

    Tries markdown code fences first, then bare JSON objects.
    """
    # Fenced JSON block: ```json ... ```
    fence_match = re.search(r"```(?:json)?\s*\n([\s\S]*?)\n```", text)
    if fence_match:
        fenced = fence_match.group(1).strip()
        if fenced.startswith("{"):
            try:
                obj, _ = json.JSONDecoder().raw_decode(fenced)
                if isinstance(obj, dict):
                    return obj
            except json.JSONDecodeError:
                pass

    # Bare JSON object
    decoder = json.JSONDecoder()
    search_from = 0
    while search_from < len(text):
        start = text.find("{", search_from)
        if start == -1:
            break
        try:
            obj, _end_idx = decoder.raw_decode(text, start)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
        search_from = start + 1

    logger.warning("Could not extract JSON from agent output")
    return None


# Backward-compatible alias
_extract_json = extract_json


# Re-export for backward compatibility
from .agent_backend import (
    _is_transient_error,  # noqa: F401 — re-exported for backward compatibility
    _transcript_path_for_attempt,  # noqa: F401 — re-exported for backward compatibility
    _write_transcript_turn,  # noqa: F401 — re-exported for backward compatibility
)

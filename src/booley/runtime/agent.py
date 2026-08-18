"""Wrapper around agent backends with token tracking and dispatch.

Public API:
  - call_agent()           — dispatch by model string (backward compat) or step_name
  - extract_json()         — JSON extraction from agent prose (shared utility)

The actual SDK interaction lives in the backend modules (agent_backend.py).
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from booley.config.settings import (
    STEP_TIERS,
    get_backend_config,
)
from booley.core.models import AgentCallParams, AgentResult

from .agent_errors import TransientAPIError  # noqa: F401 (re-export for importers)

logger = logging.getLogger(__name__)


async def call_agent(
    params: AgentCallParams,
    *,
    step_name: str | None = None,
    on_event: Any = None,
) -> AgentResult:
    """Call an agent via the configured backend.

    Routes through BackendConfig: if step_name is provided, resolves
    tier -> backend + model. Otherwise uses the active backend with the
    provided model string (backward compatibility).

    The backend runs the agent CLI/SDK as a plain subprocess — Booley is
    container-only (ADR 0028), so specialists already execute inside the
    Session Runtime and the old per-call DockerSandboxBackend wrap is gone.
    """
    cfg = get_backend_config()

    if step_name is not None and step_name in STEP_TIERS:
        tier = STEP_TIERS[step_name]
        backend = cfg.backend_for_tier(tier)
        params.model = cfg.model_for_tier(tier)
        if params.reasoning_effort is None:
            params.reasoning_effort = cfg.effort_for_tier(tier)
    else:
        backend = cfg.active_backend

    return await backend.call(params, on_event=on_event)


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

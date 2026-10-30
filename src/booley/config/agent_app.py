"""The agent app chosen by ``BOOLEY_AGENT_APP=auto``, published once per Sandbox start.

The in-Sandbox registrar (``booley.harness.incontainer_register``) inspects the
visitor's credentials at every start and publishes its choice as a record in the
Sandbox home directory. Every other consumer reads that record through
:func:`auto_choice`; none of them looks at credentials again, so Specialists,
the MCP server, ``booley chat`` and Doctor cannot disagree with the
registration. The config layer cannot look up credentials itself (it may not
import ``booley.runtime``), which is why the choice is a published record.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from booley.config import agent

AGENT_APP_ENV = "BOOLEY_AGENT_APP"
AUTO_APP = "auto"
_RECORD_SCHEMA = 1
_REGISTRAR_HOOK = "python -m booley.harness.incontainer_register"


class AgentAppError(agent.BackendConfigError):
    """``BOOLEY_AGENT_APP=auto`` cannot be honored (outside a Sandbox, or unresolved)."""


@dataclass(frozen=True)
class AgentAppChoice:
    """One start's resolution: the app, the ``BOOLEY_PRIMARY_AUTH`` policy, and why."""

    app: str
    policy: str | None
    reason: str


def record_path() -> Path:
    """The record's location: the Sandbox home, outside the private credential store."""
    return Path.home() / ".booley-agent-app.json"


def encode(choice: AgentAppChoice) -> bytes:
    """Serialize *choice* for :func:`record_path`."""
    document = {
        "schema": _RECORD_SCHEMA,
        "app": choice.app,
        "policy": choice.policy,
        "reason": choice.reason,
    }
    return (json.dumps(document, sort_keys=True) + "\n").encode()


def auto_requested() -> bool:
    """Whether ``BOOLEY_AGENT_APP`` is ``auto``; refuses ``auto`` outside a Sandbox."""
    if (os.environ.get(AGENT_APP_ENV) or "").strip() != AUTO_APP:
        return False
    if not agent._inside_container():
        raise AgentAppError(
            f"{AGENT_APP_ENV}=auto is honored only inside a Sandbox (for example a "
            "Codespace); set it to claude, codex or none"
        )
    return True


def auto_choice() -> AgentAppChoice | None:
    """The start's choice under ``auto``, or ``None`` when ``auto`` is not requested.

    Raises :class:`AgentAppError` outside a Sandbox, and when the registrar has
    not published a record this start; the choice is never re-derived here.
    """
    if not auto_requested():
        return None
    unresolved = (
        f"{AGENT_APP_ENV}=auto has no resolved choice at {record_path()}: run "
        f"`{_REGISTRAR_HOOK}` (the spec's postStartCommand does) and check its output"
    )
    try:
        document = json.loads(record_path().read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AgentAppError(unresolved) from exc
    if not isinstance(document, dict) or document.get("schema") != _RECORD_SCHEMA:
        raise AgentAppError(unresolved)
    return AgentAppChoice(document["app"], document["policy"], document["reason"])

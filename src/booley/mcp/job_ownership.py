"""Who started a Job: the owner an argument-free ``booley_poll`` waits on.

An owner is the submitting MCP server plus, when the client names one, the agent
tab (Codex thread or Claude Code process) that made the call: Interactive Mode
serves every tab of a Sandbox from one HTTP server. Ownership only selects which
result an argument-free poll hands back; any Job stays reachable by its ``run_id``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from booley.mcp.application import McpRequestContext
from booley.mcp.session_registry import Attribution
from booley.measurement.mcp_job import MEASURE_ENDPOINT
from booley.runtime.display_identity import DisplayScope
from booley.runtime.job_records import JobRecord
from booley.runtime.pid import capture_process_identity
from booley.runtime.session_activity import process_session_key

# Endpoints that ran as background Jobs (submit, then poll) before Jobs had an
# owner. Only these may fall back to the worktree ``session_key`` for such records;
# this is that history, not the routing rule (the catalog's ``memory_admitted``).
_PRE_OWNER_BACKGROUND_ENDPOINTS = frozenset(
    {"sim", "fpga", "synth", "reviewer", "mutation", "coverage_analyst", MEASURE_ENDPOINT}
)


@dataclass(frozen=True, slots=True)
class Caller:
    """One MCP call's identity: the Job owner it matches and its advisory session key."""

    owner: str
    session_key: str | None


def server_identity() -> str:
    """Identity of a stdio server: display scope plus its client process.

    A respawned server keeps its parent and so its identity. A server without a
    visible parent (``docker exec``) is identified by itself, shared with no one.
    """
    parent = os.getppid()
    identity = capture_process_identity(parent) if parent > 1 else None
    if identity is not None:
        holder = process_session_key(identity)
    else:
        own = capture_process_identity(os.getpid())
        holder = "self:" + (process_session_key(own) if own else f"pid:{os.getpid()}")
    return f"mcp-stdio:{DisplayScope.current().value}:{holder}"


def http_server_identity(hostname: str, port: int) -> str:
    """Identity of the Interactive Mode HTTP server; stable across Sandbox restarts."""
    return f"mcp-http:{DisplayScope.current().value}:{hostname}:{port}"


def job_owner(server: str, attribution: Attribution | None) -> str:
    """Server identity, narrowed to one agent tab when the client names a thread or process."""
    if attribution is not None and attribution.kind in ("thread", "process"):
        return f"{server}|{attribution.key}"
    return server


def caller_for(server: str, request_context: McpRequestContext | None) -> Caller:
    """The caller of one MCP request."""
    if request_context is None:
        return Caller(server, None)
    return Caller(job_owner(server, request_context.attribution), request_context.session_key())


def owns(rec: JobRecord, caller: Caller) -> bool:
    """Whether *caller* started *rec*.

    A record written before owners existed belongs to the caller whose
    ``session_key`` stamped it, but only for background-Job endpoints: inline
    and baseline records carry the same key without being the caller's Jobs.
    """
    if rec.owner is not None:
        return rec.owner == caller.owner
    return (
        rec.endpoint in _PRE_OWNER_BACKGROUND_ENDPOINTS
        and caller.session_key is not None
        and caller.session_key == rec.session_key
    )

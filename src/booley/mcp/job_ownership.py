"""Who started a Job: the owner an argument-free ``booley_poll`` waits on.

An owner is the submitting MCP server plus, when the caller has one, the agent
thread (Codex thread, or Claude's T3 Code thread) that made the call: the Sandbox
serves every tab of a Sandbox from one HTTP server. Ownership only selects which
result an argument-free poll hands back; any Job stays reachable by its ``run_id``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from booley.mcp.agent_thread import AgentThread
from booley.mcp.application import McpRequestContext
from booley.runtime.display_identity import DisplayScope
from booley.runtime.job_records import JobRecord
from booley.runtime.pid import ProcessIdentity, capture_process_identity


@dataclass(frozen=True, slots=True)
class Caller:
    """One MCP call's identity: the Job owner it matches and the agent behind it."""

    owner: str
    agent: AgentThread | None = None


def _process_holder(identity: ProcessIdentity) -> str:
    """One process as a stdio owner names it; records stamped earlier keep matching."""
    return f"pid:{identity.identity_scope}:{identity.pid}:{identity.start_token}"


def server_identity() -> str:
    """Identity of a stdio server: display scope plus its client process.

    A respawned server keeps its parent and so its identity. A server without a
    visible parent (``docker exec``) is identified by itself, shared with no one.
    """
    parent = os.getppid()
    identity = capture_process_identity(parent) if parent > 1 else None
    if identity is not None:
        holder = _process_holder(identity)
    else:
        own = capture_process_identity(os.getpid())
        holder = "self:" + (_process_holder(own) if own else f"pid:{os.getpid()}")
    return f"mcp-stdio:{DisplayScope.current().value}:{holder}"


def http_server_identity(hostname: str, port: int) -> str:
    """Identity of the Sandbox HTTP server; stable across Sandbox restarts."""
    return f"mcp-http:{DisplayScope.current().value}:{hostname}:{port}"


def job_owner(server: str, agent: AgentThread | None = None) -> str:
    """Server identity, narrowed to the agent's thread when it has one."""
    if agent is not None and agent.thread is not None:
        return f"{server}|{agent.client}:{agent.thread}"
    return server


def caller_for(server: str, request_context: McpRequestContext | None) -> Caller:
    """The caller of one MCP request."""
    agent = None if request_context is None else request_context.agent
    return Caller(job_owner(server, agent), agent)


def owns(rec: JobRecord, caller: Caller) -> bool:
    """Whether *caller* started *rec*. A record without an owner belongs to no caller."""
    return rec.owner is not None and rec.owner == caller.owner

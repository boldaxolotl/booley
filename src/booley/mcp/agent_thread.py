"""The agent client and thread a call comes from, recorded on each Job it starts."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from booley.mcp.peer_checkout import codex_client, process_client
from booley.runtime.pid import ProcessIdentity, capture_process_identity

logger = logging.getLogger(__name__)

T3_THREAD_ENV = "T3CODE_THREAD_ID"
MAX_THREAD_ID = 1024


def valid_thread_id(value: object) -> bool:
    """Bound client metadata and reject controls or unencodable Unicode."""
    return isinstance(value, str) and 0 < len(value) <= MAX_THREAD_ID and value.isprintable()


@dataclass(frozen=True, slots=True)
class AgentThread:
    """``client`` is ``claude`` or ``codex``; ``thread`` is its thread id, if it has one."""

    client: str
    thread: str | None


def reads_peer(client_name: str, metadata: Mapping[str, Any]) -> bool:
    """Whether naming this caller's thread needs its loopback peer: Claude's, not Codex's."""
    return not codex_client(client_name, metadata) and "claude" in client_name.lower()


async def resolve_agent_thread(
    client_name: str,
    metadata: Mapping[str, Any],
    peer: ProcessIdentity | None,
    *,
    proc_root: Path = Path("/proc"),
) -> AgentThread | None:
    """The caller's client and thread, or ``None`` for a client that is neither Claude nor Codex.

    Codex names its thread in ``_meta``. A Claude caller's T3 Code thread is in the
    environment of its own process, the call's loopback *peer*, read off the event loop.
    """
    if codex_client(client_name, metadata):
        thread = metadata.get("threadId")
        return AgentThread("codex", str(thread) if valid_thread_id(thread) else None)
    if not reads_peer(client_name, metadata):
        return None
    thread = (
        await asyncio.to_thread(_t3_thread, peer, proc_root)
        if peer is not None and process_client(client_name, peer)
        else None
    )
    return AgentThread("claude", thread)


def _t3_thread(process: ProcessIdentity, proc_root: Path) -> str | None:
    """T3 Code's thread id from *process*'s environment; ``None`` when unreadable or recycled."""
    try:
        environment = (proc_root / str(process.pid) / "environ").read_bytes().split(b"\0")
    except OSError as exc:
        logger.debug("cannot read the environment of process %s: %s", process.pid, exc)
        return None
    if capture_process_identity(process.pid, proc_root=proc_root) != process:
        return None  # The PID now belongs to another process.
    prefix = T3_THREAD_ENV.encode() + b"="
    value = next((item[len(prefix) :] for item in environment if item.startswith(prefix)), b"")
    thread = value.decode("utf-8", errors="replace")
    return thread if valid_thread_id(thread) else None

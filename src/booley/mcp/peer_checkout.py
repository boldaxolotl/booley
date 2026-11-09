"""The one place that decides which checkout a call without ``work_dir`` targets.

A call that omits ``work_dir`` runs in the checkout its calling agent works in,
instead of the MCP server's cwd, when the agent's *session process* has the root
of a linked worktree of the server's repository as its cwd. Every other case
(main checkout, which is the Sandbox server's cwd; unrelated directory;
subdirectory; another repository; unreadable or deleted cwd) keeps the server cwd.

The session process is the loopback peer of a non-Codex client (Claude Code's
own process) or the marked ``booley chat`` Codex launch owner. Ordinary Codex
(shared daemon, thread identity) and hand-started Codex never route: a daemon's
cwd belongs to no particular tab.

The result only seeds ``McpRequestContext.checkout``; the application then
treats it exactly like an explicit ``work_dir`` argument.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from starlette.requests import Request

from booley.mcp.session_peer import observed_peer
from booley.mcp.session_registry import codex_client, git_checkout_metadata, process_client
from booley.runtime.booley_dir import resolve_common_booley_dir
from booley.runtime.chat_sessions import codex_chat_owner
from booley.runtime.pid import ProcessIdentity
from booley.runtime.session_activity import valid_thread_id


@dataclass(frozen=True)
class CallerCheckout:
    """One call's looked-up peer and the checkout it routes to (``None`` = server cwd)."""

    peer: ProcessIdentity | None
    checkout: Path | None


def session_process(
    peer: ProcessIdentity,
    *,
    client_name: str,
    metadata: Mapping[str, Any],
    proc_root: Path,
) -> ProcessIdentity | None:
    """The process whose cwd names the caller's checkout, mirroring session attribution."""
    if codex_client(client_name, metadata):
        if not valid_thread_id(metadata.get("threadId")):
            return None
        # The handle map lives in Common Booley State; only Codex needs it.
        return codex_chat_owner(resolve_common_booley_dir(), peer, proc_root=proc_root)
    return peer if process_client(client_name, peer) else None


def process_checkout(
    process: ProcessIdentity, *, server_cwd: Path, proc_root: Path
) -> Path | None:
    """The process's cwd when it is a routable linked-worktree root, else ``None``."""
    try:
        cwd = (proc_root / str(process.pid) / "cwd").readlink()
    except OSError:
        return None  # Exited or unreadable process entry.
    server, caller = git_checkout_metadata(server_cwd), git_checkout_metadata(cwd)
    if server is None or caller is None:
        return None  # Not Git checkouts: nothing to match against.
    _, _, server_common = server
    caller_root, caller_git, caller_common = caller
    # A linked worktree (git dir != common dir) of the server's repository, named by
    # its own root rather than a subdirectory, and not the directory the server is in.
    routable = (
        caller_root == cwd
        and caller_git != caller_common
        and caller_common == server_common
        and cwd != server_cwd
    )
    return cwd if routable else None


def caller_checkout(
    request: Request | None,
    *,
    client_name: str,
    metadata: Mapping[str, Any],
    proc_root: Path = Path("/proc"),
) -> CallerCheckout:
    """Look up the call's peer and the checkout it routes to; any failure routes nowhere."""
    peer = observed_peer(request, proc_root=proc_root)
    if peer is None:
        return CallerCheckout(None, None)
    try:
        process = session_process(
            peer,
            client_name=client_name,
            metadata=metadata,
            proc_root=proc_root,
        )
        if process is None:
            return CallerCheckout(peer, None)
        checkout = process_checkout(process, server_cwd=Path.cwd().resolve(), proc_root=proc_root)
    except (OSError, ValueError, RuntimeError, UnicodeError):
        return CallerCheckout(peer, None)
    return CallerCheckout(peer, checkout)

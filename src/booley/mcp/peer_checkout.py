"""The one place that decides which checkout a call without ``work_dir`` targets.

A call that omits ``work_dir`` runs in the checkout its calling agent works in,
instead of the MCP server's cwd, when the agent's *session process* has the root
of a linked worktree of the server's repository as its cwd. Every other case
(main checkout, which is the Sandbox server's cwd; unrelated directory;
subdirectory; another repository; unreadable or deleted cwd) keeps the server cwd.

A non-Codex client's session process is its loopback peer (Claude Code's own
process). A Codex caller's checkout is the workspace root in the call's ``_meta``
``x-codex-turn-metadata.workspaces``, because one shared ``codex app-server``
serves every thread and its process cwd names no thread. A Codex call whose
metadata names no workspace is refused with a retry message.

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
from booley.runtime.pid import ProcessIdentity

NO_WORKSPACE_REFUSAL = (
    "ERROR: Codex named no workspace for this call, so Booley cannot tell which checkout "
    "it is for. Retry the call, or pass `work_dir`."
)


@dataclass(frozen=True)
class CallerCheckout:
    """One call's looked-up peer, its checkout (``None`` = server cwd), and any refusal."""

    peer: ProcessIdentity | None
    checkout: Path | None
    refusal: str | None = None


def process_checkout(
    process: ProcessIdentity, *, server_cwd: Path, proc_root: Path
) -> Path | None:
    """The process's cwd when it is a routable linked-worktree root, else ``None``."""
    try:
        link_target = (proc_root / str(process.pid) / "cwd").readlink()
        # ``/proc`` targets are kernel-canonical, so no ``resolve()``. Only the Windows
        # ``\\?\`` spelling (fake-proc tests; ``resolve()`` keeps it) needs stripping so
        # it equals the ordinary spelling. The UNC form is deliberately not handled.
        cwd = Path(str(link_target).removeprefix("\\\\?\\"))
    except OSError:
        return None  # Exited or unreadable process entry.
    return routable_checkout(cwd, server_cwd=server_cwd)


def routable_checkout(cwd: Path, *, server_cwd: Path) -> Path | None:
    """*cwd* when it is the root of a linked worktree the server can route to, else ``None``."""
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


def codex_workspaces(metadata: Mapping[str, Any]) -> list[Path]:
    """The workspace roots a Codex call's turn metadata names (none when absent or odd)."""
    turn = metadata.get("x-codex-turn-metadata")
    workspaces = turn.get("workspaces") if isinstance(turn, Mapping) else None
    return [Path(root) for root in workspaces] if isinstance(workspaces, Mapping) else []


def caller_checkout(
    request: Request | None,
    *,
    client_name: str,
    metadata: Mapping[str, Any],
    proc_root: Path = Path("/proc"),
) -> CallerCheckout:
    """Look up the call's peer and the checkout it routes to; any failure routes nowhere."""
    peer = observed_peer(request, proc_root=proc_root)
    try:
        server_cwd = Path.cwd().resolve()
        if codex_client(client_name, metadata):
            roots = codex_workspaces(metadata)
            if not roots:
                return CallerCheckout(peer, None, NO_WORKSPACE_REFUSAL)
            routed = (routable_checkout(root, server_cwd=server_cwd) for root in roots)
            return CallerCheckout(peer, next((path for path in routed if path), None))
        if peer is not None and process_client(client_name, peer):
            return CallerCheckout(
                peer, process_checkout(peer, server_cwd=server_cwd, proc_root=proc_root)
            )
    except (OSError, ValueError, RuntimeError, UnicodeError):
        pass
    return CallerCheckout(peer, None)

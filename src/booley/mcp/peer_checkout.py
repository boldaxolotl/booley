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

import os
from collections.abc import Mapping
from dataclasses import dataclass
from itertools import islice
from pathlib import Path
from typing import Any

from starlette.requests import Request

from booley.core.boundary import as_dict
from booley.mcp.session_peer import observed_peer
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


def codex_client(client_name: str, metadata: Mapping[str, Any]) -> bool:
    """Whether a call comes from Codex, which is identified by thread rather than process."""
    return "codex" in client_name.lower() or metadata.get("threadId") is not None


def process_client(client_name: str, peer: ProcessIdentity) -> bool:
    """Whether a non-Codex client's loopback *peer* is its own session process."""
    return (
        bool(client_name)
        and len(client_name) <= 128
        and client_name.isprintable()
        and peer.pid != os.getpid()
    )


def _git_fact(path: Path) -> str:
    with path.open(encoding="utf-8", newline="") as handle:
        value = handle.read(16385)
    if len(value) > 16384:
        raise ValueError("Git metadata exceeds observation bound")
    return value.strip()


def git_checkout_metadata(root: Path) -> tuple[Path, Path, Path] | None:
    """Nearest enclosing checkout of *root* as ``(checkout root, git dir, common dir)``.

    ``None`` when no ``.git`` is found. A main checkout has ``git == common``; a linked
    worktree's ``git`` is a subdirectory of its ``common``.
    """
    # Observational filesystem facts avoid invoking Git on the MCP response path.
    for candidate in islice((root, *root.parents), 128):
        marker = candidate / ".git"
        if marker.is_dir():
            return candidate, marker.resolve(), marker.resolve()
        if marker.is_file():
            raw = _git_fact(marker)
            if not raw.startswith("gitdir: "):
                raise ValueError("invalid Git worktree marker")
            git = (candidate / raw.removeprefix("gitdir: ")).resolve()
            common_file = git / "commondir"
            common = (git / _git_fact(common_file)).resolve() if common_file.exists() else git
            return candidate, git, common
    return None


def git_branch(git_dir: Path) -> str:
    """The branch *git_dir*'s ``HEAD`` names; empty when detached or unreadable."""
    try:
        head = _git_fact(git_dir / "HEAD")
    except (OSError, ValueError, UnicodeError):
        return ""
    prefix = "ref: refs/heads/"
    return head.removeprefix(prefix) if head.startswith(prefix) else ""


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
    turn = as_dict(metadata.get("x-codex-turn-metadata"), default={}) or {}
    return [Path(root) for root in as_dict(turn.get("workspaces"), default={}) or {}]


def first_routable_workspace(roots: list[Path], *, server_cwd: Path) -> Path | None:
    """The first of *roots* that routes; one whose Git marker cannot be read is skipped."""
    for root in roots:
        try:
            routed = routable_checkout(root, server_cwd=server_cwd)
        except (OSError, ValueError, RuntimeError, UnicodeError):
            continue
        if routed is not None:
            return routed
    return None


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
            return CallerCheckout(peer, first_routable_workspace(roots, server_cwd=server_cwd))
        if peer is not None and process_client(client_name, peer):
            return CallerCheckout(
                peer, process_checkout(peer, server_cwd=server_cwd, proc_root=proc_root)
            )
    except (OSError, ValueError, RuntimeError, UnicodeError):
        return CallerCheckout(peer, None)  # An unreadable lookup keeps the server cwd.
    return CallerCheckout(peer, None)

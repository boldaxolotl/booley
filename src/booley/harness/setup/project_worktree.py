"""Compatibility adapters for the runtime-owned Ticket Workspace."""

from __future__ import annotations

from pathlib import Path

from booley.harness.models import TicketContext
from booley.runtime.ticket_repositories import (
    TicketWorkspace,
    TicketWorkspaceError,
    TicketWorkspaceRequest,
    WorkspaceMode,
    remove_project_worktree,
)

ProjectWorktreeError = TicketWorkspaceError


def ticket_workspace(ctx: TicketContext) -> TicketWorkspace:
    """Build the runtime workspace described by a validated harness context."""
    if ctx.worktree_path is None:
        raise TicketWorkspaceError("Ticket worktree is unavailable")
    expected_sha = ctx.target_contract.project_sha if ctx.target_contract is not None else ""
    return TicketWorkspace(
        TicketWorkspaceRequest(
            project_root=ctx.project_root,
            worktree=ctx.worktree_path,
            ticket_slug=ctx.slug,
            base=ctx.branch,
            ticket_scope=tuple(ctx.scope_raw),
            mode=WorkspaceMode(ctx.workspace_intent),
            expected_sha=expected_sha,
        )
    )


def prepare_project_worktree(ctx: TicketContext) -> Path | None:
    """Prepare ticket-authored project content through TicketWorkspace."""
    if ctx.worktree_path is None:
        return None
    return ticket_workspace(ctx).prepare()


__all__ = [
    "ProjectWorktreeError",
    "prepare_project_worktree",
    "remove_project_worktree",
    "ticket_workspace",
]

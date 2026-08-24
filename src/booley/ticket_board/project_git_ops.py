"""Compatibility exports for runtime-owned Ticket Workspace operations."""

from booley.runtime.ticket_repositories import (
    cleanup_project_ticket_branch,
    merge_project_ticket_branch,
)

__all__ = ["cleanup_project_ticket_branch", "merge_project_ticket_branch"]

"""Ticket archive operations — remove completed/specific tickets from the board.

Extracted from operations.py for single-responsibility (P8).
"""

from __future__ import annotations

import contextlib
import logging
import shutil
import sys
from pathlib import Path
from typing import Any

from booley.runtime.ticket_repositories import TicketWorkspace, WorkspaceDisposition

from .frontmatter import parse_frontmatter
from .git_ops import cleanup_worktree_and_branch
from .io import scan_all_tickets
from .paths import existing_ticket_runtime_file, ticket_log_dir

logger = logging.getLogger(__name__)


def _cleanup_session_files(log_dir: Path) -> None:
    """Remove *.session_id files — stale resume IDs are never worth keeping."""
    if not log_dir.exists():
        return
    for f in log_dir.glob("*.session_id"):
        with contextlib.suppress(OSError):
            f.unlink()


def _warn_dependents(tio, slug):
    """Warn about waiting tickets that depend on the slug being archived."""
    all_tickets = scan_all_tickets(tio.tickets_dir)
    dependents = [
        t.get("feature_branch") or Path(t.get("file", "")).stem
        for t in all_tickets
        if t.get("status") == "waiting" and slug in t.get("dependencies", [])
    ]
    if dependents:
        print(
            f"WARNING: these tickets depend on '{slug}' and will be "
            f"stuck in waiting/: {', '.join(dependents)}. Edit their "
            f"dependencies or archive them too.",
            file=sys.stderr,
        )


def _cleanup_log_dir(log_dir, keep_logs):
    """Remove log dir contents except the lock held by the caller."""
    _cleanup_session_files(log_dir)
    if not keep_logs and log_dir.exists():
        for entry_path in log_dir.iterdir():
            if entry_path.name == ".runtime":
                runtime_lock = entry_path / "ticket.lock"
                for runtime_entry in entry_path.iterdir():
                    if runtime_entry == runtime_lock:
                        continue
                    if runtime_entry.is_dir():
                        shutil.rmtree(str(runtime_entry))
                    else:
                        runtime_entry.unlink()
                continue
            if entry_path.name == "ticket.lock":
                continue
            if entry_path.is_dir():
                shutil.rmtree(str(entry_path))
            else:
                entry_path.unlink()


def _cleanup_log_dir_phase2(log_dir, keep_logs):
    """Phase 2 cleanup: remove lock file and empty dir after lock release."""
    if not keep_logs and log_dir.exists():
        existing_ticket_runtime_file(log_dir, "ticket.lock").unlink(missing_ok=True)
        with contextlib.suppress(OSError):
            (log_dir / ".runtime").rmdir()
        with contextlib.suppress(OSError):
            log_dir.rmdir()


def _archive_single(tio: Any, slug: str, keep_logs: bool, force: bool) -> list[str]:
    """Archive a single ticket by slug; non-``done`` states need *force* (A-5)."""
    from .io import find_ticket_file

    file_path, status = find_ticket_file(tio.tickets_dir, slug)
    if file_path is None:
        print(f"Error: ticket '{slug}' not found", file=sys.stderr)
        return []
    # Lookup accepts feature-branch aliases; persistent ticket state does not.
    slug = file_path.stem

    # A queued/running/review ticket silently disappearing is a footgun (A-5):
    # archiving is destructive (worktree, branch, and logs go with it), so a
    # ticket that is not finished requires an explicit --force.
    if status != "done" and not force:
        print(
            f"Error: ticket '{slug}' is '{status}', not 'done' — archiving "
            f"discards its state, worktree and branch. "
            f"Use --force to archive it anyway.",
            file=sys.stderr,
        )
        return []

    with file_path.open(encoding="utf-8") as f:
        fields, _ = parse_frontmatter(f.read())
    summary = fields.get("summary", slug)
    _warn_dependents(tio, slug)

    log_dir = ticket_log_dir(tio.logs_dir, slug)
    with tio._ticket_lock(slug):
        ok, _detail = TicketWorkspace.retire(
            tio._project_root,
            slug,
            WorkspaceDisposition.DISCARD,
        )
        if not ok:
            print(
                f"Error: could not clean up project repository branch for '{slug}'",
                file=sys.stderr,
            )
            return []
        if fields.get("feature_branch", "") and not cleanup_worktree_and_branch(
            fields["feature_branch"], force=True
        ):
            print(
                f"Error: could not clean up feature branch for '{slug}'",
                file=sys.stderr,
            )
            return []
        tio._append_transition_unlocked(
            slug,
            f"{status}:{fields.get('step', '')}",
            f"archived:{fields.get('step', '')}",
            "ticket-triage",
            "user archived",
        )
        file_path.unlink(missing_ok=True)
        _cleanup_log_dir(log_dir, keep_logs)

    _cleanup_log_dir_phase2(log_dir, keep_logs)
    return [summary]


def op_archive(
    tio: Any, slug: str | None = None, keep_logs: bool = False, force: bool = False
) -> list[str]:
    """Archive tickets: remove from board and clean up files.

    When *slug* is provided, archives that specific ticket — from any status
    if *force*, otherwise ``done`` only (A-5).
    When no slug, cleans all done/ tickets.
    Returns list of archived ticket summaries.
    """
    if slug is not None:
        return _archive_single(tio, slug, keep_logs, force)

    archived = []
    scan_dir = tio.tickets_dir / "board" / "done"
    if not scan_dir.is_dir():
        return archived

    for md_file in sorted(scan_dir.glob("*.md")):
        ticket_slug = md_file.stem
        log_dir = ticket_log_dir(tio.logs_dir, ticket_slug)
        with tio._ticket_lock(ticket_slug):
            try:
                with md_file.open(encoding="utf-8") as f:
                    fields, _ = parse_frontmatter(f.read())
                summary = fields.get("summary", md_file.stem)
            except OSError:
                summary = md_file.stem
                fields = {}
            ok, _detail = TicketWorkspace.retire(
                tio._project_root,
                ticket_slug,
                WorkspaceDisposition.DISCARD,
            )
            if not ok:
                print(
                    f"Error: could not clean up project repository branch for '{ticket_slug}'",
                    file=sys.stderr,
                )
                continue
            if fields.get("feature_branch", "") and not cleanup_worktree_and_branch(
                fields["feature_branch"], force=True
            ):
                print(
                    f"Error: could not clean up feature branch for '{ticket_slug}'",
                    file=sys.stderr,
                )
                continue
            archived.append(summary)
            md_file.unlink()
            _cleanup_log_dir(log_dir, keep_logs)
        _cleanup_log_dir_phase2(log_dir, keep_logs)

    return archived

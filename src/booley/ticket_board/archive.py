"""Archive: abandon a live Ticket, closing it into Ticket History (ADR 0065).

Archiving releases the Ticket's worktrees and refs, then closes it with outcome
archived (:func:`booley.ticket_board.ticket_history.close_ticket`) and commits
the history record. Logs are kept. A done Ticket closes by itself when its
completion finishes, so it is never archived; a Closed Ticket is never
reopened. Waiting dependents of an archived Ticket are blocked lazily by
waiting promotion, not here.

A marker under ``.runtime/acceptance/archive/`` makes each archive resumable
after a crash; it is removed once the Ticket is closed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from booley.core.boundary import require_bool, require_dict, require_str, require_str_value
from booley.runtime.project_dir import runtime_dir

from .archive_generation import plan_generation, release_generation
from .board_layout import board_relative_document_path
from .frontmatter import parse_frontmatter
from .git_ops import cleanup_worktree_and_branch
from .history_publication import publish_history_record
from .lifecycle import TicketState
from .paths import ticket_log_dir
from .persistence import atomic_replace_bytes, durable_unlink
from .ticket_history import ClosedBlock, close_ticket, read_closed_ticket, ticket_generation
from .ticket_repositories import (
    TicketWorkspace,
    WorkspaceDisposition,
    has_project_ticket_branch,
)


@dataclass
class ArchiveOutcome:
    """Successful archives and failures from one command invocation."""

    archived: list[str] = field(default_factory=list)
    failures: dict[str, str] = field(default_factory=dict)


def _cleanup_session_files(log_dir: Path) -> None:
    """Remove *.session_id files — stale resume IDs are never worth keeping."""
    if not log_dir.exists():
        return
    for f in log_dir.glob("*.session_id"):
        f.unlink()


def _marker_path(root: Path, slug: str) -> Path:
    return runtime_dir(root) / "acceptance" / "archive" / f"{slug}.json"


def _save_marker(path: Path, marker: dict) -> None:
    atomic_replace_bytes(path, (json.dumps(marker, sort_keys=True) + "\n").encode())


def _load_marker(path: Path, slug: str) -> dict | None:
    if not path.exists():
        return None
    marker = require_dict(json.loads(path.read_text(encoding="utf-8")), field="archive marker")
    expected = {
        "digest",
        "file",
        "summary",
        "status",
        "step",
        "generation",
        "transitioned",
        "closed",
        "descriptor_retired",
    }
    if set(marker) != expected:
        raise ValueError(f"archive marker is invalid: {path}")
    ticket = Path(require_str(marker, "file"))
    for key in ("digest", "summary", "status"):
        require_str(marker, key)
    for key in ("transitioned", "closed", "descriptor_retired"):
        require_bool(marker, key)
    require_str_value(marker["generation"], field="archive marker generation", allow_empty=True)
    # A blank step is valid for an untouched draft Ticket.
    if marker["step"] != "":
        require_str(marker, "step")
    # The marker records the board-relative document path it archived.
    if (
        ticket != board_relative_document_path(slug)
        or re.fullmatch(r"[0-9a-f]{64}", marker["digest"]) is None
    ):
        raise ValueError(f"archive marker identity is invalid: {path}")
    return marker


def _pending_operations(tio: Any, slug: str) -> None:
    """Reject transactions whose second generation is not yet on the Board."""
    from .acceptance_journal import JournalState, acceptance_state
    from .amendment import pending_amendment
    from .basis_publication import load_basis_publication
    from .basis_refresh import load_basis_refresh
    from .draft_transition import transition_pending
    from .enqueue_publication import load_enqueue_journal
    from .ticket_jobs import active_ticket_jobs

    root = Path(tio._project_root)
    operations = (
        (load_enqueue_journal(root, slug), "enqueue", "retry enqueue"),
        (load_basis_publication(root, slug), "Basis Publication", "retry enqueue"),
        (load_basis_refresh(root, slug), "Basis Refresh", "retry refresh"),
        (transition_pending(root, slug), "return-to-draft", "retry return-to-draft"),
    )
    for journal, label, recovery in operations:
        if journal and getattr(journal, "state", None) != "done":
            raise RuntimeError(f"{label} is pending for {slug}; {recovery} before archive")
    amendment = pending_amendment(root, slug)
    if amendment is not None and amendment.get("phase") != "queued":
        raise RuntimeError(f"amendment is pending for {slug}; retry amend apply before archive")
    state = acceptance_state(tio.tickets_dir, slug)
    if state is not None and state is not JournalState.DONE:
        raise RuntimeError(f"acceptance is pending for {slug}; retry acceptance before archive")
    active = active_ticket_jobs(ticket_log_dir(tio.logs_dir, slug))
    if active:
        raise RuntimeError(f"active endpoint jobs remain for {slug}; wait or cancel them")


def _finish_archive(tio: Any, slug: str, marker_path: Path, marker: dict) -> None:
    """Close the Ticket as archived, persisting each checkpoint in the marker."""
    if not marker["transitioned"]:
        from .paths import human_log_file

        log = human_log_file(tio.logs_dir, slug, "transitions.log")
        detail = f"user archived; archive operation {marker['digest']}"
        if not log.exists() or detail not in log.read_text(encoding="utf-8"):
            tio._append_transition_unlocked(
                slug,
                f"{marker['status']}:{marker['step']}",
                f"archived:{marker['step']}",
                "ticket-triage",
                detail,
            )
        marker["transitioned"] = True
        _save_marker(marker_path, marker)
    if not marker["closed"]:
        ticket_path = tio.tickets_dir / marker["file"]
        if ticket_path.exists():
            digest = hashlib.sha256(ticket_path.read_bytes()).hexdigest()
            if digest != marker["digest"]:
                raise RuntimeError(f"Ticket {slug} changed during archive finalization")
        block = ClosedBlock.now(TicketState.ARCHIVED, marker["generation"])
        close_ticket(tio.tickets_dir, slug, block)
        marker["closed"] = True
        _save_marker(marker_path, marker)
    descriptor = runtime_dir(Path(tio._project_root)) / "acceptance" / "drafts" / f"{slug}.json"
    descriptor.unlink(missing_ok=True)
    marker["descriptor_retired"] = True
    _save_marker(marker_path, marker)
    _cleanup_session_files(ticket_log_dir(tio.logs_dir, slug))


def _refuse_closed_or_done(tio: Any, slug: str, status: str | None) -> None:
    if status == TicketState.DONE.status:
        raise RuntimeError(
            "Ticket is done and closes when its completion finishes; "
            f"retry with 'booley board approve {slug}'"
        )
    if status is None:
        closed = read_closed_ticket(tio.tickets_dir, slug)
        if closed is not None:
            raise RuntimeError(f"Ticket is already closed ({closed.closed.outcome.status})")
        raise RuntimeError("Ticket not found")


def _prepare_marker(tio: Any, slug: str, path: Path) -> dict:
    """Read the live Ticket under lock, then release its generation once."""
    from .io import find_ticket_file

    file_path, status = find_ticket_file(tio.tickets_dir, slug)
    marker = _load_marker(path, slug)
    if marker is not None:
        if (
            file_path is not None
            and hashlib.sha256(file_path.read_bytes()).hexdigest() != (marker["digest"])
        ):
            raise RuntimeError("Ticket changed during unfinished archive")
        return marker
    _refuse_closed_or_done(tio, slug, status if file_path is not None else None)
    assert file_path is not None and status is not None
    fields = tio.inspect_ticket(slug)
    if fields is None:
        raise RuntimeError("Ticket disappeared during archive")
    digest = hashlib.sha256(file_path.read_bytes()).hexdigest()
    _pending_operations(tio, slug)
    root = Path(tio._project_root)
    generation = ticket_generation(root, slug, fields)
    _release_workspaces(tio, slug, status, fields, file_path)
    marker = {
        "digest": digest,
        "file": str(file_path.relative_to(tio.tickets_dir)),
        "summary": fields.get("summary", slug),
        "status": status,
        "step": fields.get("step", ""),
        "generation": generation,
        "transitioned": False,
        "closed": False,
        "descriptor_retired": False,
    }
    _save_marker(path, marker)
    return marker


def _release_workspaces(tio: Any, slug: str, status: str, fields: dict, file_path: Path) -> None:
    """Release the Ticket's worktrees and refs before it closes."""
    root = Path(tio._project_root)
    release_generation(plan_generation(root, slug, status, fields))
    if has_project_ticket_branch(root, slug):
        ok, detail = TicketWorkspace.retire(root, slug, WorkspaceDisposition.DISCARD)
        if not ok:
            raise RuntimeError(f"legacy project workspace cleanup failed: {detail}")
    # find_ticket supplies the slug as a fallback alias. Only an explicitly
    # recorded legacy branch belongs to this Ticket.
    authored_fields, _ = parse_frontmatter(file_path.read_text(encoding="utf-8"))
    feature_branch = authored_fields.get("feature_branch", "")
    if feature_branch and not cleanup_worktree_and_branch(feature_branch, force=True):
        raise RuntimeError(f"feature branch cleanup failed: {feature_branch}")


def _refuse_live_owner(tio: Any, slug: str) -> None:
    """Refuse to abandon a Ticket a live developer process still owns.

    Checked before taking the Ticket lock, which stamps this process's PID.
    """
    from booley.runtime.pid import is_pid_alive

    from .board_layout import read_state_record
    from .operations import _live_owner_pid

    owners = {_live_owner_pid(tio, slug)}
    record = read_state_record(tio.tickets_dir, slug)
    if record is not None:
        pid = record.runtime["execution_owner_pid"]
        if pid is not None and pid != os.getpid() and is_pid_alive(pid):
            owners.add(pid)
    owners.discard(None)
    if owners:
        pid = min(owners)
        raise RuntimeError(f"Ticket is owned by live process {pid}; stop it (kill {pid}) first")


def _archive_single(tio: Any, slug: str) -> ArchiveOutcome:
    """Archive one Ticket under its lock, resuming an interrupted archive."""
    from .helpers import validate_ticket_slug
    from .io import find_ticket_file

    outcome = ArchiveOutcome()
    slug = slug.removesuffix(".md")
    try:
        validate_ticket_slug(slug)
        file_path, _ = find_ticket_file(tio.tickets_dir, slug)
        if file_path is not None:
            slug = file_path.stem
        marker_path = _marker_path(Path(tio._project_root), slug)
        _refuse_live_owner(tio, slug)
        with tio._ticket_lock(slug):
            marker = _prepare_marker(tio, slug, marker_path)
            _finish_archive(tio, slug, marker_path, marker)
        durable_unlink(marker_path)
        publish_history_record(tio.tickets_dir, slug, policy_root=Path(tio._project_root))
        outcome.archived.append(marker["summary"])
    except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
        outcome.failures[slug] = str(exc)
        print(f"Error: could not archive '{slug}': {exc}", file=sys.stderr)
    return outcome


def op_archive(tio: Any, slug: str | None = None) -> ArchiveOutcome:
    """Abandon the live Ticket *slug*, closing it with outcome archived.

    Without *slug*, only resumes archives a crash interrupted. Returns archived
    summaries and per-Ticket failures.
    """
    if slug is not None:
        return _archive_single(tio, slug)
    outcome = ArchiveOutcome()
    archive_dir = runtime_dir(Path(tio._project_root)) / "acceptance" / "archive"
    for marker_path in sorted(archive_dir.glob("*.json")):
        single = _archive_single(tio, marker_path.stem)
        outcome.archived.extend(single.archived)
        outcome.failures.update(single.failures)
    return outcome


def run_archive_command(
    tio: Any, slug: str | None, *, force: bool = False, keep_logs: bool = False
) -> int:
    """Run ``archive`` for either CLI; the retired flags only earn a note."""
    for flag, given in (("--force", force), ("--keep-logs", keep_logs)):
        if given:
            print(
                f"Note: {flag} has no effect: archive abandons any live Ticket and keeps "
                "its logs; the flag will be removed",
                file=sys.stderr,
            )
    return report_archive_outcome(op_archive(tio, slug=slug))


def report_archive_outcome(outcome: ArchiveOutcome) -> int:
    """Print one archive result consistently for both CLI entry points."""
    if outcome.archived:
        print(f"Archived {len(outcome.archived)} ticket(s):")
        for name in outcome.archived:
            print(f"  - {name}")
    if outcome.failures:
        print("Failed to archive: " + ", ".join(outcome.failures), file=sys.stderr)
        return 1
    if not outcome.archived:
        print("No interrupted archives to resume; name a Ticket to archive it.")
    return 0

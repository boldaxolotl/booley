"""Ticket archive operations — remove completed/specific tickets from the board.

Extracted from operations.py for single-responsibility (P8).
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from booley.runtime.project_dir import runtime_dir

from .archive_generation import plan_generation, release_generation
from .io import scan_all_tickets
from .paths import existing_ticket_runtime_file, ticket_log_dir
from .persistence import atomic_replace_bytes


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


def _warn_dependents(tio, slug):
    """Warn about waiting tickets that depend on the slug being archived."""
    all_tickets = scan_all_tickets(tio.tickets_dir, project_root=tio._project_root)
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
        (log_dir / ".runtime").rmdir()
        log_dir.rmdir()


def _marker_path(root: Path, slug: str) -> Path:
    return runtime_dir(root) / "acceptance" / "archive" / f"{slug}.json"


def _save_marker(path: Path, marker: dict) -> None:
    atomic_replace_bytes(path, (json.dumps(marker, sort_keys=True) + "\n").encode())


def _load_marker(path: Path, slug: str) -> dict | None:
    if not path.exists():
        return None
    marker = json.loads(path.read_text(encoding="utf-8"))
    expected = {
        "digest",
        "file",
        "summary",
        "status",
        "step",
        "keep_logs",
        "transitioned",
        "unlinked",
        "descriptor_retired",
        "logs_cleaned",
    }
    if not isinstance(marker, dict) or set(marker) != expected:
        raise ValueError(f"archive marker is invalid: {path}")
    if not isinstance(marker["file"], str):
        raise ValueError(f"archive marker identity is invalid: {path}")
    ticket = Path(marker["file"])
    if (
        ticket.name != f"{slug}.md"
        or len(ticket.parts) != 3
        or ticket.parts[0] != "board"
        or ticket.parts[1] in {".", ".."}
        or not isinstance(marker["digest"], str)
        or re.fullmatch(r"[0-9a-f]{64}", marker["digest"]) is None
        or any(not isinstance(marker[key], str) for key in ("summary", "status", "step"))
        or any(
            not isinstance(marker[key], bool)
            for key in (
                "keep_logs",
                "transitioned",
                "unlinked",
                "descriptor_retired",
                "logs_cleaned",
            )
        )
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
    """Persist each finalization checkpoint outside the log directory."""
    log_dir = ticket_log_dir(tio.logs_dir, slug)
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
    ticket_path = tio.tickets_dir / marker["file"]
    if ticket_path.exists():
        digest = hashlib.sha256(ticket_path.read_bytes()).hexdigest()
        if digest != marker["digest"]:
            raise RuntimeError(f"Ticket {slug} changed during archive finalization")
        ticket_path.unlink()
    marker["unlinked"] = True
    _save_marker(marker_path, marker)
    descriptor = runtime_dir(Path(tio._project_root)) / "acceptance" / "drafts" / f"{slug}.json"
    descriptor.unlink(missing_ok=True)
    marker["descriptor_retired"] = True
    _save_marker(marker_path, marker)
    _cleanup_log_dir(log_dir, marker["keep_logs"])


def _prepare_marker(tio: Any, slug: str, path: Path, keep_logs: bool, force: bool) -> dict:
    """Read the Ticket under lock, then release its generation once."""
    from .io import find_ticket_file

    file_path, status = find_ticket_file(tio.tickets_dir, slug)
    marker = _load_marker(path, slug)
    if file_path is None:
        if marker is None or marker["logs_cleaned"]:
            raise RuntimeError("Ticket not found")
        return marker
    if status != "done" and not force and marker is None:
        raise RuntimeError(f"Ticket is {status!r}; use --force to archive it")
    fields = tio.find_ticket(slug)
    if fields is None:
        raise RuntimeError("Ticket disappeared during archive")
    digest = hashlib.sha256(file_path.read_bytes()).hexdigest()
    if marker is not None and not marker["logs_cleaned"]:
        if marker["digest"] != digest:
            raise RuntimeError("Ticket changed during unfinished archive")
        return marker
    _pending_operations(tio, slug)
    release_generation(plan_generation(Path(tio._project_root), slug, status, fields))
    _warn_dependents(tio, slug)
    marker = {
        "digest": digest,
        "file": str(file_path.relative_to(tio.tickets_dir)),
        "summary": fields.get("summary", slug),
        "status": status,
        "step": fields.get("step", ""),
        "keep_logs": keep_logs,
        "transitioned": False,
        "unlinked": False,
        "descriptor_retired": False,
        "logs_cleaned": False,
    }
    _save_marker(path, marker)
    return marker


def _archive_single(tio: Any, slug: str, keep_logs: bool, force: bool) -> ArchiveOutcome:
    """Archive one Ticket under its lock, including recovery after unlink."""
    from .helpers import validate_ticket_slug
    from .io import find_ticket_file

    outcome = ArchiveOutcome()
    slug = slug.removesuffix(".md")
    try:
        validate_ticket_slug(slug)
        file_path, _ = find_ticket_file(tio.tickets_dir, slug)
        if file_path is not None:
            slug = file_path.stem
        log_dir = ticket_log_dir(tio.logs_dir, slug)
        marker_path = _marker_path(Path(tio._project_root), slug)
        with tio._ticket_lock(slug):
            marker = _prepare_marker(tio, slug, marker_path, keep_logs, force)
            _finish_archive(tio, slug, marker_path, marker)
        _cleanup_log_dir_phase2(log_dir, marker["keep_logs"])
        marker["logs_cleaned"] = True
        _save_marker(marker_path, marker)
        outcome.archived.append(marker["summary"])
    except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
        outcome.failures[slug] = str(exc)
        print(f"Error: could not archive '{slug}': {exc}", file=sys.stderr)
    return outcome


def op_archive(
    tio: Any, slug: str | None = None, keep_logs: bool = False, force: bool = False
) -> ArchiveOutcome:
    """Archive tickets: remove from board and clean up files.

    When *slug* is provided, archives that specific ticket — from any status
    if *force*, otherwise ``done`` only (A-5).
    When no slug, cleans all done/ tickets.
    Returns archived summaries and per-Ticket failures.
    """
    if slug is not None:
        return _archive_single(tio, slug, keep_logs, force)

    outcome = ArchiveOutcome()
    scan_dir = tio.tickets_dir / "board" / "done"
    if not scan_dir.is_dir():
        return outcome

    for md_file in sorted(scan_dir.glob("*.md")):
        single = _archive_single(tio, md_file.stem, keep_logs, force)
        outcome.archived.extend(single.archived)
        outcome.failures.update(single.failures)
    return outcome

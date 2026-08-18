"""Orphan ticket handling — detect and resolve tickets stuck in active/.

Extracted from booley.py for single-responsibility (P8).  Three entry points:

- handle_startup_orphans:  block tickets whose owning PID is dead.
- handle_post_run_orphans: requeue / block / fail tickets still active after
  the harness subprocess exits.
- _diagnose_crash:         translate exit codes into human-readable reasons.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from booley.harness.colors import bold, red, yellow
from booley.harness.terminal import status_indent
from booley.ticket_board.helpers import tickets_dir_from_project_root
from booley.ticket_board.paths import existing_runtime_file

logger = logging.getLogger("booley")


# ---------------------------------------------------------------------------
# Startup orphan scan
# ---------------------------------------------------------------------------


def handle_startup_orphans(project_root: Path) -> int:
    """Block tickets in active/ only if their lock PID is dead.

    Skips tickets with unreadable PIDs (possible concurrent pickup).
    See docstring on _read_pid_with_retry for TOCTOU rationale.

    Returns the number of tickets recovered (blocked), so callers that
    report — ``booley doctor``'s board self-heal check (ADR 0028 Decision
    11) — can say how many tickets they touched.
    """
    # Lazy imports to break circular dependency with booley.harness.booley
    from booley.harness.booley import _run_board, get_active_slugs, get_ticket_summary
    from booley.ticket_board.helpers import is_pid_alive, read_lock_pid

    orphans = get_active_slugs(project_root)
    if not orphans:
        return 0

    logger.debug("Found %d ticket(s) in active/, checking PID liveness", len(orphans))
    blocked_count = 0
    for slug in orphans:
        lock_path = existing_runtime_file(
            tickets_dir_from_project_root(project_root) / "logs",
            slug,
            "ticket.lock",
        )
        pid = _read_pid_with_retry(lock_path, read_lock_pid)

        if pid is None:
            logger.debug(
                "Ticket '%s': no readable PID in %s after retries -- "
                "skipping (possible concurrent pickup)",
                slug,
                lock_path,
            )
            continue
        if is_pid_alive(pid):
            logger.debug("Ticket '%s' has live PID %d, skipping", slug, pid)
            continue

        summary = get_ticket_summary(project_root, slug)
        logger.debug("Blocking orphaned ticket '%s' (%s) -- PID %s dead", slug, summary, pid)
        status_indent(f"{yellow('!')} Blocking orphaned ticket {bold(repr(slug))} ({summary})")
        _run_board(
            project_root,
            [
                "block",
                slug,
                "--reason",
                "Found in active/ with dead PID on loop runner startup",
                "--step",
                "unknown",
            ],
        )
        blocked_count += 1

    if blocked_count:
        print()
    return blocked_count


def find_startup_orphans(project_root: Path) -> list[str]:
    """Return active tickets whose recorded Developer PID is dead.

    This is the observational half of :func:`handle_startup_orphans`, used by
    automatic Doctor runs that must report state without moving tickets.
    """
    from booley.harness.booley import get_active_slugs
    from booley.ticket_board.helpers import is_pid_alive, read_lock_pid

    active = get_active_slugs(project_root)
    if active:
        logger.debug("Found %d ticket(s) in active/, checking PID liveness", len(active))
    orphans: list[str] = []
    for slug in active:
        lock_path = existing_runtime_file(
            tickets_dir_from_project_root(project_root) / "logs", slug, "ticket.lock"
        )
        pid = _read_pid_with_retry(lock_path, read_lock_pid)
        if pid is None:
            logger.debug("Ticket '%s': PID unreadable; possible concurrent pickup", slug)
        elif is_pid_alive(pid):
            logger.debug("Ticket '%s' has live PID %d, skipping", slug, pid)
        else:
            logger.debug("Ticket '%s' has dead PID %d", slug, pid)
            orphans.append(slug)
    return orphans


def _read_pid_with_retry(lock_path: Path, read_lock_pid) -> int | None:
    """Retry PID read up to 3 times to handle concurrent lock writes."""
    pid = None
    for _ in range(3):
        pid = read_lock_pid(lock_path)
        if pid is not None:
            break
        time.sleep(0.1)
    return pid


# ---------------------------------------------------------------------------
# Crash diagnostics
# ---------------------------------------------------------------------------


def _diagnose_crash(exit_code: int) -> str:  # noqa: PLR0911 — exit-code lookup ladder; each return maps a distinct signal/code to its reason
    """Translate exit code into a human-readable crash reason."""
    if exit_code in (130, -2, 0xC000013A):
        return "killed by SIGINT (Ctrl+C or external interrupt)"
    if exit_code in (137, -9):
        return "killed by SIGKILL (likely OOM or Docker memory limit)"
    if exit_code in (143, -15):
        return "killed by SIGTERM (graceful shutdown signal)"
    if exit_code == -1:
        return "Docker container failed to start"
    if exit_code == 1:
        return "agent exited with error (turn exhaustion or unhandled exception)"
    if exit_code == 2:
        return "agent CLI argument error"
    return f"unknown crash (exit code {exit_code})"


# ---------------------------------------------------------------------------
# Post-run orphan handling
# ---------------------------------------------------------------------------


def handle_post_run_orphans(project_root: Path, exit_code: int, limit_wait: int) -> None:
    """Safety net: handle tickets still in active/ after execution exit.

    Skips tickets owned by another live runner process.
    """
    # Lazy imports to break circular dependency with booley.harness.booley
    from booley.harness.booley import get_active_slugs
    from booley.ticket_board.helpers import is_pid_alive, read_lock_pid

    orphans = get_active_slugs(project_root)
    if not orphans:
        return

    my_pid = os.getpid()
    for slug in orphans:
        lock_path = existing_runtime_file(
            tickets_dir_from_project_root(project_root) / "logs",
            slug,
            "ticket.lock",
        )
        pid = read_lock_pid(lock_path)
        if pid is not None and pid != my_pid and is_pid_alive(pid):
            logger.debug("Post-run: ticket '%s' owned by live PID %d, skipping", slug, pid)
            continue

        _handle_single_orphan(project_root, slug, exit_code, limit_wait)


def _handle_single_orphan(
    project_root: Path,
    slug: str,
    exit_code: int,
    limit_wait: int,
) -> None:
    """Resolve a single orphaned ticket based on exit conditions."""
    # Lazy import to break circular dependency with booley.harness.booley
    from booley.harness.booley import _run_board

    if limit_wait > 0:
        logger.debug("Subscription limit -- requeueing '%s'", slug)
        status_indent(f"{yellow('[wait]')} Subscription limit -- requeueing {bold(repr(slug))}")
        _run_board(
            project_root,
            [
                "requeue",
                slug,
                "--reason",
                f"subscription limit -- requeued, waiting {limit_wait}s",
            ],
        )
    elif exit_code == 0:
        logger.debug("Ticket '%s' still active after clean exit -- blocking", slug)
        status_indent(
            f"{yellow('!')} Ticket {bold(repr(slug))} still active after clean exit -- {yellow('blocking for triage')}"
        )
        _run_board(
            project_root,
            [
                "block",
                slug,
                "--reason",
                "Harness exited cleanly without transitioning ticket (developer bug?)",
                "--step",
                "unknown",
            ],
        )
    else:
        reason = _diagnose_crash(exit_code)
        logger.debug(
            "Ticket '%s' still active after crash (code %d: %s) -- failing",
            slug,
            exit_code,
            reason,
        )
        status_indent(
            f"{red('!')} Ticket {bold(repr(slug))} crashed ({reason}) -- {red('failing')}"
        )
        _run_board(
            project_root,
            [
                "fail",
                slug,
                "--error",
                f"Harness crashed: {reason} (code {exit_code})",
                "--step",
                "unknown",
            ],
        )

"""Composite ticket operations: block, fail, handoff, merge, reset, etc."""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import sys
import tempfile
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from booley.runtime.pid import is_pid_alive
from booley.runtime.ticket_repositories import TicketWorkspace, WorkspaceDisposition
from booley.runtime.timefmt import format_human_datetime

logger = logging.getLogger(__name__)

from .git_ops import cleanup_worktree_and_branch
from .helpers import compute_done_slugs, parse_arrow, slug_from_file
from .io import scan_all_tickets
from .lifecycle import (
    STATE_BY_DIR,
    STATE_BY_STATUS,
    TicketState,
    format_user_board_moves,
    is_user_board_move,
)
from .logs import RESET_BOUNDARY_PREFIX, reset_progress, save_progress
from .notifications import is_event_enabled, ntfy_review_digest, ntfy_send
from .paths import (
    existing_human_log_file,
    existing_runtime_file,
    ticket_human_log_file,
    ticket_log_dir,
)

if TYPE_CHECKING:  # booley.core.models is imported lazily in the bodies below
    from booley.core.models import OnSuccess

    from .acceptance_ledger import AcceptanceSnapshot

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _op_move_and_log(
    tio,
    slug,
    to_dir,
    updates,
    transition: tuple[str, str, str, str],
    append_step=None,
    *,
    expected_status: str | None = None,
    expected_execution_id: str | None = None,
    before_move: Callable[[], bool] | None = None,
):
    """Move ticket, update fields, log transition atomically.

    Transition is written inside move_and_update's lock to prevent
    interleaved writes under concurrent execution (docs/PRINCIPLES §7).
    """
    from_state = transition[0]
    locked_status = expected_status or from_state.partition(":")[0]
    success = tio.move_and_update(
        slug,
        to_dir,
        updates,
        append_step=append_step,
        transition=transition,
        enforce_lifecycle=True,
        expected_status=locked_status,
        expected_execution_id=expected_execution_id,
        before_move=before_move,
    )
    if not success:
        print(f"Error: operation failed for '{slug}'", file=sys.stderr)
        return False

    # Clear stale PID from ticket.lock when leaving active/ (running state).
    # Without this, a requeued ticket can be falsely blocked if the OS
    # reuses the old PID for an unrelated process.
    if to_dir != "active" and from_state.startswith("running"):
        _clear_lock_pid(tio, slug)

    return True


def _clear_lock_pid(tio, slug: str) -> None:
    """Erase the PID stamp from ticket.lock under per-ticket lock (§7)."""
    lock_path = existing_runtime_file(tio.logs_dir, slug, "ticket.lock")
    if not lock_path.exists():
        return
    with tio._ticket_lock(slug), contextlib.suppress(OSError):
        lock_path.write_text("", encoding="utf-8")


def _find_last_run_start(lines):
    """Find the index of the last run-start entry in transition lines."""
    last_run_start = -1
    for i, line in enumerate(lines):
        parts = line.split(" | ")
        if len(parts) < 3:
            continue
        detail = parts[3].strip() if len(parts) >= 4 else ""
        to_state = ""
        arrow_result = parse_arrow(parts[1].strip())
        if arrow_result:
            to_state = arrow_result[1]
        if "picked up" in detail or to_state == "running:init" or "running:setup" in to_state:
            last_run_start = i
    return last_run_start


def _extract_logged_steps(lines, last_run_start):
    """Extract step names logged as 'step complete' from the current run."""
    logged_steps = set()
    for line in lines[max(0, last_run_start) :]:
        parts = line.split(" | ")
        if len(parts) < 4:
            continue
        detail = parts[3].strip()
        arrow_result = parse_arrow(parts[1].strip())
        if arrow_result is None:
            continue
        from_state, to_state = arrow_result

        if "step complete" in detail:
            if ":" in from_state:
                logged_steps.add(from_state.split(":")[-1])
            if ":" in to_state:
                logged_steps.add(to_state.split(":")[-1])

        if "ready for user review" in detail and ":" in to_state:
            logged_steps.add(to_state.split(":")[-1])

    return logged_steps


def _validate_transitions_for_handoff(tio, slug, entry):
    """Verify transitions.log has entries for all completed steps.

    Returns True if valid, False (with stderr messages) if not.
    """
    log_path = existing_human_log_file(tio.logs_dir, slug, "transitions.log")
    if not log_path.exists():
        print(
            f"HANDOFF GATE: transitions.log missing for '{slug}'. "
            f"No step transitions were recorded.",
            file=sys.stderr,
        )
        return False

    steps_completed = entry.get("steps_completed", []) if entry else []
    if not steps_completed:
        return True

    lines = [l.strip() for l in log_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    last_run_start = _find_last_run_start(lines)
    logged_steps = _extract_logged_steps(lines, last_run_start)

    # Exempt: setup (logged as "picked up"), summary (recorded by handoff)
    yaml_steps = set(steps_completed) - {"setup"}
    yaml_steps.discard("summary")
    missing = yaml_steps - logged_steps

    if missing:
        print(
            f"HANDOFF GATE: {len(missing)} step(s) in steps_completed but "
            f"NOT in transitions.log: {sorted(missing)}",
            file=sys.stderr,
        )
        print(f"  Logged steps: {sorted(logged_steps)}", file=sys.stderr)
        print(f"  YAML steps:   {sorted(set(steps_completed))}", file=sys.stderr)
        print(
            "The executing agent likely bypassed the update-board CLI. "
            "Use 'update-board --append-step --log' for each missing step, "
            "or re-execute the ticket.",
            file=sys.stderr,
        )
        return False

    return True


# ---------------------------------------------------------------------------
# Public operations
# ---------------------------------------------------------------------------


def _get_old_state(tio, slug, default_step=""):
    """Look up a ticket and return (entry, old_status, old_step).

    NOTE: reads outside the per-ticket lock — the returned state may be stale
    if another process mutates the ticket concurrently.  This only affects
    transition log accuracy, not state-mutation correctness (§7).
    """
    entry = tio.find_ticket(slug)
    old_status = entry.get("status", "running") if entry else "running"
    old_step = entry.get("step", default_step) if entry else default_step
    return entry, old_status, old_step


def op_activate(
    tio: Any,
    slug: str,
    owner_pid: int | None = None,
    execution_id: str | None = None,
) -> bool:
    """Activate a ticket for execution: move to active/, log transition.

    When the ticket is already running, checks PID ownership to prevent
    two runners from executing the same ticket concurrently. Returns False
    if another live process owns the ticket.

    Args:
        owner_pid: PID claiming ownership (default: current process).
                   Written to ticket.lock atomically under the lock.
    """
    from .helpers import read_lock_pid

    if owner_pid is None:
        owner_pid = os.getpid()
    execution_id = execution_id or uuid.uuid4().hex

    entry, old_status, old_step = _get_old_state(tio, slug)

    if old_status == "running":
        lock_path = existing_runtime_file(tio.tickets_dir / "logs", slug, "ticket.lock")
        existing_pid = read_lock_pid(lock_path)
        if existing_pid is not None and existing_pid != owner_pid and is_pid_alive(existing_pid):
            return False  # another live runner owns this ticket
        # Dead or missing PID — safe to take over; stamp our PID under lock.
        # _ticket_lock already stamps the developer PID (via
        # *_DEVELOPER_PID env var) into ticket.lock on acquisition.
        # Acquiring the lock is itself atomic proof that no other process
        # holds it, so no re-read of the PID is needed inside the block.
        # (read_lock_pid / write_text would open a second handle and
        # collide with msvcrt.locking on Windows → Errno 13.)
        old_execution_id = entry.get("execution_id", "") if entry else ""
        return tio.stamp_execution(
            slug,
            execution_id,
            owner_pid,
            expected_execution_id=old_execution_id,
        )

    # F-43: this is the run loop's PRE-claim (`booley run --ticket <slug>` moves
    # the ticket to active/ before the harness starts), and the harness's own
    # init_ticket logs "picked up" moments later. Calling both "picked up
    # (resume)" told a never-run ticket's transitions.log it had resumed. The
    # resume wording is only truthful when there is something to resume: prior
    # progress, or a ticket coming back from blocked/failed.
    resumed = old_status in ("blocked", "failed") or bool(entry and entry.get("steps_completed"))
    detail = "picked up (resume)" if resumed else "claimed for execution"
    return _op_move_and_log(
        tio,
        slug,
        "active",
        {"execution_id": execution_id, "execution_owner_pid": owner_pid},
        (
            f"{old_status}:{old_step}",
            f"running:{old_step}",
            "ticket-execute",
            detail,
        ),
    )


def op_claim(tio: Any, slug: str) -> bool:
    """Atomically claim a queued ticket for execution.

    Acquires the per-ticket lock and verifies the ticket is still
    queued before moving to active/. Returns True on success, False
    if the ticket was already claimed by another runner.

    Unlike init_ticket (which creates logs and copies the ticket),
    this is a lightweight atomic guard used during auto-select to
    close the TOCTOU window in concurrent ticket pickup (§7).
    """
    from .io import find_ticket_file

    with tio._ticket_lock(slug):
        file_path, status = find_ticket_file(tio.tickets_dir, slug)
        if file_path is None or status != "queued":
            return False
        active_dir = tio.tickets_dir / "board" / "active"
        active_dir.mkdir(parents=True, exist_ok=True)
        dest = active_dir / file_path.name
        if dest.exists():
            return False
        progress = tio._load_or_bootstrap_progress(slug, file_path)
        progress["execution_id"] = uuid.uuid4().hex
        progress["execution_owner_pid"] = os.getpid()
        save_progress(tio.logs_dir, slug, progress)
        shutil.move(str(file_path), str(dest))
        tio._append_transition_unlocked(
            slug, "queued:claim", "running:claim", "ticket-execute", "claimed for execution"
        )
    return True


def op_block(
    tio: Any,
    slug: str,
    reason: str,
    step: str,
    *,
    expected_execution_id: str | None = None,
) -> bool:
    """Block a ticket: move to blocked/, update frontmatter, log transition."""
    entry, old_status, old_step = _get_old_state(tio, slug, step)

    ok = _op_move_and_log(
        tio,
        slug,
        "blocked",
        {"blocked_reason": reason, "blocked_step": step},
        (f"{old_status}:{old_step}", f"blocked:{step}", "ticket-execute", f"blocked -- {reason}"),
        expected_status="running" if expected_execution_id is not None else None,
        expected_execution_id=expected_execution_id,
    )
    if ok and is_event_enabled("blocked"):
        ticket_name = entry.get("summary", slug) if entry else slug
        body = f"{step} | {reason}"[:120]
        ntfy_send(f"BLOCKED: {ticket_name}", body, priority="4")
    return ok


def op_fail(tio: Any, slug: str, error: str, step: str) -> bool:
    """Fail a ticket: delegates to op_block() with error semantics.

    Kept as a thin wrapper so callers (state machine, orphan detection)
    don't need to change.  The ticket lands in blocked/, not failed/.
    """
    return op_block(tio, slug, reason=error, step=step)


def op_requeue(tio: Any, slug: str, reason: str = "requeued") -> bool:
    """Requeue an interrupted run after proving no other process owns it."""
    entry, old_status, old_step = _get_old_state(tio, slug)
    if entry:
        slug = Path(str(entry["file"])).stem
    live_pid = _live_owner_pid(tio, slug)
    if live_pid is not None:
        print(
            f"Error: ticket '{slug}' is owned by a live process (PID {live_pid}).\n"
            "  Stop the active run before requeueing it.",
            file=sys.stderr,
        )
        return False

    return _op_move_and_log(
        tio,
        slug,
        "queue",
        {
            "step": "",
            "workspace_intent": "resume",
            "error": None,
            "failed_step": None,
            "blocked_reason": None,
            "blocked_step": None,
        },
        (f"{old_status}:{old_step}", "queued:requeue", "loop-runner", reason),
        expected_execution_id=str(entry.get("execution_id", "")) if entry else None,
    )


def _handoff_to_review(
    tio,
    slug,
    entry,
    old_status,
    old_step,
    expected_execution_id: str | None,
):
    """Move ticket to review/ and send notification if enabled.

    ``on_success.cleanup`` is deliberately NOT honored here: the reviewer needs
    the worktree and branch to inspect the work. Cleanup runs later, in
    ``op_complete``, once the review is approved. That is easy to mistake for a
    bug, so say it out loud (F-55).
    """
    from booley.core.models import OnSuccess

    if OnSuccess.from_dict(entry.get("on_success") if entry else None).cleanup:
        print(
            f"  Keeping worktree/branch for '{slug}': cleanup is deferred until "
            "the review is approved (destination=review)."
        )
    ok = _op_move_and_log(
        tio,
        slug,
        "review",
        {"step": "summary"},
        (f"{old_status}:{old_step}", "review:summary", "ticket-execute", "ready for user review"),
        append_step="summary",
        expected_status="running" if expected_execution_id is not None else None,
        expected_execution_id=expected_execution_id,
        before_move=lambda: _prepare_handoff_snapshot(tio, slug, entry, expected_execution_id),
    )
    if ok and is_event_enabled("review"):
        ticket_name = entry.get("summary", slug) if entry else slug
        digest = ntfy_review_digest(tio.logs_dir, slug)
        body = digest[:120] if digest else ""
        ntfy_send(f"REVIEW: {ticket_name}", body)
    return ok


def _prepare_handoff_snapshot(
    tio: Any,
    slug: str,
    entry: dict | None,
    expected_execution_id: str | None,
) -> bool:
    """Fence Jobs and freeze live acceptance before a review transition."""
    log_dir = ticket_log_dir(tio.logs_dir, slug)
    if not _handoff_jobs_clear(log_dir, slug):
        return False
    participant_heads = _handoff_basis_heads(tio, slug)
    if participant_heads is None:
        return False
    existing = _bind_existing_handoff_snapshot(log_dir, slug, participant_heads)
    if existing is not None:
        return existing
    return _freeze_handoff_snapshot(
        tio,
        slug,
        entry,
        expected_execution_id,
        log_dir,
        participant_heads,
    )


def _handoff_basis_heads(tio: Any, slug: str) -> dict[str, str] | None:
    """Validate current and live Basis views, returning exact participant heads."""
    from .acceptance_basis import (
        AcceptanceBasisError,
        assert_live_inputs_unchanged,
        materialize_ticket_commits,
        validate_current_basis_refs,
    )

    try:
        basis = _load_handoff_basis(tio, slug)
        heads = validate_current_basis_refs(tio._project_root, basis)
        with tempfile.TemporaryDirectory(prefix="booley-handoff-basis-") as directory:
            current = materialize_ticket_commits(
                tio._project_root,
                basis,
                Path(directory) / "checkout",
                heads,
            )
            errors = _prepare_materialized_basis_view(tio, slug, current, basis)
            assert_live_inputs_unchanged(basis, tio._project_root, current)
        if errors:
            raise AcceptanceBasisError("Acceptance Basis selectors changed: " + "; ".join(errors))
    except (AcceptanceBasisError, OSError, ValueError) as exc:
        print(f"Error: cannot hand off '{slug}': {exc}", file=sys.stderr)
        return None
    return heads


def _prepare_materialized_basis_view(
    tio: Any,
    slug: str,
    checkout: Path,
    basis: Any,
) -> list[str]:
    """Prepare and validate one exact composite through the runtime contract."""
    from booley.flows.execution import flow_enabled
    from booley.runtime.project_dir import resolve_checkout_project_dir
    from booley.runtime.project_prepare import prepare_project

    from .acceptance_basis import AcceptanceBasisError, validate_ticket_view
    from .io import find_ticket_file

    ticket, _status = find_ticket_file(tio.tickets_dir, slug)
    if ticket is None:
        raise AcceptanceBasisError(f"ticket {slug!r} is unavailable during Basis validation")
    try:
        project_dir = resolve_checkout_project_dir(checkout).resolve()
        project_dir.relative_to(checkout.resolve())
        preparation = prepare_project(
            tio._project_root,
            checkout,
            slug=slug,
            ticket_path=ticket,
            sim_flow_enabled=flow_enabled("sim", checkout),
        )
    except (FileNotFoundError, ValueError) as exc:
        raise AcceptanceBasisError(
            f"cannot prepare materialized Acceptance Basis at {checkout}: {exc}"
        ) from exc
    if not preparation.ok:
        raise AcceptanceBasisError(preparation.error)
    return validate_ticket_view(checkout, basis, allow_generated=True)


def _handoff_jobs_clear(log_dir: Path, slug: str) -> bool:
    from booley.harness.job_fence import active_ticket_jobs

    active = active_ticket_jobs(log_dir)
    if not active:
        return True
    names = ", ".join(f"{job.endpoint} ({job.run_id})" for job in active)
    print(
        f"Error: cannot hand off '{slug}' while endpoint Jobs are active: {names}",
        file=sys.stderr,
    )
    return False


def _bind_existing_handoff_snapshot(
    log_dir: Path,
    slug: str,
    participant_heads: dict[str, str],
) -> bool | None:
    from .acceptance_ledger import AcceptanceLedgerError, bind_review_package, read_acceptance

    accepted = read_acceptance(log_dir)
    if accepted.kind == "accepted":
        if accepted.snapshot is None:
            print(
                f"Error: cannot hand off '{slug}': accepted snapshot is unreadable",
                file=sys.stderr,
            )
            return False
        if accepted.snapshot.participant_heads != participant_heads:
            print(
                f"Error: cannot hand off '{slug}': Ticket heads changed after acceptance freeze",
                file=sys.stderr,
            )
            return False
        try:
            bind_review_package(log_dir, accepted.snapshot)
        except AcceptanceLedgerError as exc:
            print(f"Error: cannot bind review package for '{slug}': {exc}", file=sys.stderr)
            return False
        return True
    if accepted.kind == "corrupt":
        print(f"Error: cannot hand off '{slug}': {accepted.reason}", file=sys.stderr)
        return False
    return None


def _freeze_handoff_snapshot(
    tio: Any,
    slug: str,
    entry: dict | None,
    expected_execution_id: str | None,
    log_dir: Path,
    participant_heads: dict[str, str],
) -> bool:
    from booley.criteria.state import DevelopmentState

    from .acceptance_basis import AcceptanceBasisError
    from .acceptance_ledger import AcceptanceLedgerError, bind_review_package, freeze_acceptance
    from .criteria_acceptance import check_criteria_acceptance

    state_path = existing_runtime_file(tio.logs_dir, slug, "booley_state.json")
    if not state_path.exists():
        print(
            f"Error: cannot hand off '{slug}': durable criteria state is unavailable",
            file=sys.stderr,
        )
        return False
    state = DevelopmentState.load(state_path)
    work_dir = Path(state.work_dir) if state.work_dir else None
    verdict = check_criteria_acceptance(state_path, work_dir=work_dir)
    if verdict.disposition != "review":
        print(
            f"Error: cannot hand off '{slug}': acceptance is {verdict.disposition}",
            file=sys.stderr,
        )
        return False
    execution_id = expected_execution_id or str((entry or {}).get("execution_id", ""))
    try:
        evidence_basis = _handoff_basis_evidence(tio, slug)
        snapshot = freeze_acceptance(
            log_dir,
            DevelopmentState.load(state_path),
            execution_id=execution_id,
            acceptance_basis=evidence_basis,
            participant_heads=participant_heads,
        )
        bind_review_package(log_dir, snapshot)
    except (AcceptanceBasisError, AcceptanceLedgerError) as exc:
        print(f"Error: cannot freeze acceptance for '{slug}': {exc}", file=sys.stderr)
        return False
    return True


def _handoff_basis_evidence(tio: Any, slug: str) -> dict:
    """Load the durable enqueue receipt embedded in an Acceptance Snapshot."""
    from .acceptance_basis import load_basis_receipt

    basis = _load_handoff_basis(tio, slug)
    return load_basis_receipt(tio._project_root, slug, basis.as_dict())


def _load_handoff_basis(tio: Any, slug: str) -> Any:
    runtime_ticket = ticket_log_dir(tio.logs_dir, slug) / "ticket.md"
    return tio._load_basis_unlocked(slug, runtime_ticket_path=runtime_ticket)


def op_handoff(
    tio: Any,
    slug: str,
    *,
    expected_execution_id: str | None = None,
) -> bool:
    """Hand off ticket after development: route by on_success.destination.

    destination=review (default): move to review/, notify.
    destination=done: call op_complete() directly (skip review).

    Requires logs/<slug>/human-logs/run.log to exist. Returns False if missing.
    Also validates that transitions.log has entries for all completed steps
    (prevents governance bypass via direct YAML editing).
    """
    run_log = existing_human_log_file(tio.logs_dir, slug, "run.log")
    if not run_log.exists():
        print("ERROR: run.log not found -- developer log missing.", file=sys.stderr)
        return False

    entry, old_status, old_step = _get_old_state(tio, slug, "summary")

    if not _validate_transitions_for_handoff(tio, slug, entry):
        return False
    from booley.core.models import OnSuccess

    on_success = OnSuccess.from_dict(entry.get("on_success") if entry else None)

    if on_success.destination == "done":
        ok = _op_move_and_log(
            tio,
            slug,
            "review",
            {"step": "summary"},
            (
                f"{old_status}:{old_step}",
                "review:summary",
                "ticket-execute",
                "ready for completion (destination=done)",
            ),
            append_step="summary",
            expected_status="running" if expected_execution_id is not None else None,
            expected_execution_id=expected_execution_id,
            before_move=lambda: _prepare_handoff_snapshot(tio, slug, entry, expected_execution_id),
        )
        return ok and op_complete(tio, slug)

    return _handoff_to_review(
        tio,
        slug,
        entry,
        old_status,
        old_step,
        expected_execution_id,
    )


def op_unblock(
    tio: Any,
    slug: str,
    feedback: str = "",
    *,
    actor: str = "ticket-triage",
    detail: str = "user answered questions",
    feedback_heading: str = "Human Response",
) -> bool:
    """Unblock a ticket: move blocked->queue, clear blocked fields, log.

    *actor*/*detail* land in transitions.log and default to human triage.
    The harness auto-retry path overrides them so a machine requeue is
    distinguishable from a human one in the board history.
    """
    entry = tio.find_ticket(slug)
    if not entry:
        print(f"Error: ticket '{slug}' not found", file=sys.stderr)
        return False
    status = entry.get("status", "")
    if status != "blocked":
        print(f"Error: ticket '{slug}' is {status}, not blocked", file=sys.stderr)
        return False
    step = entry.get("blocked_step", "")

    # Note: do NOT delete ticket.lock before the locked operation — on Unix,
    # unlinking a held lock file breaks mutual exclusion (new inode); on
    # Windows it raises PermissionError. move_and_update holds its own lock.

    ok = _op_move_and_log(
        tio,
        slug,
        "queue",
        {
            "workspace_intent": "resume",
            "blocked_reason": None,
            "blocked_step": None,
            "error": None,
            "failed_step": None,
        },
        (f"blocked:{step}", f"queued:{step}", actor, detail),
    )
    if ok and feedback:
        _append_feedback(tio, slug, feedback, heading=feedback_heading)
    elif ok:
        _append_unblock_marker(tio, slug)
    return ok


def _append_feedback(tio, slug, message, heading: str = "Human Response"):
    """Append a feedback entry to logs/<slug>/blocked.md.

    *heading* defaults to the human-triage wording; machine requeues pass
    their own so an auto-generated note is never mistaken for an
    authoritative operator directive.

    Acquires per-ticket lock to prevent the TOCTOU race between the
    existence check and the create/append write.
    """
    from datetime import datetime

    with tio._ticket_lock(slug):
        blocked_path = Path(tio.logs_dir) / slug / "blocked.md"
        blocked_path.parent.mkdir(parents=True, exist_ok=True)
        timestamp = format_human_datetime(datetime.now(UTC), seconds=True)
        entry = f"\n### {heading} ({timestamp})\n\n{message}\n"
        if not blocked_path.exists():
            header = (
                "# Escalation History\n\n"
                "Authoritative guidance from the human operator.\n"
                "Agents MUST honor these directives.\n"
            )
            blocked_path.write_text(header + entry, encoding="utf-8")
        else:
            with blocked_path.open("a", encoding="utf-8") as f:
                f.write(entry)


def _append_unblock_marker(tio, slug):
    """Append a minimal unblock marker to logs/<slug>/blocked.md."""
    from datetime import datetime

    with tio._ticket_lock(slug):
        blocked_path = Path(tio.logs_dir) / slug / "blocked.md"
        if not blocked_path.exists():
            return
        timestamp = format_human_datetime(datetime.now(UTC), seconds=True)
        with blocked_path.open("a", encoding="utf-8") as f:
            f.write(f"\n### Unblocked ({timestamp})\n")


def _approve_transition(
    tio: Any, slug: str, actor: str = "ticket-triage", detail: str = "user approved merge"
) -> bool:
    """Apply only the final board transition after terminal validation."""
    entry = tio.find_ticket(slug)
    if not entry:
        print(f"Error: ticket '{slug}' not found", file=sys.stderr)
        return False
    status = entry.get("status", "")
    if status != "review":
        print(
            f"Error: cannot approve '{slug}' from status '{status}'; must be in review",
            file=sys.stderr,
        )
        return False
    return _op_move_and_log(
        tio, slug, "done", {"step": "complete"}, ("review:summary", "done:complete", actor, detail)
    )


def op_approve(tio: Any, slug: str) -> bool:
    """Complete a review Ticket through the validated terminal boundary."""
    return op_complete(tio, slug)


def op_promote_waiting(tio: Any) -> list[dict[str, str]]:
    """Move waiting tickets to queue/ if all their dependencies are now done.

    Returns list of promoted ticket dicts: [{"slug": ..., "summary": ...}].
    """
    tickets = scan_all_tickets(tio.tickets_dir)

    done_slugs = compute_done_slugs(tickets)

    # Find waiting tickets whose deps are all satisfied
    promoted = []
    for t in tickets:
        if t.get("status") != "waiting":
            continue
        deps = t.get("dependencies", [])
        if deps and not all(d in done_slugs for d in deps):
            continue
        slug = t.get("feature_branch") or slug_from_file(t.get("file", ""))
        summary = t.get("summary", slug)
        # Move waiting/ → queue/
        ok = _op_move_and_log(
            tio,
            slug,
            "queue",
            {},
            (
                "waiting:init",
                "queued:init",
                "ticket-board",
                "dependencies satisfied — promoted to queue",
            ),
        )
        if ok:
            promoted.append({"slug": slug, "summary": summary})

    return promoted


def _effective_on_success(entry: dict, *, no_merge: bool, no_cleanup: bool) -> OnSuccess:
    """The ticket's ``on_success`` with the per-invocation overrides applied.

    The overrides are subtractive only: they can turn a configured action OFF,
    never on. Every downstream decision must read the effective values.
    Destructive cleanup is valid only with journaled merge publication, so
    ``--no-merge`` must be paired with ``--no-cleanup`` when cleanup was
    configured.
    """
    from dataclasses import replace

    from booley.core.models import OnSuccess

    configured = OnSuccess.from_dict(entry.get("on_success"))
    return replace(
        configured,
        merge=configured.merge and not no_merge,
        cleanup=configured.cleanup and not no_cleanup,
    )


def _acceptance_failure_detail(tio: Any, slug: str) -> str:
    try:
        current = tio.find_ticket(slug)
    except (OSError, ValueError):
        current = None
    if current is not None and current.get("status") == "review":
        return "ticket stays in review"
    return "inspect the Ticket and Acceptance Journal before retrying"


def _validate_accepted_snapshot(tio: Any, slug: str, log_dir: Path, snapshot: Any) -> None:
    from .acceptance_basis import (
        assert_live_inputs_unchanged,
        load_basis_receipt,
        materialize_ticket_commits,
        validate_current_basis_refs,
    )
    from .acceptance_journal import completion_basis_sources
    from .acceptance_ledger import AcceptanceLedgerError, validate_review_package_binding

    validate_review_package_binding(log_dir, snapshot)
    basis = tio.load_basis(slug)
    current_receipt = load_basis_receipt(tio._project_root, slug, basis.as_dict())
    if snapshot.acceptance_basis != current_receipt:
        raise AcceptanceLedgerError("Acceptance Snapshot names a different Board Acceptance Basis")
    with tempfile.TemporaryDirectory(prefix="booley-completion-basis-") as directory:
        snapshot_sources = snapshot.participant_heads
        sources = completion_basis_sources(
            Path(tio._project_root),
            slug,
            basis,
            expected_sources=snapshot_sources,
        )
        if sources is None:
            current_sources = validate_current_basis_refs(tio._project_root, basis)
            if current_sources != snapshot_sources:
                raise AcceptanceLedgerError(
                    "Ticket heads changed after the accepted snapshot was frozen"
                )
            sources = snapshot_sources
        authoring = materialize_ticket_commits(
            tio._project_root,
            basis,
            Path(directory) / "checkout",
            sources,
        )
        selector_errors = _prepare_materialized_basis_view(tio, slug, authoring, basis)
        assert_live_inputs_unchanged(basis, tio._project_root, authoring)
    if selector_errors:
        raise AcceptanceLedgerError(
            "Acceptance Basis selectors changed: " + "; ".join(selector_errors)
        )


def _completion_acceptance_valid(tio: Any, slug: str) -> AcceptanceSnapshot | None:
    """Refuse destructive terminal actions when durable acceptance is broken."""
    from .acceptance_ledger import AcceptanceLedgerError, read_acceptance

    log_dir = ticket_log_dir(tio.logs_dir, slug)
    accepted = read_acceptance(log_dir)
    if accepted.kind == "accepted":
        if accepted.snapshot is None:
            print(f"Error: accepted snapshot for '{slug}' is unreadable", file=sys.stderr)
            return None
        try:
            _validate_accepted_snapshot(tio, slug, log_dir, accepted.snapshot)
        except (AcceptanceLedgerError, ValueError, OSError) as exc:
            print(
                f"Error: review package binding for '{slug}' is corrupt: {exc}",
                file=sys.stderr,
            )
            return None
        return accepted.snapshot
    if accepted.kind == "corrupt":
        print(
            f"Error: accepted snapshot for '{slug}' is corrupt: {accepted.reason}",
            file=sys.stderr,
        )
        return None
    print(f"Error: accepted snapshot for '{slug}' is unavailable", file=sys.stderr)
    return None


def op_complete(  # noqa: PLR0911 - ordered validation and terminal-action paths
    tio: Any,
    slug: str,
    *,
    no_merge: bool = False,
    no_cleanup: bool = False,
) -> bool:
    """Complete a ticket: approve, merge/cleanup based on on_success.

    *no_merge* / *no_cleanup* are per-invocation opt-outs of the ticket's
    configured terminal actions (``booley board move <slug> done
    --no-merge``) — useful when a ticket's branch must survive for inspection,
    or when the merge is being done by hand. They never enable an action the
    ticket did not ask for.

    Returns True on success, False on failure.
    """
    entry = tio.find_ticket(slug)
    if not entry:
        print(f"Error: ticket '{slug}' not found", file=sys.stderr)
        return False
    # ``feature_branch`` is an accepted lookup alias, but all runtime paths and
    # paired repository branches are keyed by the ticket filename stem.
    slug = Path(str(entry["file"])).stem

    on_success = _effective_on_success(entry, no_merge=no_merge, no_cleanup=no_cleanup)
    if on_success.remove_targets and not on_success.merge:
        print(
            f"Error: cannot remove Targets when merge is disabled for '{slug}'",
            file=sys.stderr,
        )
        return False
    policy_errors = on_success.validate()
    if policy_errors:
        print(f"Error: cannot complete '{slug}': {policy_errors[0]}", file=sys.stderr)
        return False

    status = entry.get("status", "")
    if status != "review" and not (status == "done" and on_success.merge):
        print(
            f"Error: cannot complete '{slug}' from status '{status}'; must be in review",
            file=sys.stderr,
        )
        return False
    accepted_snapshot = _completion_acceptance_valid(tio, slug)
    if accepted_snapshot is None:
        return False

    from .acceptance_basis import AcceptanceBasis, AcceptanceBasisError

    try:
        AcceptanceBasis.from_mapping(entry.get("acceptance_basis"))
    except AcceptanceBasisError as exc:
        print(f"Error: cannot complete '{slug}': {exc}", file=sys.stderr)
        return False

    if on_success.merge:
        from .acceptance_journal import cleanup_finished
        from .completion import complete_review_ticket

        if not complete_review_ticket(
            tio,
            slug,
            on_success,
            expected_sources=accepted_snapshot.participant_heads,
        ):
            detail = _acceptance_failure_detail(tio, slug)
            print(f"Error: acceptance failed for '{slug}'; {detail}", file=sys.stderr)
            return False
        finished_cleanup = on_success.cleanup and cleanup_finished(
            Path(tio._project_root).resolve(), slug
        )
        _finish_completed_ticket(tio, slug, cleanup=finished_cleanup)
        return True

    if not _approve_transition(tio, slug, actor="op-complete", detail="terminal actions"):
        return False

    _finish_completed_ticket(tio, slug, cleanup=False)
    return True


def _finish_completed_ticket(tio: Any, slug: str, *, cleanup: bool) -> None:
    """Release ephemeral execution state after durable acceptance."""
    if cleanup:
        from booley.harness.setup.worktree_lock_gc import release_worktree_locks

        release_worktree_locks(tio._project_root, slug)

    from .archive import _cleanup_session_files

    _cleanup_session_files(tio.logs_dir / slug)
    op_promote_waiting(tio)


def op_board_move(
    tio: Any,
    slug: str,
    target: str,
    feedback: str = "",
    no_merge: bool = False,
    no_cleanup: bool = False,
) -> bool:
    """User-facing state transition, validated against the lifecycle graph.

    The legal user moves (draft->queue, blocked->queue, review->done,
    running->queue) come from ``lifecycle.USER_BOARD_MOVES`` — the enforced
    matrix and the help text below are both derived from it, so they cannot
    drift apart. Returns True on success, False on invalid transition or error.

    *no_merge* / *no_cleanup* opt out of the ticket's configured terminal
    actions. Only the review->done edge runs any, so they are announced as
    ignored on the others rather than silently dropped.
    """
    entry = tio.find_ticket(slug)
    if not entry:
        print(f"Error: ticket '{slug}' not found", file=sys.stderr)
        return False

    status = entry.get("status", "")
    src = STATE_BY_STATUS.get(status)
    dst = STATE_BY_DIR.get(target)

    if src is None or dst is None or not is_user_board_move(src, dst):
        print(f"Error: cannot move '{slug}' from '{status}' to '{target}'", file=sys.stderr)
        print(f"Valid transitions: {format_user_board_moves()}", file=sys.stderr)
        return False

    # Route the validated move to its operation (handlers differ per edge).
    if src is TicketState.REVIEW:
        return op_complete(tio, slug, no_merge=no_merge, no_cleanup=no_cleanup)
    if no_merge or no_cleanup:
        print(
            f"Note: --no-merge/--no-cleanup apply to the review->done move only; "
            f"ignored for '{status}' -> '{target}'",
            file=sys.stderr,
        )
    if src is TicketState.DRAFT:
        return tio.enqueue_ticket(slug)
    if src is TicketState.BLOCKED:
        return op_unblock(tio, slug, feedback=feedback)
    return op_requeue(tio, slug, reason="user requeued")  # RUNNING -> QUEUED


def _archive_run_artifacts(log_dir: Path, preserved: set[str]) -> None:
    """Move ephemeral run artifacts into runs/<NNN>/ for post-mortem analysis.

    Only archives if there's meaningful content beyond the preserved files.
    The runs/ directory itself is preserved across resets.
    """
    runtime_dir = log_dir / ".runtime"
    runtime_entries = (
        [entry for entry in runtime_dir.iterdir() if entry.name != "ticket.lock"]
        if runtime_dir.is_dir()
        else []
    )
    archivable = [p for p in log_dir.iterdir() if p.name not in preserved and p != runtime_dir]
    if runtime_entries:
        archivable.append(runtime_dir)
    if not archivable:
        return

    runs_dir = log_dir / "runs"
    runs_dir.mkdir(exist_ok=True)
    existing = sorted(
        (d for d in runs_dir.iterdir() if d.is_dir() and d.name.isdigit()),
        key=lambda d: int(d.name),
    )
    next_index = int(existing[-1].name) + 1 if existing else 1
    archive_dir = runs_dir / f"{next_index:03d}"
    archive_dir.mkdir()

    for entry in archivable:
        try:
            if entry == runtime_dir:
                archived_runtime = archive_dir / entry.name
                archived_runtime.mkdir()
                for runtime_entry in runtime_entries:
                    shutil.move(str(runtime_entry), str(archived_runtime / runtime_entry.name))
            else:
                shutil.move(str(entry), str(archive_dir / entry.name))
        except OSError:
            logger.warning("Failed to archive %s", entry)


def _wipe_log_dir(tio, slug, preserved):
    """Archive run artifacts and wipe non-preserved files from log dir.

    Backs up transitions.log before wipe for crash recovery.
    """
    log_dir = ticket_log_dir(tio.logs_dir, slug)
    if not log_dir.exists():
        return

    transitions_log = ticket_human_log_file(log_dir, "transitions.log")
    bak_dir = tio.logs_dir / f"{slug}.bak"
    transitions_backup = None

    # Backup transitions.log for crash recovery
    if transitions_log.exists():
        transitions_backup = transitions_log.read_text(encoding="utf-8")
        bak_dir.mkdir(parents=True, exist_ok=True)
        (bak_dir / "transitions.log").write_text(transitions_backup, encoding="utf-8")

    _archive_run_artifacts(log_dir, preserved)

    try:
        for entry_path in log_dir.iterdir():
            if entry_path.name in preserved:
                continue
            if entry_path.is_dir():
                shutil.rmtree(str(entry_path))
            else:
                entry_path.unlink()
    except BaseException:
        # Restore transitions.log if wipe crashed partway through
        if transitions_backup is not None and not transitions_log.exists():
            log_dir.mkdir(parents=True, exist_ok=True)
            transitions_log.parent.mkdir(parents=True, exist_ok=True)
            transitions_log.write_text(transitions_backup, encoding="utf-8")
        raise
    else:
        if bak_dir.exists():
            shutil.rmtree(str(bak_dir), ignore_errors=True)


def _move_to_queue(tio, file_path):
    """Move a ticket file to the queue/ directory. Returns False on conflict."""
    from .constants import normalize_dir

    new_dir = tio.tickets_dir / normalize_dir("queue")
    new_dir.mkdir(parents=True, exist_ok=True)
    new_path = new_dir / file_path.name
    if new_path.exists() and new_path != file_path:
        print(f"Error: destination already exists: {new_path}", file=sys.stderr)
        return False
    if file_path.exists() and file_path != new_path:
        shutil.move(str(file_path), str(new_path))
    return True


def _queue_destination_available(tio: Any, file_path: Path) -> bool:
    """Refuse a reset before destructive work if queue/ already has this ticket."""
    from .constants import normalize_dir

    new_path = tio.tickets_dir / normalize_dir("queue") / file_path.name
    if new_path.exists() and new_path != file_path:
        print(f"Error: destination already exists: {new_path}", file=sys.stderr)
        return False
    return True


def _append_reset_boundary(logs_dir: Path, slug: str) -> None:
    """Mark earlier append-only escalation entries as inactive history."""
    blocked_path = Path(logs_dir) / slug / "blocked.md"
    if not blocked_path.exists():
        return
    timestamp = format_human_datetime(datetime.now(UTC), seconds=True)
    with blocked_path.open("a", encoding="utf-8") as file:
        file.write(
            f"\n{RESET_BOUNDARY_PREFIX}{timestamp})\n\n"
            "Earlier escalation entries are archived history and do not apply "
            "to the new run.\n"
        )


def _live_owner_pid(tio: Any, slug: str) -> int | None:
    """PID of a *running* process that currently owns *slug*, if any."""
    from .helpers import read_lock_pid

    lock_path = existing_runtime_file(tio.tickets_dir / "logs", slug, "ticket.lock")
    pid = read_lock_pid(lock_path)
    caller_owner_pid = str(tio._resolve_developer_pid())
    if pid is None or str(pid) == caller_owner_pid or not is_pid_alive(pid):
        return None
    return pid


def _reset_owner_available(tio: Any, slug: str, force: bool) -> bool:
    """Report a live owner and refuse reset unless the caller forced it."""
    live_pid = _live_owner_pid(tio, slug)
    if live_pid is None or force:
        return True
    print(
        f"Error: ticket '{slug}' is owned by a live process (PID {live_pid}).\n"
        f"  Resetting now would queue the ticket for that same run to pick up "
        f"again, and wipe its runtime state mid-flight.\n"
        f"  Stop it first:  kill {live_pid}    then re-run this reset.\n"
        f"  To reset anyway (the PID is stale or you have already stopped it): "
        f"--force",
        file=sys.stderr,
    )
    return False


def _reset_jobs_inactive(tio: Any, slug: str) -> bool:
    """Refuse to archive runtime state while a detached endpoint owns it."""
    from booley.harness.job_fence import active_ticket_jobs

    active = active_ticket_jobs(ticket_log_dir(tio.logs_dir, slug))
    if not active:
        return True
    jobs = ", ".join(f"{record.endpoint} ({record.run_id})" for record in active)
    print(
        f"Error: ticket '{slug}' still has active endpoint jobs: {jobs}.\n"
        "  Wait for them to finish or cancel them before resetting; --force "
        "does not override active job leases.",
        file=sys.stderr,
    )
    return False


def _reset_runtime_state(tio: Any, slug: str) -> None:
    """Archive active run state and establish an empty current runtime."""
    log_dir = ticket_log_dir(tio.logs_dir, slug)
    transitions_path = ticket_human_log_file(log_dir, "transitions.log")
    transition_history = (
        transitions_path.read_text(encoding="utf-8") if transitions_path.exists() else ""
    )

    # Keep human history, but move every active runtime artifact out of the
    # paths consumed by the board and the next developer run.
    # The ticket lock is held inside .runtime while reset runs. Windows cannot
    # rename or delete an open file, so preserve that directory and archive
    # every other runtime entry around the live lock.
    _wipe_log_dir(tio, slug, {"ticket.md", "runs", "blocked.md", ".runtime"})
    reset_progress(tio.logs_dir, slug)
    _append_reset_boundary(tio.logs_dir, slug)
    if transition_history:
        transitions_path.parent.mkdir(parents=True, exist_ok=True)
        transitions_path.write_text(transition_history, encoding="utf-8")


def _locked_reset_candidate(tio: Any, slug: str) -> Path | None:
    """Resolve a resettable ticket after the caller acquires its lock."""
    from .io import find_ticket_file

    if not _reset_jobs_inactive(tio, slug):
        return None
    file_path, _ = find_ticket_file(tio.tickets_dir, slug)
    if file_path is None:
        print(f"Error: ticket '{slug}' not found after lock", file=sys.stderr)
        return None
    return file_path if _queue_destination_available(tio, file_path) else None


def _validated_reset_context(
    tio: Any, slug: str
) -> tuple[Path, dict[str, Any], Any | None] | None:
    """Reload the reset target and its authoritative basis while locked."""
    file_path = _locked_reset_candidate(tio, slug)
    if file_path is None:
        return None
    current = tio.find_ticket(slug)
    if current is None:
        print(f"Error: ticket '{slug}' not found after lock", file=sys.stderr)
        return None
    if current.get("acceptance_basis") is None:
        return file_path, current, None
    from .acceptance_basis import AcceptanceBasisError

    try:
        basis = tio._load_basis_unlocked(slug)
    except (AcceptanceBasisError, OSError, ValueError) as exc:
        print(
            f"Error: reset could not validate the Acceptance Basis for '{slug}': {exc}",
            file=sys.stderr,
        )
        return None
    return file_path, current, basis


def _perform_reset(tio: Any, slug: str, reason: str) -> bool:
    """Reset under the ticket lock, publishing queue state only at the end."""
    with tio._ticket_lock(slug):
        context = _validated_reset_context(tio, slug)
        if context is None:
            return False
        file_path, current, basis = context

        reset_plan = _preflight_reset_branches(tio._project_root, slug, current, basis)
        if basis is not None and reset_plan is None:
            return False

        try:
            _reset_runtime_state(tio, slug)
        except OSError as exc:
            print(
                f"Error: reset cleanup failed for '{slug}': {exc}. Ticket was not moved to queue.",
                file=sys.stderr,
            )
            return False
        if not _reset_ticket_branches(
            tio._project_root,
            slug,
            current,
            basis,
            reset_plan=reset_plan,
        ):
            return False

        if not _move_to_queue(tio, file_path):
            return False

        tio._append_transition_unlocked(
            slug,
            f"{current.get('status', 'unknown')}:{current.get('step', '')}",
            "queued:reset",
            "ticket-triage",
            reason,
        )

    return True


def _cleanup_reset_branches(project_root: Path, slug: str, feature_branch: str) -> bool:
    """Discard both repositories' ticket branches during a full reset."""
    ok, _detail = TicketWorkspace.retire(
        project_root,
        slug,
        WorkspaceDisposition.DISCARD,
    )
    if not ok:
        print(
            f"Error: reset could not delete project repository branch for '{slug}'.",
            file=sys.stderr,
        )
        return False
    if feature_branch and not cleanup_worktree_and_branch(feature_branch, force=True):
        print(
            f"Error: reset could not delete feature branch '{feature_branch}'.",
            file=sys.stderr,
        )
        return False
    return True


def _reset_ticket_branches(
    project_root: Path,
    slug: str,
    entry: dict[str, Any],
    basis: Any | None,
    *,
    reset_plan: Any | None = None,
) -> bool:
    """Restore an Acceptance Basis generation or remove draft branches."""
    raw_basis = entry.get("acceptance_basis")
    if raw_basis is None:
        return _cleanup_reset_branches(project_root, slug, entry.get("feature_branch", ""))
    from .acceptance_basis import AcceptanceBasisError
    from .workspace_ops import AcceptanceBasisOperationError, reset_basis_worktrees

    try:
        if basis is None:
            raise AcceptanceBasisError("authoritative Acceptance Basis is unavailable")
        reset_basis_worktrees(
            project_root,
            slug,
            basis,
            str(entry.get("branch", "")),
            plan=reset_plan,
        )
    except (AcceptanceBasisOperationError, AcceptanceBasisError, OSError) as exc:
        print(
            f"Error: reset could not restore the Acceptance Basis for '{slug}': {exc}",
            file=sys.stderr,
        )
        return False
    return True


def _preflight_reset_branches(
    project_root: Path,
    slug: str,
    entry: dict[str, Any],
    basis: Any | None,
) -> Any | None:
    """Resolve every Acceptance Basis identity before runtime cleanup begins."""
    if entry.get("acceptance_basis") is None:
        return None
    from .acceptance_basis import AcceptanceBasisError
    from .workspace_ops import AcceptanceBasisOperationError, preflight_basis_reset

    try:
        if basis is None:
            raise AcceptanceBasisError("authoritative Acceptance Basis is unavailable")
        return preflight_basis_reset(
            project_root,
            slug,
            basis,
            str(entry.get("branch", "")),
        )
    except (AcceptanceBasisOperationError, AcceptanceBasisError, OSError) as exc:
        print(
            f"Error: reset could not preflight the Acceptance Basis for '{slug}': {exc}",
            file=sys.stderr,
        )
        return None


def op_reset(
    tio: Any,
    slug: str,
    force: bool = False,
    reason: str = "user reset ticket",
) -> bool:
    """Reset a ticket's state and artifacts, then move it to queue/.

    The queue move is the final publication step: a queued ticket therefore
    never advertises stale active-run evidence, even if cleanup fails.
    """
    entry = tio.find_ticket(slug)
    if not entry:
        print(f"Error: ticket '{slug}' not found", file=sys.stderr)
        return False
    project_root = Path(getattr(tio, "_project_root", ""))
    if entry.get("acceptance_basis") is None and (project_root / ".git").exists():
        print(
            f"Error: ticket '{slug}' has no Acceptance Basis; return it to draft and "
            "enqueue a new generation.",
            file=sys.stderr,
        )
        return False

    # ``find_ticket`` accepts both the canonical slug and user-facing aliases
    # such as a copied ``<slug>.md`` filename or feature branch. Runtime paths,
    # however, are always keyed by the ticket file's stem. Resolve that stem
    # before touching locks or logs so reset cannot silently wipe a sibling
    # alias directory while leaving the real run state intact.
    canonical_slug = Path(entry["file"]).stem
    if not _reset_owner_available(tio, canonical_slug, force):
        return False
    return _perform_reset(tio, canonical_slug, reason)

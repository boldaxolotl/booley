"""Explicit, recoverable entry to human review without accepting unfinished work."""

from __future__ import annotations

import os
import time
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any, Literal

from booley.core.boundary import require_dict
from booley.criteria.state import DevelopmentState
from booley.runtime.job_records import JobRecord
from booley.runtime.pid import is_pid_alive
from booley.runtime.timefmt import utc_now_rfc3339
from booley.ticket_board.acceptance_ledger import (
    bind_review_package,
    freeze_acceptance,
    read_acceptance,
)
from booley.ticket_board.helpers import tickets_dir_from_project_root
from booley.ticket_board.io import TicketIO
from booley.ticket_board.logs import load_progress
from booley.ticket_board.persistence import atomic_replace_bytes
from booley.ticket_board.ticket_jobs import active_ticket_jobs, wait_for_ticket_jobs

from . import review_preparation as prep
from .review_records import (
    ReviewEntryError,
    ReviewInspection,
    assert_idle,
    digest,
    entry_path,
    operation_path,
    package_dir,
    parse_inspection,
    read_entry,
    read_json,
    require_clean,
)

ReviewPrepError = prep.ReviewPrepError
ReviewPrepOutcome = prep.ReviewPrepOutcome
ReviewBriefingOutcome = prep.ReviewBriefingOutcome


def active_review_jobs(log_dir: Path) -> list[JobRecord]:
    """Return jobs that must finish before review admission or handoff."""
    return active_ticket_jobs(log_dir)


async def wait_for_review_jobs(log_dir: Path) -> list[JobRecord]:
    """Wait at the Ticket Board lifecycle boundary for review-blocking jobs."""
    return await wait_for_ticket_jobs(log_dir)


def _write(path: Path, value: dict[str, Any]) -> None:
    import json

    atomic_replace_bytes(path, (json.dumps(value, sort_keys=True) + "\n").encode())


def _quiescent(tio: TicketIO, slug: str) -> None:
    progress = load_progress(tio.logs_dir, slug) or {}
    owner = progress.get("execution_owner_pid")
    if isinstance(owner, int) and owner != os.getpid() and is_pid_alive(owner):
        raise ReviewEntryError("ticket execution owner is still running")
    if active_ticket_jobs(tio.logs_dir / slug):
        raise ReviewEntryError("ticket Jobs are active; wait or cancel them before review")


def _capture_state(ctx: prep.ReviewPrepContext) -> dict[str, Any]:
    tio = TicketIO(tickets_dir_from_project_root(ctx.project_root), project_root=ctx.project_root)
    document = tio.load_document(ctx.slug, runtime_ticket_path=ctx.ticket_path)
    state = read_json(ctx.log_dir / ".runtime" / "booley_state.json") or {}
    criteria = dict(require_dict(state.get("criteria", {}), field="criteria"))
    expected = {row.identity: row.mandatory for row in document.spec.criteria}
    from booley.config.project_config import is_run_report_enabled

    if is_run_report_enabled():
        expected["_report_submitted"] = True
    for key, mandatory in expected.items():
        if key in criteria:
            row = dict(require_dict(criteria[key], field=f"criterion {key}"))
            row["mandatory"] = mandatory
            criteria[key] = row
        else:
            criteria[key] = {"met": False, "mandatory": mandatory, "availability": "unavailable"}
    for key, value in criteria.items():
        require_dict(value, field=f"criterion {key}")
    return {**state, "criteria": criteria}


def _capture(
    tio: TicketIO, slug: str, reason: str, disposition: Literal["unaccepted", "accepted"]
) -> prep.ReviewPrepContext:
    ctx = prep._resolve_context(
        tio._project_root,
        slug,
        require_review=True,
        allow_report_disabled=True,
        inspect_unaccepted=True,
    )
    require_clean(ctx)
    basis = tio.load_basis(slug)
    board = tio.find_ticket(slug)
    assert board is not None
    heads = {"outer": ctx.head_sha}
    if ctx.project_repository is not None:
        heads["project"] = ctx.project_repository.head_sha
    row: ReviewInspection = {
        "schema": 1,
        "generation": uuid.uuid4().hex,
        "ticket_generation": basis.ticket_identity()["generation"],
        "ticket_identity": basis.ticket_identity(),
        "execution_id": str(board.get("execution_id", "")),
        "source_status": board["status"],
        "heads": heads,
        "disposition": disposition,
        "reason": reason,
        "blocked_reason": str(board.get("blocked_reason") or ""),
        "state": {},
        "capture_sha": "",
        "created_at": utc_now_rfc3339(),
    }
    captured = replace(ctx, inspection=row, runtime_dir=package_dir(ctx.log_dir, row))
    row["capture_sha"] = prep._source_fingerprint(captured)
    row["state"] = _capture_state(ctx)
    prep._require_unchanged(captured, row["capture_sha"], "review inputs changed during capture")
    return captured


def _validate_action(tio: TicketIO, slug: str, action: str, repair: bool) -> None:
    board = tio.find_ticket(slug)
    assert board is not None
    prior = read_entry(tio.logs_dir / slug)
    accepted = read_acceptance(tio.logs_dir / slug)
    if accepted.kind == "corrupt":
        raise ReviewEntryError(f"Criteria Satisfaction Record is corrupt: {accepted.reason}")
    if accepted.kind == "accepted" and action != "regenerate":
        raise ReviewEntryError(f"already accepted; run booley board approve {slug} to complete it")
    if action == "request":
        if board["status"] != "blocked" and not (repair and board["status"] == "review"):
            raise ReviewEntryError(
                "board review --request requires blocked; use --repair for stranded review"
            )
    elif board["status"] != "review" or prior is None:
        raise ReviewEntryError("refresh/finalize requires an explicitly requested review")
    if action == "regenerate" and prior is None:
        raise ReviewEntryError("no inspection to regenerate")


def _acceptance_ready(tio: TicketIO, ctx: prep.ReviewPrepContext) -> None:
    from booley.ticket_board.criteria_acceptance import check_criteria_acceptance
    from booley.ticket_board.operations import _handoff_basis_heads

    assert ctx.inspection is not None
    raw = read_json(ctx.log_dir / ".runtime" / "booley_state.json") or {}
    actual = require_dict(raw.get("criteria", {}), field="criteria")
    for key, expected in ctx.inspection["state"]["criteria"].items():
        if expected.get("mandatory"):
            observed = require_dict(actual.get(key, {}), field=f"criterion {key}")
            if observed.get("mandatory") is not True or observed.get("met") is not True:
                raise ReviewEntryError(
                    f"mandatory criterion {key!r} is missing, downgraded or unmet"
                )
    verdict = check_criteria_acceptance(
        ctx.log_dir / ".runtime" / "booley_state.json",
        work_dir=ctx.worktree,
    )
    if verdict.disposition != "review":
        raise ReviewEntryError(f"acceptance is {verdict.disposition}: {verdict}")
    if _handoff_basis_heads(tio, ctx.slug) != ctx.inspection["heads"]:
        raise ReviewEntryError("Ticket baseline or participant heads failed final validation")


def _claim(tio: TicketIO, slug: str, action: str, reason: str, repair: bool) -> dict[str, Any]:
    with tio._ticket_lock(slug, review_operation=True):
        assert_idle(tio.logs_dir / slug)
        _quiescent(tio, slug)
        _validate_action(tio, slug, action, repair)
        previous = read_entry(tio.logs_dir / slug)
        reason = reason.strip() or (previous or {}).get("reason", "")
        if not reason:
            raise ReviewEntryError("a nonblank review reason is required")
        operation = {
            "pid": os.getpid(),
            "phase": "preparing",
            "action": action,
            "reason": reason,
            "repair": repair,
            "previous_entry": previous,
        }
        _write(operation_path(tio.logs_dir / slug), operation)
        return operation


def _check_capture(tio: TicketIO, ctx: prep.ReviewPrepContext, expected: str) -> None:
    assert ctx.inspection is not None
    _quiescent(tio, ctx.slug)
    board = tio.find_ticket(ctx.slug)
    if board is None or str(board.get("execution_id", "")) != ctx.inspection["execution_id"]:
        raise ReviewEntryError("ticket execution generation changed during review")
    basis = tio._load_basis_unlocked(ctx.slug)
    if basis.ticket_identity()["generation"] != ctx.ticket_generation:
        raise ReviewEntryError("Ticket generation changed during review")
    current = prep._resolve_context(
        ctx.project_root,
        ctx.slug,
        require_review=True,
        allow_report_disabled=True,
        inspect_unaccepted=True,
        locked_basis=basis,
    )
    if current.head_sha != ctx.head_sha or current.project_repository != ctx.project_repository:
        raise ReviewEntryError("review heads changed during generation")
    prep._require_unchanged(ctx, expected, "review inputs changed during generation")


def _publish_acceptance(ctx: prep.ReviewPrepContext, operation: dict[str, Any]) -> None:
    row = ctx.inspection
    assert row is not None
    accepted = read_acceptance(ctx.log_dir)
    if accepted.kind == "accepted":
        snapshot = accepted.snapshot
        if snapshot is None or snapshot.participant_heads != row["heads"]:
            raise ReviewEntryError("interrupted acceptance names different heads")
    else:
        snapshot = freeze_acceptance(
            ctx.log_dir,
            DevelopmentState.load(ctx.log_dir / ".runtime" / "booley_state.json"),
            execution_id=row["execution_id"],
            ticket_identity=row["ticket_identity"],
            participant_heads=row["heads"],
            accepted_at=operation["accepted_at"],
        )
    manifest = ctx.runtime_dir / "manifest.json"
    atomic_replace_bytes(
        ctx.log_dir / ".runtime" / "triage-prep" / "manifest.json", manifest.read_bytes()
    )
    bind_review_package(
        ctx.log_dir, snapshot, replace_existing=operation["action"] == "regenerate"
    )


def _commit(tio: TicketIO, ctx: prep.ReviewPrepContext, operation: dict[str, Any]) -> None:
    row = ctx.inspection
    assert row is not None
    if row["disposition"] == "accepted" or operation["action"] == "approve":
        _publish_acceptance(ctx, operation)
    _write(entry_path(ctx.log_dir), {"entry": row, "sha256": digest(row)})
    board = tio.find_ticket(ctx.slug)
    if board is None or board["status"] not in {"blocked", "review"}:
        raise ReviewEntryError("ticket moved during review publication")
    if board["status"] == "blocked" and not tio._move_unaccepted_review_locked(
        ctx.slug,
        expected_status="blocked",
        expected_execution_id=row["execution_id"],
    ):
        raise ReviewEntryError("ticket changed during review publication")
    # Generation-specific event prevents duplicate log rows on interrupted retry.
    event = f"review-entry {row['generation']}"
    transitions = ctx.log_dir / "human-logs" / "transitions.log"
    if not transitions.exists() or event not in transitions.read_text():
        tio._append_transition_unlocked(
            ctx.slug, row["source_status"], "review", f"{operation['action']}-review", event
        )
    operation_path(ctx.log_dir).unlink(missing_ok=True)


def _recover(tio: TicketIO, slug: str) -> prep.ReviewPrepOutcome | None:
    with tio._ticket_lock(slug, review_operation=True):
        operation = read_json(operation_path(tio.logs_dir / slug))
        if operation is None or operation.get("phase") not in {"publishing", "accepting"}:
            return None
        pid = operation.get("pid")
        if pid != os.getpid() and isinstance(pid, int) and is_pid_alive(pid):
            raise ReviewEntryError("another process is publishing review")
        operation["pid"] = os.getpid()
        _write(operation_path(tio.logs_dir / slug), operation)
        try:
            return _recover_publication(tio, slug, operation)
        except (ReviewEntryError, prep.ReviewPrepError):
            if (
                operation["phase"] != "publishing"
                or read_acceptance(tio.logs_dir / slug).kind != "unavailable"
            ):
                raise
            _abandon_publication(tio, slug, operation)
            return None


def _recover_publication(tio, slug, operation):
    row = parse_inspection(operation["entry"])
    ctx = prep._resolve_context(
        tio._project_root,
        slug,
        allow_report_disabled=True,
        inspect_unaccepted=True,
        locked_basis=tio._load_basis_unlocked(slug),
    )
    ctx = replace(ctx, inspection=row, runtime_dir=package_dir(ctx.log_dir, row))
    _check_capture(tio, ctx, operation["source_sha"])
    manifest = prep._read_manifest(ctx)
    _prompt, prompt_sha = prep._review_prompt(ctx)
    outcome = prep._fresh_outcome(ctx, manifest, prompt_sha, operation["source_sha"])
    if outcome is None:
        raise ReviewEntryError("interrupted package is stale or corrupt")
    _commit(tio, ctx, operation)
    return outcome


def _abandon_publication(tio, slug, operation):
    """Withdraw stale, never-accepted publication without discarding source work."""
    log_dir = tio.logs_dir / slug
    prior = operation.get("previous_entry")
    board = tio.find_ticket(slug)
    if board and board["status"] == "review" and operation["entry"]["source_status"] == "blocked":
        source = tio.tickets_dir / board["file"]
        target = tio.tickets_dir / "board" / "blocked" / source.name
        if target.exists():
            raise ReviewEntryError("blocked recovery destination already exists")
        target.parent.mkdir(parents=True, exist_ok=True)
        source.replace(target)
    if prior is None:
        entry_path(log_dir).unlink(missing_ok=True)
    else:
        _write(entry_path(log_dir), {"entry": prior, "sha256": digest(prior)})
    _write(log_dir / "review" / "last-failure.json", operation)
    operation_path(log_dir).unlink(missing_ok=True)


def _seal_package(ctx: prep.ReviewPrepContext) -> None:
    """Bind every generation artifact, including the materialized diff endpoints."""
    manifest_path = ctx.runtime_dir / "manifest.json"
    manifest = read_json(manifest_path)
    if manifest is None:
        raise ReviewEntryError("prepared manifest is unavailable")
    manifest["inspection_artifacts"] = {
        str(path.relative_to(ctx.runtime_dir)): prep._file_sha256(path)
        for path in sorted(ctx.runtime_dir.rglob("*"))
        if path.is_file() and path != manifest_path
    }
    _write(manifest_path, manifest)


def _generation_disposition(
    tio: TicketIO, slug: str, action: str, prior: dict[str, Any] | None
) -> Literal["unaccepted", "accepted"]:
    """Select the immutable disposition for one preparation generation."""
    if action == "finalize":
        return "accepted"
    if action == "regenerate" and prior is not None:
        return (
            "accepted"
            if read_acceptance(tio.logs_dir / slug).kind == "accepted"
            else prior["disposition"]
        )
    return "unaccepted"


def _validate_regeneration(
    tio: TicketIO,
    slug: str,
    action: str,
    prior: dict[str, Any] | None,
    ctx: prep.ReviewPrepContext,
) -> None:
    """Ensure regeneration keeps the selected review inputs immutable."""
    if action != "regenerate":
        return
    assert ctx.inspection is not None
    if (
        prior is None
        or prior["heads"] != ctx.inspection["heads"]
        or prior["state"] != ctx.inspection["state"]
    ):
        if read_acceptance(tio.logs_dir / slug).kind == "accepted":
            raise ReviewEntryError("accepted review inputs changed; use acceptance recovery")
        raise ReviewEntryError("inspection changed; use board review to select new inputs")


async def _generate(tio: TicketIO, slug: str, operation: dict[str, Any]) -> prep.ReviewPrepOutcome:
    """Generate and publish one selected review package."""
    action = operation["action"]
    prior = read_entry(tio.logs_dir / slug)
    disposition = _generation_disposition(tio, slug, action, prior)
    ctx = _capture(tio, slug, operation["reason"], disposition)
    assert ctx.inspection is not None
    if ctx.triage_report_enabled:
        prep.load_backend_config(tio._project_root)
    _validate_regeneration(tio, slug, action, prior, ctx)
    if action == "finalize":
        _acceptance_ready(tio, ctx)
        ctx = _capture(tio, slug, operation["reason"], disposition)
    prompt, prompt_sha = prep._review_prompt(ctx)
    source_sha = ctx.inspection["capture_sha"]
    outcome = await prep._prepare_resolved_review(
        ctx, prompt, prompt_sha, source_sha, time.monotonic(), force=True
    )
    if not outcome.ready:
        return outcome
    _seal_package(ctx)
    return await _publish_generation(tio, slug, ctx, operation, outcome, disposition, source_sha)


async def _publish_generation(
    tio: TicketIO,
    slug: str,
    ctx: prep.ReviewPrepContext,
    operation: dict[str, Any],
    outcome: prep.ReviewPrepOutcome,
    disposition: Literal["unaccepted", "accepted"],
    source_sha: str,
) -> prep.ReviewPrepOutcome:
    """Publish a generated package after rechecking its live inputs."""
    with tio._ticket_lock(slug, review_operation=True):
        _check_capture(tio, ctx, source_sha)
        operation.update(
            phase="accepting" if disposition == "accepted" else "publishing",
            entry=ctx.inspection,
            source_sha=source_sha,
            accepted_at=utc_now_rfc3339(),
        )
        _write(operation_path(ctx.log_dir), operation)
        _commit(tio, ctx, operation)
    return outcome


async def request_review_command(
    project_root: Path,
    slug: str,
    *,
    reason: str = "",
    repair: bool = False,
    action: str = "request",
) -> prep.ReviewPrepOutcome:
    """Prepare and publish requested review, refresh, repair or first acceptance."""
    if action not in {"request", "refresh", "regenerate", "finalize"}:
        return prep.ReviewPrepOutcome("failed", f"unknown review action: {action}")
    tio = TicketIO(tickets_dir_from_project_root(project_root), project_root=project_root)
    board = tio.find_ticket(slug)
    if board is None:
        return prep.ReviewPrepOutcome("failed", f"ticket {slug!r} not found")
    slug = Path(board["file"]).stem
    try:
        recovered = _recover(tio, slug)
        if recovered is not None:
            return recovered
        prior = read_entry(tio.logs_dir / slug)
        if prior and (
            (action == "request" and prior["disposition"] == "unaccepted")
            or (action == "finalize" and prior["disposition"] == "accepted")
        ):
            return prep.verify_review_handoff(project_root, slug)
        operation = _claim(tio, slug, action, reason, repair)
        return await _generate(tio, slug, operation)
    except Exception as exc:  # noqa: BLE001 — stable public command outcome
        return prep.ReviewPrepOutcome("failed", str(exc))
    finally:
        try:
            pending = read_json(operation_path(tio.logs_dir / slug))
        except (ValueError, OSError):
            pending = None
        if pending and pending.get("pid") == os.getpid() and pending.get("phase") == "preparing":
            operation_path(tio.logs_dir / slug).unlink(missing_ok=True)


def _selected_context(tio: TicketIO, slug: str) -> prep.ReviewPrepContext:
    row = read_entry(tio.logs_dir / slug)
    if row is None:
        raise ReviewEntryError("no selected review; run board review first")
    ctx = prep._resolve_context(
        tio._project_root,
        slug,
        require_review=True,
        allow_report_disabled=True,
        inspect_unaccepted=True,
        locked_basis=tio._load_basis_unlocked(slug),
    )
    return replace(ctx, inspection=row, runtime_dir=package_dir(ctx.log_dir, row))


def _require_selected_package(tio: TicketIO, ctx: prep.ReviewPrepContext) -> None:
    row = ctx.inspection
    assert row is not None
    try:
        _check_capture(tio, ctx, row["capture_sha"])
    except prep.ReviewPrepConcurrentChangeError as exc:
        accepted = read_acceptance(ctx.log_dir)
        guidance = "acceptance recovery" if accepted.kind == "accepted" else "board review"
        raise ReviewEntryError(f"selected review inputs changed; use {guidance}") from exc
    manifest = prep._read_manifest(ctx)
    _prompt, prompt_sha = prep._review_prompt(ctx)
    if prep._fresh_outcome(ctx, manifest, prompt_sha, row["capture_sha"]) is None:
        raise ReviewEntryError("selected package is missing or stale; run board review first")


def _approve_done_ticket(tio: TicketIO, slug: str, *, no_merge: bool, no_cleanup: bool) -> bool:
    """Retry terminal actions for a previously accepted Ticket."""
    from booley.core.models import OnSuccess

    from .operations import _completion_acceptance_valid, op_complete

    if _completion_acceptance_valid(tio, slug) is None:
        return False
    entry = tio.find_ticket(slug)
    assert entry is not None
    policy = OnSuccess.from_dict(entry.get("on_success"))
    if policy.merge and not no_merge:
        return op_complete(tio, slug, no_merge=no_merge, no_cleanup=no_cleanup)
    return True


def _approve_review_ticket(tio: TicketIO, slug: str, *, no_merge: bool, no_cleanup: bool) -> bool:
    """Publish acceptance for a selected review and complete the Ticket."""
    from .operations import _completion_context, op_complete

    completion = _completion_context(tio, slug, no_merge, no_cleanup)
    if completion is None:
        return False
    if not completion[1].merge and tio.load_basis(slug).target_plan is not None:
        raise ReviewEntryError("Target Plan acceptance requires merge")
    _recover(tio, slug)
    with tio._ticket_lock(slug, review_operation=True):
        assert_idle(tio.logs_dir / slug)
        _quiescent(tio, slug)
        board = tio.find_ticket(slug)
        if board is None or board["status"] != "review":
            status = "missing" if board is None else board["status"]
            raise ReviewEntryError(f"approve requires a review ticket, got {status}")
        log_dir = tio.logs_dir / slug
        accepted = read_acceptance(log_dir)
        if accepted.kind == "corrupt":
            raise ReviewEntryError(f"Criteria Satisfaction Record is corrupt: {accepted.reason}")
        selected = read_entry(log_dir)
        if selected is not None or accepted.kind != "accepted":
            ctx = _selected_context(tio, slug)
            _require_selected_package(tio, ctx)
        if accepted.kind == "unavailable":
            _acceptance_ready(tio, ctx)
            _require_selected_package(tio, ctx)
            operation = {
                "pid": os.getpid(),
                "phase": "accepting",
                "action": "approve",
                "reason": ctx.inspection["reason"],
                "entry": ctx.inspection,
                "source_sha": ctx.inspection["capture_sha"],
                "accepted_at": utc_now_rfc3339(),
            }
            _write(operation_path(ctx.log_dir), operation)
            _commit(tio, ctx, operation)
    return op_complete(
        tio,
        slug,
        no_merge=no_merge,
        no_cleanup=no_cleanup,
        require_review_package_binding=selected is not None,
    )


def _verified_accepted_handoff(
    project_root: Path, slug: str, tio: TicketIO, snapshot: Any
) -> prep.ReviewPrepOutcome:
    from .acceptance_ledger import AcceptanceLedgerError, validate_review_package_binding

    guidance = (
        f"Ticket {slug!r} is already accepted; run booley board approve {slug} to complete it."
    )
    if snapshot is None:
        return prep.ReviewPrepOutcome(
            "failed", "Criteria Satisfaction Record is unreadable; use acceptance recovery"
        )
    binding = tio.logs_dir / slug / "acceptance" / "review-package.json"
    manifest = tio.logs_dir / slug / ".runtime" / "triage-prep" / "manifest.json"
    if not binding.exists() and not manifest.exists():
        return prep.ReviewPrepOutcome("accepted", guidance)
    try:
        validate_review_package_binding(tio.logs_dir / slug, snapshot)
        outcome = prep.verify_review_handoff(
            project_root, slug, locked_basis=tio._load_basis_unlocked(slug)
        )
    except (AcceptanceLedgerError, prep.ReviewPrepError, OSError, ValueError):
        return prep.ReviewPrepOutcome("accepted", guidance)
    return replace(outcome, message=guidance)


def _accepted_unselected_handoff(
    project_root: Path, slug: str, tio: TicketIO
) -> prep.ReviewPrepOutcome | None:
    """Return immutable handoff material for an accepted Ticket without selection."""
    from .acceptance_ledger import AcceptanceLedgerError

    try:
        _recover(tio, slug)
        with tio._ticket_lock(slug, review_operation=True):
            assert_idle(tio.logs_dir / slug)
            _quiescent(tio, slug)
            board = tio.find_ticket(slug)
            if board is None or board["status"] != "review":
                status = "missing" if board is None else board["status"]
                return prep.ReviewPrepOutcome(
                    "failed", f"review requires a review ticket, got {status}"
                )
            accepted = read_acceptance(tio.logs_dir / slug)
            if accepted.kind == "corrupt":
                return prep.ReviewPrepOutcome(
                    "failed", f"Criteria Satisfaction Record is corrupt: {accepted.reason}"
                )
            if accepted.kind != "accepted" or read_entry(tio.logs_dir / slug) is not None:
                return None
            return _verified_accepted_handoff(project_root, slug, tio, accepted.snapshot)
    except (
        ReviewEntryError,
        prep.ReviewPrepError,
        AcceptanceLedgerError,
        OSError,
        ValueError,
    ) as exc:
        return prep.ReviewPrepOutcome("failed", f"{exc}; use acceptance recovery")


def _accepted_unselected_handoff_for_ticket(
    project_root: Path, slug: str
) -> prep.ReviewPrepOutcome | None:
    """Resolve an accepted, unselected review Ticket's handoff outcome, if applicable."""
    tio = TicketIO(tickets_dir_from_project_root(project_root), project_root=project_root)
    board = tio.find_ticket(slug)
    if board is None or board["status"] != "review":
        return None
    canonical = Path(board["file"]).stem
    return _accepted_unselected_handoff(project_root, canonical, tio)


def approve_review_command(
    project_root: Path, slug: str, *, no_merge: bool = False, no_cleanup: bool = False
) -> bool:
    """Accept the selected inspection and complete without agent preparation."""
    tio = TicketIO(tickets_dir_from_project_root(project_root), project_root=project_root)
    board = tio.find_ticket(slug)
    if board is None:
        raise ReviewEntryError(f"ticket {slug!r} not found")
    slug = Path(board["file"]).stem
    if board["status"] == "done":
        return _approve_done_ticket(tio, slug, no_merge=no_merge, no_cleanup=no_cleanup)
    if board["status"] != "review":
        raise ReviewEntryError("approve requires a review ticket")
    return _approve_review_ticket(tio, slug, no_merge=no_merge, no_cleanup=no_cleanup)


def _current_review_package(tio: TicketIO, slug: str) -> prep.ReviewPrepOutcome | None:
    try:
        with tio._ticket_lock(slug, review_operation=True):
            ctx = _selected_context(tio, slug)
            _require_selected_package(tio, ctx)
            manifest = prep._read_manifest(ctx)
            assert manifest is not None
            _prompt, prompt_sha = prep._review_prompt(ctx)
            return prep._fresh_outcome(ctx, manifest, prompt_sha, ctx.inspection["capture_sha"])
    except (ReviewEntryError, prep.ReviewPrepError, OSError, ValueError):
        return None


async def _review_blocked_ticket(
    project_root: Path, slug: str, tio: TicketIO, *, force: bool
) -> prep.ReviewPrepOutcome:
    """Prepare blocked diagnostics and review material without transitioning."""
    from booley.harness.blocked_prep import prepare_blocked_dossier

    try:
        with tio._ticket_lock(slug, review_operation=True):
            assert_idle(tio.logs_dir / slug)
            _quiescent(tio, slug)
    except ReviewEntryError as exc:
        return prep.ReviewPrepOutcome("failed", str(exc))
    dossier = await prepare_blocked_dossier(project_root, slug, force=force)
    if not dossier.ready:
        return prep.ReviewPrepOutcome("failed", dossier.message)
    return await prep.prepare_review_command(project_root, slug, force=force)


async def _review_existing_ticket(
    project_root: Path, slug: str, tio: TicketIO, *, force: bool
) -> prep.ReviewPrepOutcome:
    """Reuse or refresh review material for an existing review Ticket."""
    if not force and (fresh := _current_review_package(tio, slug)) is not None:
        return fresh
    accepted = read_acceptance(tio.logs_dir / slug)
    if accepted.kind == "corrupt":
        return prep.ReviewPrepOutcome(
            "failed", f"Criteria Satisfaction Record is corrupt: {accepted.reason}"
        )
    action = "regenerate" if force or accepted.kind == "accepted" else "refresh"
    return await request_review_command(project_root, slug, action=action)


async def review_command(  # noqa: PLR0911 — each Ticket state has a distinct outcome
    project_root: Path,
    slug: str,
    *,
    request: bool = False,
    reason: str = "",
    force: bool = False,
    repair: bool = False,
) -> prep.ReviewPrepOutcome:
    """Prepare review material according to the Ticket's lifecycle state."""
    tio = TicketIO(tickets_dir_from_project_root(project_root), project_root=project_root)
    board = tio.find_ticket(slug)
    if board is None:
        return prep.ReviewPrepOutcome("failed", f"ticket {slug!r} not found")
    slug = Path(board["file"]).stem
    status = board["status"]
    if status == "review":
        accepted = _accepted_unselected_handoff(project_root, slug, tio)
        if accepted is not None:
            return accepted
    if request:
        if status != "blocked" and not (repair and status == "review"):
            return prep.ReviewPrepOutcome("failed", "--request requires a blocked ticket")
        return await request_review_command(
            project_root, slug, action="request", reason=reason, repair=repair
        )
    if repair:
        return prep.ReviewPrepOutcome("failed", "--repair requires --request")
    if status == "blocked":
        return await _review_blocked_ticket(project_root, slug, tio, force=force)
    if status != "review":
        return prep.ReviewPrepOutcome("failed", f"review requires blocked or review, got {status}")
    return await _review_existing_ticket(project_root, slug, tio, force=force)


async def prepare_review(
    project_root: Path, slug: str, *, force: bool = False
) -> prep.ReviewPrepOutcome:
    """Prepare artifacts through the Ticket Board lifecycle facade."""
    accepted = _accepted_unselected_handoff_for_ticket(project_root, slug)
    if accepted is not None:
        return accepted
    return await prep.prepare_review(project_root, slug, force=force)


def verify_review_handoff(project_root: Path, slug: str) -> prep.ReviewPrepOutcome:
    """Verify the immutable package selected for an automatic handoff."""
    return prep.verify_review_handoff(project_root, slug)


async def prepare_review_command(
    project_root: Path, slug: str, *, force: bool = False
) -> prep.ReviewPrepOutcome:
    """Run manual artifact preparation through the lifecycle facade."""
    accepted = _accepted_unselected_handoff_for_ticket(project_root, slug)
    if accepted is not None:
        return accepted
    return await prep.prepare_review_command(project_root, slug, force=force)


def review_briefing_command(
    project_root: Path, slug: str, *, open_diffs: bool = True
) -> prep.ReviewBriefingOutcome:
    """Render a prepared package through the lifecycle facade."""
    return prep.review_briefing_command(project_root, slug, open_diffs=open_diffs)


def run_review_command(project_root: Path, slug: str, command: list[str]) -> int:
    """Run a ticket-bound endpoint under the lifecycle's execution fence."""
    from .review_execution import run_review_command as execute

    return execute(project_root, slug, command)

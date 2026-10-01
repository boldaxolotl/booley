"""Board-owned, recoverable unaccepted handoff for outstanding done findings."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from booley.core.boundary import require_dict, require_list
from booley.criteria.state import DevelopmentState
from booley.evidence.review_dispositions import outstanding_done_findings
from booley.runtime.timefmt import utc_now_rfc3339

from .acceptance_ledger import read_acceptance
from .lifecycle import TicketState
from .paths import existing_human_log_file, existing_runtime_file, ticket_log_dir
from .persistence import atomic_replace_bytes
from .review_records import advisory_handoff_path, read_entry, read_json

ADVISORY_REVIEW_REASON = (
    "Current done review findings require explicit human approval before acceptance."
)


def read_advisory_marker(log_dir: Path) -> dict[str, Any] | None:
    """Read a complete advisory handoff marker, rejecting corrupt records."""
    marker = read_json(advisory_handoff_path(log_dir))
    if marker is None:
        return None
    if marker.get("schema") != 1:
        raise ValueError("unsupported advisory handoff schema")
    if not isinstance(marker.get("execution_id"), str):
        raise ValueError("advisory handoff execution identity is missing")
    heads = require_dict(marker.get("heads"), field="advisory handoff heads")
    if not heads or any(not isinstance(head, str) or not head for head in heads.values()):
        raise ValueError("advisory handoff heads are missing")
    findings = require_list(marker.get("findings"), field="advisory handoff findings")
    if not findings or any(
        not isinstance(row, dict) or not row.get("criterion") for row in findings
    ):
        raise ValueError("advisory handoff findings are missing")
    if not isinstance(marker.get("created_at"), str) or not marker["created_at"]:
        raise ValueError("advisory handoff timestamp is missing")
    return marker


def advisory_marker_current(tio: Any, slug: str) -> bool:
    """Require the durable marker to match this execution and exact current heads."""
    from .operations import _handoff_basis_heads

    marker = read_advisory_marker(ticket_log_dir(tio.logs_dir, slug))
    if marker is None:
        return False
    if marker["execution_id"] != tio.read_progress(slug)["execution_id"]:
        return False
    heads = _handoff_basis_heads(tio, slug)
    if heads is None or marker["heads"] != heads:
        raise ValueError("advisory handoff heads changed; inspect or reset the run")
    return True


def _prepare(tio: Any, slug: str, execution_id: str, *, renew_heads: bool = False) -> bool:
    """Fence jobs, strict acceptance and immutable heads before marker publication."""
    from booley.runtime.pid import is_pid_alive

    from .operations import (
        _bind_existing_handoff_snapshot,
        _handoff_basis_heads,
        _handoff_jobs_clear,
    )

    owner = tio.read_progress(slug).get("execution_owner_pid")
    if (
        isinstance(owner, int)
        and str(owner) != str(tio._resolve_developer_pid())
        and is_pid_alive(owner)
    ):
        print("Error: advisory handoff execution owner is still running", file=sys.stderr)
        return False
    log_dir = ticket_log_dir(tio.logs_dir, slug)
    if not _handoff_jobs_clear(log_dir, slug):
        return False
    heads = _handoff_basis_heads(tio, slug)
    if heads is None:
        return False
    existing = _bind_existing_handoff_snapshot(tio, log_dir, slug, heads)
    if existing is not None:
        if existing:
            print(
                f"Error: '{slug}' already has acceptance; inspect it and use board approve "
                "for authorized recovery, or booley board reset to discard the run.",
                file=sys.stderr,
            )
        return False
    return _publish_marker(tio, slug, execution_id, heads, renew_heads=renew_heads)


def _publish_marker(
    tio: Any, slug: str, execution_id: str, heads: dict[str, str], *, renew_heads: bool = False
) -> bool:
    from .criteria_acceptance import check_criteria_acceptance

    log_dir = ticket_log_dir(tio.logs_dir, slug)
    path = existing_runtime_file(tio.logs_dir, slug, "booley_state.json")
    if not path.exists():
        raise ValueError("durable criteria state is unavailable")
    state = DevelopmentState.load(path)
    verdict = check_criteria_acceptance(
        path, work_dir=Path(state.work_dir) if state.work_dir else None
    )
    if verdict.disposition != "review" or verdict.provisional:
        print(
            f"Error: cannot hand off '{slug}': acceptance is {verdict.disposition}",
            file=sys.stderr,
        )
        return False
    findings = outstanding_done_findings(DevelopmentState.load(path).criteria)
    if not findings:
        raise ValueError("advisory handoff requires outstanding done findings")
    old = read_advisory_marker(log_dir)
    if old is not None:
        same_execution = old["execution_id"] == execution_id
        if same_execution and old["heads"] == heads and old["findings"] == findings:
            return True
        if read_entry(log_dir) is not None:
            raise ValueError("selected review inspection prevents advisory marker renewal")
        if same_execution and not renew_heads:
            raise ValueError(
                "stale advisory handoff marker; run booley board review or reset the run"
            )
    marker = {
        "schema": 1,
        "execution_id": execution_id,
        "heads": dict(sorted(heads.items())),
        "findings": findings,
        "created_at": utc_now_rfc3339(),
    }
    atomic_replace_bytes(
        advisory_handoff_path(log_dir), (json.dumps(marker, sort_keys=True) + "\n").encode()
    )
    return True


def op_handoff_advisory(tio: Any, slug: str, *, expected_execution_id: str | None = None) -> bool:
    """Move to review without accepting, merging or cleaning up the run."""
    from .operations import _get_old_state, _op_move_and_log, _validate_transitions_for_handoff

    if not existing_human_log_file(tio.logs_dir, slug, "run.log").exists():
        print("ERROR: run.log not found -- developer log missing.", file=sys.stderr)
        return False
    entry, old_status, old_step = _get_old_state(tio, slug, "summary")
    if not _validate_transitions_for_handoff(tio, slug, entry):
        return False
    execution_id = str((entry or {}).get("execution_id", ""))
    if expected_execution_id is not None and expected_execution_id != execution_id:
        print("Error: advisory handoff execution identity changed", file=sys.stderr)
        return False
    if old_status == "review":
        return read_acceptance(
            ticket_log_dir(tio.logs_dir, slug)
        ).kind == "unavailable" and advisory_marker_current(tio, slug)
    return _op_move_and_log(
        tio,
        slug,
        TicketState.REVIEW,
        {"step": "summary"},
        (f"{old_status}:{old_step}", "review:summary", "ticket-execute", ADVISORY_REVIEW_REASON),
        append_step="summary",
        expected_status="running",
        expected_execution_id=execution_id,
        before_move=lambda: _prepare(tio, slug, execution_id),
    )


def renew_advisory_inspection(tio: Any, slug: str) -> None:
    """Explicit board review may renew heads before the initial selected inspection."""
    log_dir = ticket_log_dir(tio.logs_dir, slug)
    with tio._ticket_lock(slug, review_operation=True):
        from .review_lifecycle import _quiescent

        _quiescent(tio, slug)
        marker = read_advisory_marker(log_dir)
        if marker is None or read_entry(log_dir) is not None:
            return
        if marker["execution_id"] != tio.read_progress(slug)["execution_id"]:
            raise ValueError("advisory marker belongs to another execution")
        if not _prepare(tio, slug, marker["execution_id"], renew_heads=True):
            raise ValueError(
                "cannot renew advisory review; verify Criteria and inspect existing acceptance"
            )

"""Hand off a Ticket met only by a Provisional Coverage Verdict (ADR 0066).

Such a Ticket always goes to review, whatever ``on_success.destination`` says,
and its Criteria Satisfaction Record is NOT frozen: the strict verdict is not
met yet. Instead a durable marker records the participant heads and the
candidate record the provisional verdict was judged from. Review then proceeds
as an unaccepted inspection, and approval freezes acceptance only after the
Human's waiver decisions are promoted and strict evaluation passes.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from booley.runtime.timefmt import utc_now_rfc3339

from .acceptance_ledger import read_acceptance
from .lifecycle import TicketState
from .paths import existing_human_log_file, ticket_log_dir
from .persistence import atomic_replace_bytes
from .provisional_coverage import TicketProvisionalCoverage
from .review_records import provisional_handoff_path, read_json

PROVISIONAL_HANDOFF_SCHEMA = 1
PROVISIONAL_REVIEW_REASON = (
    "Coverage is met only provisionally; decide each Waiver Candidate at review."
)


def provisional_marker(
    provisional: TicketProvisionalCoverage, *, execution_id: str, heads: dict[str, str]
) -> dict[str, Any]:
    """Build the durable marker for one provisional handoff."""
    return {
        "schema": PROVISIONAL_HANDOFF_SCHEMA,
        "execution_id": execution_id,
        "heads": dict(sorted(heads.items())),
        "candidate_record_sha256": provisional.record_sha256,
        "criteria": [
            {
                "key": item.key,
                "campaign_id": item.campaign_id,
                "reference_path": item.reference_path,
                "offered": list(item.verdict.offered_ids),
                "needed": sorted(s.candidate_id for s in item.verdict.screens if s.needed),
            }
            for item in provisional.criteria
        ],
        "created_at": utc_now_rfc3339(),
    }


def read_provisional_marker(log_dir: Path) -> dict[str, Any] | None:
    """Return the provisional handoff marker, or None for an ordinary review."""
    marker = read_json(provisional_handoff_path(log_dir))
    if marker is not None and marker.get("schema") != PROVISIONAL_HANDOFF_SCHEMA:
        raise ValueError(f"unsupported provisional handoff schema: {marker.get('schema')!r}")
    return marker


def _prepare(
    tio: Any,
    slug: str,
    provisional: TicketProvisionalCoverage,
    execution_id: str,
) -> bool:
    """Fence Jobs and record the marker; never freeze acceptance."""
    from .operations import _handoff_basis_heads, _handoff_jobs_clear

    log_dir = ticket_log_dir(tio.logs_dir, slug)
    if not _handoff_jobs_clear(log_dir, slug):
        return False
    if read_acceptance(log_dir).kind != "unavailable":
        print(
            f"Error: cannot hand off '{slug}' provisionally: acceptance is already recorded",
            file=sys.stderr,
        )
        return False
    heads = _handoff_basis_heads(tio, slug)
    if heads is None:
        return False
    marker = provisional_marker(provisional, execution_id=execution_id, heads=heads)
    raw = (json.dumps(marker, sort_keys=True) + "\n").encode()
    atomic_replace_bytes(provisional_handoff_path(log_dir), raw)
    return True


def op_handoff_provisional(
    tio: Any,
    slug: str,
    provisional: TicketProvisionalCoverage,
    *,
    expected_execution_id: str | None = None,
) -> bool:
    """Move a running Ticket to review on a provisional verdict, without freezing."""
    from .operations import _get_old_state, _op_move_and_log, _validate_transitions_for_handoff

    if not provisional.criteria:
        raise ValueError("provisional handoff requires at least one provisional Criterion")
    if not existing_human_log_file(tio.logs_dir, slug, "run.log").exists():
        print("ERROR: run.log not found -- developer log missing.", file=sys.stderr)
        return False
    entry, old_status, old_step = _get_old_state(tio, slug, "summary")
    if not _validate_transitions_for_handoff(tio, slug, entry):
        return False
    execution_id = expected_execution_id or str((entry or {}).get("execution_id", ""))
    keys = ", ".join(item.key for item in provisional.criteria)
    return _op_move_and_log(
        tio,
        slug,
        TicketState.REVIEW,
        {"step": "summary"},
        (
            f"{old_status}:{old_step}",
            "review:summary",
            "ticket-execute",
            f"ready for review; provisional coverage: {keys}",
        ),
        append_step="summary",
        expected_status="running" if expected_execution_id is not None else None,
        expected_execution_id=expected_execution_id,
        before_move=lambda: _prepare(tio, slug, provisional, execution_id),
    )


__all__ = [
    "PROVISIONAL_REVIEW_REASON",
    "op_handoff_provisional",
    "provisional_marker",
    "read_provisional_marker",
]

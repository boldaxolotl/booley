"""Identity-bound runtime publication, retained proof and pointer retirement.

Callers hold the Ticket lock and validate publication/owner authority first.
"""

from __future__ import annotations

import hashlib
import json
import logging
from copy import deepcopy
from pathlib import Path
from typing import Any

from booley.core.boundary import BoundaryError, require_dict
from booley.criteria.state import CriterionChange, DevelopmentState
from booley.evidence.fields import SOURCE_FINGERPRINT_DETAIL_KEY
from booley.flows.source_fingerprint import compute_source_fingerprint

from .acceptance_ledger import (
    AcceptanceLedgerError,
    current_evidence_records,
    historical_ticket_identities,
    read_acceptance,
    record_amendment_observations,
)
from .paths import existing_runtime_file, ticket_log_dir
from .persistence import atomic_replace_bytes, atomic_write_once
from .ticket_baseline import TicketBaselineError, ticket_baseline_from_machine

logger = logging.getLogger(__name__)


def capture_runtime(tio: Any, slug: str, operation_id: str, kind: str) -> Path:
    """Preserve each original runtime document once before publication mutates it."""
    log = ticket_log_dir(tio.logs_dir, slug)
    history = log / kind
    for source, suffix in (
        (log / "ticket.md", "prior-ticket.md"),
        (existing_runtime_file(tio.logs_dir, slug, "booley_state.json"), "prior-state.json"),
    ):
        destination = history / f"{operation_id}.{suffix}"
        if source.exists() and not destination.exists():
            atomic_write_once(destination, source.read_bytes())
    return history


def capture_evidence(log: Path, history: Path, operation_id: str, identity: dict) -> None:
    """Bind the original selection and checked record hashes before retiring them."""
    path = history / f"{operation_id}.prior-evidence.json"
    if path.exists():
        return
    state_path = existing_runtime_file(log.parent, log.name, "booley_state.json")
    state = DevelopmentState.load(state_path)
    records = current_evidence_records(log, state, identity)
    payload = {
        "ticket_identity": identity,
        "selected_transactions": state.acceptance_transactions,
        "references": [
            {
                "sequence": row["sequence"],
                "criterion": row["criterion"],
                "sha256": hashlib.sha256(
                    json.dumps(
                        row, sort_keys=True, separators=(",", ":"), ensure_ascii=False
                    ).encode()
                ).hexdigest(),
            }
            for row in records
        ],
    }
    atomic_write_once(path, (json.dumps(payload, sort_keys=True, indent=2) + "\n").encode())


def _runtime_document(path: Path, description: str) -> dict[str, Any]:
    try:
        return require_dict(json.loads(path.read_text(encoding="utf-8")), field=description)
    except (OSError, UnicodeError, json.JSONDecodeError, BoundaryError) as exc:
        raise TicketBaselineError(f"invalid {description} {path}: {exc}") from exc


def _retire_pointer(path: Path, history: Path, operation_id: str) -> None:
    destination = history / f"{operation_id}.{path.parent.name}-{path.name}"
    atomic_write_once(destination, path.read_bytes())
    path.unlink()


def _foreign_pointer_paths(log: Path, identity: dict) -> list[Path]:
    """Validate selected pointers and identify foreign ones without mutation."""
    paths = []
    accepted = read_acceptance(log)
    if accepted.kind == "corrupt":
        raise AcceptanceLedgerError(accepted.reason)
    if accepted.snapshot is not None and accepted.snapshot.ticket_identity != identity:
        paths.append(log / "acceptance" / "accepted.json")
    for path in (log / ".runtime/triage-prep/manifest.json", log / "review/entry.json"):
        if not path.exists():
            continue
        row = _runtime_document(path, "selected review pointer")
        pointer_identity = row.get("ticket_identity")
        if pointer_identity is None:
            generation = row.get("ticket_generation")
            if generation == identity["generation"]:
                continue
            if not isinstance(generation, str):
                raise TicketBaselineError(
                    f"selected review pointer has no Ticket identity: {path}"
                )
        else:
            ticket_baseline_from_machine(pointer_identity)
            if pointer_identity == identity:
                continue
        paths.append(path)

    return paths


def retire_foreign_pointers(log: Path, history: Path, operation_id: str, identity: dict) -> None:
    """Retire selected old-generation pointers while retaining immutable packages."""
    for path in _foreign_pointer_paths(log, identity):
        _retire_pointer(path, history, operation_id)


def refresh_snapshot(tio: Any, slug: str) -> None:
    """Repair missing/malformed/foreign snapshots from validated Board authority."""
    basis = tio._load_basis_unlocked(slug)
    from .scanner import find_ticket_file

    board, _status = find_ticket_file(tio.tickets_dir, slug, project_root=tio._project_root)
    if board is None:
        raise TicketBaselineError("executable Ticket disappeared during snapshot repair")
    snapshot = ticket_log_dir(tio.logs_dir, slug) / "ticket.md"
    reason = "missing"
    parsed = None
    if snapshot.exists():
        try:
            document = tio._convert_ticket(snapshot, slug, "executable")
            machine = document.generated.get("machine")
            parsed = ticket_baseline_from_machine(machine)
            reason = "another identity"
        except (TicketBaselineError, ValueError, OSError):
            reason = "malformed"
    _repair_completed_amendment(tio, slug, board, basis, parsed)
    if parsed is not None and parsed.ticket_identity() == basis.ticket_identity():
        return
    atomic_replace_bytes(snapshot, board.read_bytes(), mode=0o644)
    logger.info("Refreshed runtime Ticket snapshot for %s (%s)", slug, reason)


def source_proof_is_fresh(stamp: object, checkout: Path) -> bool:
    """Validate one source stamp and compare every declared fingerprint category."""
    if not isinstance(stamp, dict) or not isinstance(stamp.get("fingerprint"), dict):
        return False
    categories = stamp.get("categories")
    if (
        not isinstance(categories, list)
        or not categories
        or any(
            not isinstance(category, str)
            or not isinstance(stamp["fingerprint"].get(category), dict)
            or not isinstance(stamp["fingerprint"][category].get("digest"), str)
            for category in categories
        )
    ):
        return False
    target = stamp.get("target")
    try:
        current = compute_source_fingerprint(
            checkout, target=target if isinstance(target, str) else None
        )
    except (OSError, ValueError):
        return False
    return all(
        stamp["fingerprint"].get(category, {}).get("digest")
        == current.get(category, {}).get("digest")
        for category in categories
    )


def _trusted_observation(row: dict | None, original: Any, checkout: Path) -> bool:
    if row is None or original is None:
        return False
    if row.get("producer") not in {
        "sim",
        "simulation_campaign",
        "lint",
        "synth",
        "fpga",
        "reviewer",
        "review",
        "amendment",
    }:
        return False
    matches = (
        row["detail"] == original.detail
        and row["params"] == original.params
        and row["mandatory"] == original.mandatory
        and row["met"] == original.met
    )
    return matches and source_proof_is_fresh(
        row["detail"].get(SOURCE_FINGERPRINT_DETAIL_KEY), checkout
    )


def _reference_digests(rows: list[dict], name: str) -> list[dict]:
    return [
        {
            "sequence": source["sequence"],
            "sha256": hashlib.sha256(
                json.dumps(
                    source, sort_keys=True, separators=(",", ":"), ensure_ascii=False
                ).encode()
            ).hexdigest(),
        }
        for source in rows
        if source["criterion"] == name
    ]


def retained_observations(
    log: Path,
    prior: DevelopmentState,
    state: DevelopmentState,
    old_identity: dict,
    operation_id: str,
    checkout: Path,
) -> list[CriterionChange]:
    """Require immutable matching and fresh source proof for every carried met value."""
    rows = current_evidence_records(log, prior, old_identity)
    latest = {row["criterion"]: row for row in rows}
    changes = []
    for name, entry in state.criteria.items():
        if name.startswith("_") or not entry.met:
            continue
        row = latest.get(name)
        original = prior.criteria.get(name)
        trusted = _trusted_observation(row, original, checkout)
        if not trusted:
            entry.met = False
            entry.stale = True
            continue
        references = _reference_digests(rows, name)
        entry.detail = {
            **entry.detail,
            "amendment_provenance": {
                "operation_id": operation_id,
                "old_ticket_identity": old_identity,
                "source_references": references,
            },
        }
        changes.append(
            CriterionChange(
                name,
                entry.met,
                "validated amendment carry-over",
                entry.detail,
                entry.mandatory,
                entry.params,
            )
        )
    return changes


def publish_retained_observations(
    log: Path,
    prior: DevelopmentState,
    state: DevelopmentState,
    old_identity: dict,
    new_identity: dict,
    operation_id: str,
    checkout: Path,
) -> None:
    """Retire old selection and publish current-generation proof before saving state."""
    changes = retained_observations(log, prior, state, old_identity, operation_id, checkout)
    state.acceptance_transactions = []
    record_amendment_observations(
        log, state, changes, operation_id=operation_id, ticket_identity=new_identity
    )


def current_transaction_selection(log: Path, state: DevelopmentState, identity: dict) -> list[str]:
    """Keep mixed legacy transactions only when they contain current observations."""
    records = current_evidence_records(log, state, identity)
    sequences = {row["sequence"] for row in records}
    selected = []
    for transaction in state.acceptance_transactions:
        if any(
            int(path.name.split(".", 1)[0]) in sequences
            for path in (log / "acceptance/evidence").glob(f"*.tx.{transaction}")
        ):
            selected.append(transaction)
    return selected


def _project_repaired_state(
    tio: Any, slug: str, board: Path, state: DevelopmentState, current_keys: set[str]
) -> None:
    from .criteria_projection import project_ticket_criteria

    projection = project_ticket_criteria(tio._convert_ticket(board, slug, "executable").spec)
    for name, required in projection.required.items():
        if name not in state.criteria or name in current_keys:
            continue
        state.criteria[name].mandatory = required
        state.criteria[name].params = projection.params.get(name, {})


def _amendment_ancestry(log: Path, basis: Any) -> tuple[dict, set[str]]:
    amendment = basis.ticket_identity()["amendment"]
    history = _runtime_document(
        log / "amendments" / f"{amendment['operation_id']}.json", "amendment history"
    )
    if history.get("new_basis_id") != basis.basis_id:
        raise TicketBaselineError(
            "historical amendment disagrees with the current runtime identity"
        )
    ancestors = set()
    parent = history.get("old_basis_id")
    histories = [
        _runtime_document(path, "amendment history")
        for path in (log / "amendments").glob("????????????????????????????????.json")
    ]
    for _ in range(len(histories) + 1):
        if not isinstance(parent, str) or parent in ancestors:
            break
        ancestors.add(parent)
        previous = next((row for row in histories if row.get("new_basis_id") == parent), None)
        if previous is None:
            break
        parent = previous.get("old_basis_id")
    return history, ancestors


def _historical_identity(
    log: Path, state: DevelopmentState, basis: Any, old: Any, history: dict, ancestors: set[str]
) -> dict | None:
    identities = historical_ticket_identities(log, state, basis.ticket_identity())
    candidates = [old.ticket_identity()] if old is not None else []
    if isinstance(history.get("old_ticket_identity"), dict):
        candidates.append(history["old_ticket_identity"])
    candidates.extend(identities)
    for candidate in candidates:
        parsed = ticket_baseline_from_machine(candidate)
        if parsed.basis_id in ancestors:
            return candidate
    return None


def _repair_current_criteria(
    log: Path,
    prior: DevelopmentState,
    state: DevelopmentState,
    basis: Any,
    old_identity: dict | None,
    operation: str,
    checkout: Path,
) -> None:
    current = current_evidence_records(log, state, basis.ticket_identity())
    current_keys = {row["criterion"] for row in current}
    protected = {
        name: deepcopy(state.criteria[name]) for name in current_keys if name in state.criteria
    }
    if old_identity is None:
        changes = []
        for name, entry in state.criteria.items():
            if name not in current_keys and entry.met:
                entry.met = False
                entry.stale = True
    else:
        changes = retained_observations(log, prior, state, old_identity, operation, checkout)
    report = state.criteria.get("_report_submitted")
    if report is not None and "_report_submitted" not in current_keys:
        report.met = False
        report.stale = True
    state.criteria.update(protected)
    state.acceptance_transactions = current_transaction_selection(
        log, state, basis.ticket_identity()
    )
    changes = [change for change in changes if change.key not in current_keys]
    if changes:
        record_amendment_observations(
            log,
            state,
            changes,
            operation_id=operation + ".repair",
            ticket_identity=basis.ticket_identity(),
        )


def _repair_completed_amendment(tio: Any, slug: str, board: Path, basis: Any, old: Any) -> None:
    """Repair old successful publishers using immutable amendment ancestry and proof."""
    amendment = basis.ticket_identity().get("amendment")
    if not isinstance(amendment, dict):
        return
    log = ticket_log_dir(tio.logs_dir, slug)
    operation = amendment["operation_id"]
    if not (log / "amendments" / f"{operation}.json").exists():
        return
    history, ancestors = _amendment_ancestry(log, basis)
    path = existing_runtime_file(tio.logs_dir, slug, "booley_state.json")
    if not path.exists():
        return
    from .ticket_baseline import worktree_for_ref

    state = DevelopmentState.load(path)
    identity = basis.ticket_identity()
    current_keys = {row["criterion"] for row in current_evidence_records(log, state, identity)}
    current_selection = current_transaction_selection(log, state, identity)
    missing_proof = any(
        entry.met and not name.startswith("_") and name not in current_keys
        for name, entry in state.criteria.items()
    )
    if (
        not missing_proof
        and current_selection == state.acceptance_transactions
        and not _foreign_pointer_paths(log, identity)
    ):
        return
    old_identity = _historical_identity(log, state, basis, old, history, ancestors)
    prior_path = log / "amendments" / f"{operation}.prior-state.json"
    prior = DevelopmentState.load(prior_path if prior_path.exists() else path)
    history_dir = capture_runtime(tio, slug, operation + ".repair", "amendments")
    _project_repaired_state(tio, slug, board, state, current_keys)
    checkout = worktree_for_ref(Path(tio._project_root), basis.participant("outer").ticket_ref)
    if checkout is None:
        raise TicketBaselineError("historical amendment execution checkout is unavailable")
    _repair_current_criteria(log, prior, state, basis, old_identity, operation, checkout)
    retire_foreign_pointers(log, history_dir, operation + ".repair", basis.ticket_identity())
    state.save()

"""Persist and evaluate the non-source dimensions of Reviewer freshness."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from booley.fusesoc import fusesoc_registry

_TICKET_FILE = "ticket.md"
_DECISIONS_FILE = "answered_questions.md"


class ReviewTicketError(OSError):
    """The persisted Ticket context can no longer be read."""


@dataclass(frozen=True)
class ReviewInvocation:
    """Inputs whose identity determines one Reviewer contract."""

    work_dir: Path
    category: str
    focus: str
    scope: tuple[str, ...]
    mode: str
    targets: tuple[str, ...]
    target_kind: str
    ticket_path: Path | None = None


def _digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _ticket_source(work_dir: Path, ticket_path: Path | None) -> dict[str, str]:
    """Resolve and persist the documents which bind a review to its Ticket."""
    logs = Path(os.environ["BOOLEY_LOGS_DIR"]) if os.environ.get("BOOLEY_LOGS_DIR") else None
    logs_ticket = logs / _TICKET_FILE if logs else None
    if logs_ticket is not None and not logs_ticket.is_absolute():
        logs_ticket = work_dir / logs_ticket
    resolved_ticket = (
        logs_ticket if logs_ticket is not None and logs_ticket.is_file() else ticket_path
    )
    if resolved_ticket is not None and not resolved_ticket.is_absolute():
        resolved_ticket = work_dir / resolved_ticket
    decisions = logs / _DECISIONS_FILE if logs else None
    if decisions is None and resolved_ticket is not None:
        decisions = resolved_ticket.parent / _DECISIONS_FILE
    elif decisions is not None and not decisions.is_absolute():
        decisions = work_dir / decisions
    return {
        "ticket": str(resolved_ticket.resolve()) if resolved_ticket else "",
        "accepted_decisions": str(decisions.resolve()) if decisions else "",
    }


def _optional_document(path_value: object) -> str:
    if not isinstance(path_value, str) or not path_value:
        return ""
    path = Path(path_value)
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def _ticket_context_digest(source: Mapping[str, object]) -> str:
    """Hash one persisted Ticket source without consulting ambient state."""
    try:
        ticket_value = source.get("ticket")
        ticket = ""
        if isinstance(ticket_value, str) and ticket_value:
            ticket = Path(ticket_value).read_text(encoding="utf-8", errors="replace")
        return _digest(
            {
                "ticket": ticket,
                "accepted_decisions": _optional_document(source.get("accepted_decisions")),
            }
        )
    except OSError as exc:
        raise ReviewTicketError(
            f"Could not read persisted Reviewer Ticket context: {exc}"
        ) from exc


def target_surface_digest(work_dir: Path) -> str:
    """Hash authored FuseSoC Target declarations without running FuseSoC."""
    rows: list[dict[str, str]] = []
    for core_file in fusesoc_registry.discover_cores(work_dir):
        try:
            rel = core_file.relative_to(work_dir).as_posix()
        except ValueError:
            rel = str(core_file)
        rows.append({"path": rel, "sha256": hashlib.sha256(core_file.read_bytes()).hexdigest()})
    return _digest(rows)


def build_review_contract_detail(invocation: ReviewInvocation) -> dict[str, Any]:
    """Build the canonical persisted identity for a Reviewer invocation."""
    ticket_source = _ticket_source(invocation.work_dir, invocation.ticket_path)
    return {
        "category": invocation.category,
        "focus": invocation.focus,
        "scope": sorted(path.replace("\\", "/") for path in invocation.scope),
        "mode": invocation.mode,
        "targets": list(invocation.targets),
        "target_kind": invocation.target_kind,
        "ticket_source": ticket_source,
        "ticket_digest": _ticket_context_digest(ticket_source),
        "target_surface_digest": target_surface_digest(invocation.work_dir),
    }


def finalize_review_detail(
    detail: Mapping[str, Any],
    source_fingerprint: Mapping[str, Any],
) -> dict[str, Any]:
    """Attach one source stamp and derive its receipt ID atomically."""
    finalized = dict(detail)
    finalized["_source_fingerprint"] = dict(source_fingerprint)
    contract = finalized.get("contract")
    if isinstance(contract, Mapping):
        finalized["receipt_id"] = _digest(
            {"contract": dict(contract), "source_fingerprint": dict(source_fingerprint)}
        )
    return finalized


def review_receipt_drift(detail: Mapping[str, Any], work_dir: Path) -> list[str]:
    """Return changed non-source dimensions for a persisted review receipt."""
    contract = detail.get("contract")
    if not isinstance(contract, Mapping):
        return []
    changed: list[str] = []
    source = contract.get("ticket_source")
    if not isinstance(source, Mapping):
        source = _ticket_source(work_dir, None)
    if contract.get("ticket_digest") != _ticket_context_digest(source):
        changed.append("ticket")
    if contract.get("target_surface_digest") != target_surface_digest(work_dir):
        changed.append("target_surface")
    return changed

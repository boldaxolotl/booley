"""Persist and evaluate the non-source dimensions of Reviewer freshness."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from booley.fusesoc import fusesoc_registry

_TICKET_FILE = "ticket.md"
_DECISIONS_FILE = "answered_questions.md"


def _digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def ticket_context_digest(ticket_path: Path | None = None) -> str:
    """Hash the mounted Ticket and accepted decisions as one scope snapshot."""
    logs = Path(os.environ["BOOLEY_LOGS_DIR"]) if os.environ.get("BOOLEY_LOGS_DIR") else None
    resolved_ticket = (logs / _TICKET_FILE) if logs else ticket_path
    decisions = (
        (logs / _DECISIONS_FILE)
        if logs
        else (resolved_ticket.parent / _DECISIONS_FILE if resolved_ticket else None)
    )
    return _digest(
        {
            "ticket": _read(resolved_ticket) if resolved_ticket else "",
            "accepted_decisions": _read(decisions) if decisions else "",
        }
    )


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


def build_review_contract_detail(
    *,
    work_dir: Path,
    category: str,
    focus: str,
    scope: list[str],
    mode: str,
    targets: tuple[str, ...],
    target_kind: str,
    ticket_path: Path | None = None,
) -> dict[str, Any]:
    """Build the canonical persisted identity for a Reviewer invocation."""
    return {
        "category": category,
        "focus": focus,
        "scope": sorted(path.replace("\\", "/") for path in scope),
        "mode": mode,
        "targets": list(targets),
        "target_kind": target_kind,
        "ticket_digest": ticket_context_digest(ticket_path),
        "target_surface_digest": target_surface_digest(work_dir),
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
    if contract.get("ticket_digest") != ticket_context_digest():
        changed.append("ticket")
    if contract.get("target_surface_digest") != target_surface_digest(work_dir):
        changed.append("target_surface")
    return changed

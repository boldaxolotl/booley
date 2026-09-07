"""Persist and evaluate the source-scoped Reviewer contract."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from booley.core.boundary import as_dict, as_str_list
from booley.targets.flow_names import config_section

_TICKET_FILE = "ticket.md"
_DECISIONS_FILE = "answered_questions.md"
REVIEW_DETAIL_VERSION = 4
_FRESHNESS_ONLY_CONTRACT_FIELDS = frozenset({"scope_hashes"})


class ReviewContextError(OSError):
    """Persisted review context can no longer be read."""


# Compatibility for callers which catch the old, narrower error name.
ReviewTicketError = ReviewContextError


@dataclass(frozen=True)
class ReviewInvocation:
    """Inputs whose identity determines one Reviewer contract."""

    work_dir: Path
    category: str
    focus: str
    scope: tuple[str, ...]
    mode: str
    spec_path: Path | None = None
    steering: str = ""


def _digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _resolved(path: Path | None, work_dir: Path) -> Path | None:
    if path is None:
        return None
    return path if path.is_absolute() else work_dir / path


def _ticket_path(work_dir: Path) -> Path | None:
    logs_value = os.environ.get("BOOLEY_LOGS_DIR")
    if not logs_value:
        return None
    candidate = _resolved(Path(logs_value) / _TICKET_FILE, work_dir)
    return candidate if candidate is not None and candidate.is_file() else None


def _decisions_path(work_dir: Path, ticket: Path | None) -> Path | None:
    logs_value = os.environ.get("BOOLEY_LOGS_DIR")
    candidate = Path(logs_value) / _DECISIONS_FILE if logs_value else None
    if candidate is None and ticket is not None:
        candidate = ticket.parent / _DECISIONS_FILE
    return _resolved(candidate, work_dir)


def _linked_spec_path(ticket: Path, work_dir: Path) -> Path | None:
    from booley.ticket_board.frontmatter import parse_frontmatter

    fields, _body = parse_frontmatter(ticket.read_text(encoding="utf-8", errors="replace"))
    value = fields.get("spec")
    if not isinstance(value, str) or not value.strip():
        return None
    return _resolved(Path(value.strip()), work_dir)


def _document_digest(path: Path | None) -> str:
    if path is None or not path.exists():
        return _digest("")
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise ReviewContextError(f"Could not read persisted Reviewer context: {exc}") from exc


def _scope_hashes(work_dir: Path, scope: tuple[str, ...]) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for raw in sorted(scope):
        normalized = raw.replace("\\", "/").removeprefix("./")
        path = work_dir / normalized
        hashes[normalized] = _document_digest(path)
    return hashes


def _tb_policy_digest(work_dir: Path, category: str) -> str:
    if category != "tb":
        return _digest({})
    try:
        from booley.runtime.shared_infra import _load_rtl_config

        cfg = _load_rtl_config(work_dir)
    except ImportError:
        cfg = None
    flows = as_dict((cfg or {}).get("flows"), default={}) or {}
    sim = config_section(flows, "sim")
    policy = {
        "pass_sentinels": as_str_list(sim.get("pass_sentinels")),
        "fail_sentinels": as_str_list(sim.get("fail_sentinels")),
        "trace_files": as_str_list(sim.get("trace_files")),
    }
    return _digest(policy)


def build_review_contract_detail(invocation: ReviewInvocation) -> dict[str, Any]:
    """Build the canonical persisted identity for a Reviewer invocation."""
    ticket = _ticket_path(invocation.work_dir)
    spec = _linked_spec_path(ticket, invocation.work_dir) if ticket else None
    if spec is None and ticket is None:
        spec = _resolved(invocation.spec_path, invocation.work_dir)
    decisions = _decisions_path(invocation.work_dir, ticket)
    scope = tuple(sorted(path.replace("\\", "/").removeprefix("./") for path in invocation.scope))
    return {
        "version": REVIEW_DETAIL_VERSION,
        "category": invocation.category,
        "focus": invocation.focus,
        "scope": list(scope),
        "scope_hashes": _scope_hashes(invocation.work_dir, scope),
        "mode": invocation.mode,
        "ticket_source": str(ticket.resolve()) if ticket else "",
        "ticket_digest": _document_digest(ticket),
        "spec_source": str(spec.resolve()) if spec else "",
        "spec_digest": _document_digest(spec),
        "decisions_source": str(decisions.resolve()) if decisions else "",
        "decisions_digest": _document_digest(decisions),
        "tb_policy_digest": _tb_policy_digest(invocation.work_dir, invocation.category),
        "steering_digest": _digest(invocation.steering),
    }


def finalize_review_detail(
    detail: Mapping[str, Any], source_fingerprint: Mapping[str, Any]
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


def review_invocation_changed(
    previous: object,
    current: Mapping[str, Any],
) -> bool:
    """Whether two contracts ask different review questions.

    Scoped source hashes are freshness evidence, not invocation identity: an
    edit to a reviewed file must preserve its finding lifecycle so the next
    review can verify or rediscover it.
    """
    if not isinstance(previous, Mapping):
        return True

    def identity(contract: Mapping[str, Any]) -> dict[str, Any]:
        return {
            key: value
            for key, value in contract.items()
            if key not in _FRESHNESS_ONLY_CONTRACT_FIELDS
        }

    return identity(previous) != identity(current)


def _persisted_document_changed(contract: Mapping[str, Any], name: str) -> bool:
    raw_path = contract.get(f"{name}_source")
    path = Path(raw_path) if isinstance(raw_path, str) and raw_path else None
    if path is not None and not path.is_file():
        raise ReviewContextError(f"Could not read persisted Reviewer context: {path}")
    return contract.get(f"{name}_digest") != _document_digest(path)


def review_receipt_drift(detail: Mapping[str, Any], work_dir: Path) -> list[str]:
    """Return changed dimensions for a persisted source-scoped receipt."""
    if detail.get("review_detail_version") != REVIEW_DETAIL_VERSION:
        # Pre-v4 receipts continue through the legacy source-fingerprint path
        # in criteria_acceptance. Upgrading Booley must not discard otherwise
        # current findings and explicit waivers merely because their persisted
        # representation predates the source-scoped contract.
        return []
    contract = detail.get("contract")
    if not isinstance(contract, Mapping) or contract.get("version") != REVIEW_DETAIL_VERSION:
        return ["contract_version"]
    changed = [
        name
        for name in ("ticket", "spec", "decisions")
        if _persisted_document_changed(contract, name)
    ]
    raw_scope = contract.get("scope")
    scope = (
        tuple(item for item in raw_scope if isinstance(item, str))
        if isinstance(raw_scope, list)
        else ()
    )
    if contract.get("scope_hashes") != _scope_hashes(work_dir, scope):
        changed.append("scope")
    category = contract.get("category")
    category = category if isinstance(category, str) else ""
    if contract.get("tb_policy_digest") != _tb_policy_digest(work_dir, category):
        changed.append("tb_policy")
    return changed

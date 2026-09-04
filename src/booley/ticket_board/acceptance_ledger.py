"""Durable acceptance evidence for Ticket lifecycle transitions.

Live ``booley_state.json`` remains useful for execution and display, but an
accepted Ticket is represented by a content-addressed snapshot outside the
runtime directory.  The small interface here owns persistence, integrity
checking, idempotency, and honest missing/corrupt results.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from booley.criteria.state import CriterionChange, DevelopmentState
from booley.runtime.timefmt import utc_now_rfc3339

from .persistence import WriteOnceConflictError, atomic_write_once

SCHEMA_VERSION = 1


class AcceptanceLedgerError(RuntimeError):
    """Durable acceptance evidence could not be written safely."""


@dataclass(frozen=True)
class AcceptanceSnapshot:
    """One immutable accepted projection of a Ticket's Criteria."""

    digest: str
    slug: str
    ticket_type: str
    execution_id: str
    accepted_at: str
    acceptance_basis: dict[str, Any]
    criteria: dict[str, dict[str, Any]]
    evidence: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class AcceptanceReadResult:
    """Lifecycle reader result that never turns missing evidence into false."""

    kind: Literal["accepted", "unavailable", "corrupt"]
    snapshot: AcceptanceSnapshot | None = None
    reason: str = ""


@dataclass(frozen=True)
class EvidenceRef:
    """Stable reference to one normalized Criterion observation."""

    sequence: int
    digest: str
    criterion: str
    role: Literal["baseline", "candidate"]


def _canonical(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _write_once(path: Path, content: bytes) -> None:
    """Atomically create *path*, accepting an identical existing value."""
    try:
        atomic_write_once(path, content)
    except WriteOnceConflictError as exc:
        raise AcceptanceLedgerError(f"conflicting acceptance record: {path}") from exc


def _snapshot_from_payload(payload: Mapping[str, Any], digest: str) -> AcceptanceSnapshot:
    try:
        return AcceptanceSnapshot(
            digest=digest,
            slug=str(payload["slug"]),
            ticket_type=str(payload["ticket_type"]),
            execution_id=str(payload["execution_id"]),
            accepted_at=str(payload["accepted_at"]),
            acceptance_basis=dict(payload.get("acceptance_basis") or {}),
            criteria={key: dict(value) for key, value in dict(payload["criteria"]).items()},
            evidence=tuple(dict(value) for value in payload.get("evidence", [])),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise AcceptanceLedgerError(f"invalid acceptance snapshot: {exc}") from exc


def _reserve_sequence(root: Path) -> tuple[int, Path]:
    """Atomically reserve the next bounded evidence sequence directory."""
    root.mkdir(parents=True, exist_ok=True)
    for sequence in range(1, 1_000_001):
        directory = root / f"{sequence:09d}"
        try:
            directory.mkdir()
        except FileExistsError:
            continue
        return sequence, directory
    raise AcceptanceLedgerError(f"acceptance evidence sequence exhausted beneath {root}")


def _read_evidence_records(log_dir: Path) -> list[dict[str, Any]]:
    """Read and structurally validate every immutable observation."""
    root = Path(log_dir) / "acceptance" / "evidence"
    if not root.exists():
        return []
    records: list[dict[str, Any]] = []
    for directory in sorted(path for path in root.iterdir() if path.is_dir()):
        try:
            sequence = int(directory.name)
            payload = json.loads((directory / "record.json").read_text(encoding="utf-8"))
            if payload.get("sequence") != sequence:
                raise ValueError("record sequence does not match its directory")
            criterion = payload["criterion"]
            role = payload["role"]
            if not isinstance(criterion, str) or role not in {"baseline", "candidate"}:
                raise ValueError("record has invalid criterion identity or role")
        except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise AcceptanceLedgerError(f"corrupt acceptance evidence {directory}: {exc}") from exc
        records.append(payload)
    return records


def _read_evidence_refs(log_dir: Path) -> list[dict[str, Any]]:
    """Return integrity-checked references to every immutable observation."""
    return [
        {
            "sequence": payload["sequence"],
            "digest": hashlib.sha256(_canonical(payload)).hexdigest(),
            "criterion": payload["criterion"],
            "role": payload["role"],
        }
        for payload in _read_evidence_records(log_dir)
    ]


def _validate_state_projection(log_dir: Path, state: DevelopmentState) -> None:
    """Reject mutable Criterion values that conflict with ledger-observed values."""
    latest: dict[str, dict[str, Any]] = {}
    for payload in _read_evidence_records(log_dir):
        latest[payload["criterion"]] = payload
    for criterion, payload in latest.items():
        entry = state.criteria.get(criterion)
        if entry is None or entry.met is not payload.get("met"):
            raise AcceptanceLedgerError(
                f"mutable Criterion {criterion!r} disagrees with its latest acceptance evidence"
            )


def record_changes(
    log_dir: Path,
    state: DevelopmentState,
    changes: list[CriterionChange],
    *,
    invocation_id: str,
    producer: str,
    execution_id: str,
    acceptance_basis: Mapping[str, Any] | None = None,
    recorded_at: str | None = None,
) -> tuple[EvidenceRef, ...]:
    """Persist normalized effective Criterion changes in completion order."""
    evidence_root = Path(log_dir) / "acceptance" / "evidence"
    refs: list[EvidenceRef] = []
    timestamp = recorded_at or utc_now_rfc3339()
    for change in changes:
        role: Literal["baseline", "candidate"] = (
            "baseline"
            if change.params.get("from_state") == "fail" and not change.met
            else "candidate"
        )
        sequence, directory = _reserve_sequence(evidence_root)
        payload = {
            "schema": SCHEMA_VERSION,
            "sequence": sequence,
            "ticket": state.slug,
            "execution_id": execution_id,
            "purpose": "ticket_acceptance",
            "producer": producer,
            "invocation_id": invocation_id,
            "role": role,
            "criterion": change.key,
            "met": change.met,
            "reason": change.reason,
            "mandatory": change.mandatory,
            "params": change.params,
            "detail": change.detail,
            "acceptance_basis": dict(acceptance_basis or {}),
            "recorded_at": timestamp,
        }
        encoded = _canonical(payload)
        digest = hashlib.sha256(encoded).hexdigest()
        _write_once(directory / "record.json", encoded + b"\n")
        refs.append(EvidenceRef(sequence, digest, change.key, role))
    return tuple(refs)


def freeze_acceptance(
    log_dir: Path,
    state: DevelopmentState,
    *,
    execution_id: str,
    acceptance_basis: Mapping[str, Any] | None,
    accepted_at: str | None = None,
) -> AcceptanceSnapshot:
    """Freeze and select one accepted snapshot for the current Ticket epoch."""
    _validate_state_projection(log_dir, state)
    payload = {
        "schema": SCHEMA_VERSION,
        "slug": state.slug,
        "ticket_type": state.ticket_type,
        "execution_id": execution_id,
        "accepted_at": accepted_at or utc_now_rfc3339(),
        "acceptance_basis": dict(acceptance_basis or {}),
        "criteria": {key: entry.to_dict() for key, entry in state.criteria.items()},
        "evidence": _read_evidence_refs(log_dir),
    }
    encoded = _canonical(payload)
    digest = hashlib.sha256(encoded).hexdigest()
    root = Path(log_dir) / "acceptance"
    _write_once(root / "snapshots" / f"{digest}.json", encoded + b"\n")
    reference = _canonical(
        {
            "schema": SCHEMA_VERSION,
            "snapshot_digest": digest,
            "execution_id": execution_id,
        }
    )
    _write_once(root / "accepted.json", reference + b"\n")
    return _snapshot_from_payload(payload, digest)


def bind_review_package(log_dir: Path, snapshot: AcceptanceSnapshot) -> bool:
    """Bind an already verified review package to its accepted snapshot."""
    root = Path(log_dir)
    manifest_path = root / ".runtime" / "triage-prep" / "manifest.json"
    if not manifest_path.exists():
        return False
    try:
        manifest_bytes = manifest_path.read_bytes()
        manifest = json.loads(manifest_bytes)
        if manifest.get("status") != "ready":
            raise ValueError("review package manifest is not ready")
        briefing_path = Path(manifest["briefing_path"])
        briefing_bytes = briefing_path.read_bytes()
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise AcceptanceLedgerError(f"cannot bind review package: {exc}") from exc
    binding = _canonical(
        {
            "schema": SCHEMA_VERSION,
            "snapshot_digest": snapshot.digest,
            "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
            "briefing_sha256": hashlib.sha256(briefing_bytes).hexdigest(),
        }
    )
    _write_once(root / "acceptance" / "review-package.json", binding + b"\n")
    return True


def validate_review_package_binding(log_dir: Path, snapshot: AcceptanceSnapshot) -> None:
    """Verify a present package binding still names unchanged artifacts."""
    root = Path(log_dir)
    binding_path = root / "acceptance" / "review-package.json"
    if not binding_path.exists():
        return
    manifest_path = root / ".runtime" / "triage-prep" / "manifest.json"
    try:
        binding = json.loads(binding_path.read_text(encoding="utf-8"))
        manifest_bytes = manifest_path.read_bytes()
        manifest = json.loads(manifest_bytes)
        briefing_bytes = Path(manifest["briefing_path"]).read_bytes()
        actual = {
            "snapshot_digest": snapshot.digest,
            "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
            "briefing_sha256": hashlib.sha256(briefing_bytes).hexdigest(),
        }
        if any(binding.get(key) != value for key, value in actual.items()):
            raise ValueError("bound review artifacts changed")
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise AcceptanceLedgerError(f"invalid review package binding: {exc}") from exc


def read_acceptance(log_dir: Path) -> AcceptanceReadResult:
    """Read and integrity-check the accepted snapshot for one Ticket."""
    root = Path(log_dir) / "acceptance"
    reference_path = root / "accepted.json"
    if not reference_path.exists():
        return AcceptanceReadResult("unavailable", reason="accepted snapshot is unavailable")
    try:
        reference = json.loads(reference_path.read_text(encoding="utf-8"))
        digest = reference["snapshot_digest"]
        if not isinstance(digest, str) or len(digest) != 64:
            raise ValueError("accepted reference has an invalid digest")
        payload = json.loads((root / "snapshots" / f"{digest}.json").read_text(encoding="utf-8"))
        actual = hashlib.sha256(_canonical(payload)).hexdigest()
        if actual != digest:
            raise ValueError("accepted snapshot digest mismatch")
        return AcceptanceReadResult("accepted", _snapshot_from_payload(payload, digest))
    except (
        AcceptanceLedgerError,
        KeyError,
        OSError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        return AcceptanceReadResult("corrupt", reason=str(exc))

"""Durable Criterion evidence and Criteria Satisfaction Records.

Live ``booley_state.json`` remains useful for execution and display, but an
accepted Ticket is represented by a content-addressed snapshot outside the
runtime directory.  The small interface here owns persistence, integrity
checking, idempotency, and honest missing/corrupt results.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
from collections.abc import Callable, Mapping
from contextlib import suppress
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from booley.criteria.state import CriterionChange, DevelopmentState
from booley.runtime.file_lock import release_file_lock, wait_for_file_lock
from booley.runtime.timefmt import utc_now_rfc3339

from .persistence import WriteOnceConflictError, atomic_replace_bytes, atomic_write_once

SCHEMA_VERSION = 1


class AcceptanceLedgerError(RuntimeError):
    """Durable Criterion evidence could not be written safely."""


@dataclass(frozen=True)
class AcceptanceSnapshot:
    """One immutable accepted projection of a Ticket's Criteria."""

    digest: str
    slug: str
    ticket_type: str
    execution_id: str
    accepted_at: str
    ticket_identity: dict[str, Any]
    participant_heads: dict[str, str]
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


@dataclass(frozen=True)
class AcceptanceTransaction:
    """One verified and selected V2 Acceptance Journal transaction."""

    transaction_id: str
    evidence: tuple[EvidenceRef, ...]


_HEX_32 = re.compile(r"[0-9a-f]{32}")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_V2_SCHEMA = "booley.acceptance-record/v2"
_COMMIT_SCHEMA = "booley.acceptance-transaction/v1"
_ENVELOPE_SCHEMA = "booley.acceptance-transaction-envelope/v1"
_INTENT_SCHEMA = "booley.simulation-acceptance-intent/v1"
_ROLE_DERIVATION = "booley.acceptance-role/v1"
_MAX_CHANGES = 4_000
_MAX_RECORD_BYTES = 1 << 20
_MAX_DOCUMENT_BYTES = 16 << 20
_MAX_INTENT_BYTES = 32 << 20
_TEMP_PATTERN = re.compile(
    r"\.tmp\.acceptance\.tx-([0-9a-f]{64})\.ord-([0-9]{8})\."
    r"seq-([0-9]{9})\.nonce-([0-9a-f]{32})"
)


def _canonical(value: Mapping[str, Any]) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode(
            "utf-8"
        )
    except (TypeError, ValueError) as exc:
        raise AcceptanceLedgerError(f"acceptance value is not canonical JSON: {exc}") from exc


def _write_once(path: Path, content: bytes) -> None:
    """Atomically create *path*, accepting an identical existing value."""
    try:
        atomic_write_once(path, content)
    except WriteOnceConflictError as exc:
        raise AcceptanceLedgerError(f"conflicting acceptance record: {path}") from exc


def _digest(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _exact_fields(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise AcceptanceLedgerError(f"{label} has unexpected fields")


def _json_value(value: Any) -> Any:
    """Copy immutable mapping/tuple codecs into plain canonical JSON values."""
    if isinstance(value, Mapping):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _canonical_timestamp(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.endswith("Z") or "." in value:
        raise AcceptanceLedgerError(f"{label} must be canonical UTC RFC3339")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise AcceptanceLedgerError(f"{label} must be canonical UTC RFC3339") from exc
    if parsed.strftime("%Y-%m-%dT%H:%M:%SZ") != value:
        raise AcceptanceLedgerError(f"{label} must be canonical UTC RFC3339")
    return value


def _campaign_uuid(value: object) -> str:
    if not isinstance(value, str):
        raise AcceptanceLedgerError("campaign_id must be a lowercase UUIDv4")
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise AcceptanceLedgerError("campaign_id must be a lowercase UUIDv4") from exc
    if parsed.version != 4 or str(parsed) != value:
        raise AcceptanceLedgerError("campaign_id must be a lowercase UUIDv4")
    return value


def _sha256_field(value: object, label: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise AcceptanceLedgerError(f"{label} must be a SHA-256 digest")
    return value


def _identity(value: object, generation: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise AcceptanceLedgerError("ticket identity must be an object")
    identity = _json_value(value)
    if (
        not isinstance(generation, str)
        or _HEX_32.fullmatch(generation) is None
        or identity.get("generation") != generation
    ):
        raise AcceptanceLedgerError("ticket generation must match the canonical identity")
    _canonical(identity)
    return identity


def _transaction_role(change: CriterionChange) -> Literal["baseline", "candidate"]:
    return (
        "baseline" if change.params.get("from_state") == "fail" and not change.met else "candidate"
    )


def _change_payload(change: CriterionChange) -> dict[str, Any]:
    if not change.key:
        raise AcceptanceLedgerError("transaction Criterion key must not be empty")
    return {
        "key": change.key,
        "met": change.met,
        "reason": change.reason,
        "mandatory": change.mandatory,
        "params": change.params,
        "detail": change.detail,
        "role": _transaction_role(change),
    }


def _recorded_at(facts: Mapping[str, Any]) -> str:
    consumed = facts.get("consumed_results")
    if not isinstance(consumed, list) or not consumed:
        raise AcceptanceLedgerError("acceptance facts require consumed terminal results")
    if len(consumed) > 1_000:
        raise AcceptanceLedgerError("acceptance facts contain too many consumed results")
    timestamps: list[str] = []
    for item in consumed:
        if not isinstance(item, Mapping):
            raise AcceptanceLedgerError("consumed result must be an object")
        timestamps.append(_canonical_timestamp(item.get("finished_at"), "finished_at"))
    return max(timestamps)


def _acceptance_fact_identity(
    acceptance_facts: Mapping[str, Any],
) -> tuple[dict[str, Any], str, str, dict[str, Any], str]:
    facts = _json_value(acceptance_facts)
    _exact_fields(
        facts,
        {
            "$schema",
            "campaign_id",
            "manifest_sha256",
            "origin",
            "target",
            "required_suite",
            "prerequisites",
            "consumed_results",
            "observations",
            "coverage_reference",
        },
        "Simulation acceptance facts",
    )
    if facts.get("$schema") != "booley.simulation-acceptance-facts/v1":
        raise AcceptanceLedgerError("unsupported Simulation acceptance facts schema")
    campaign_id = _campaign_uuid(facts.get("campaign_id"))
    manifest_sha256 = _sha256_field(facts.get("manifest_sha256"), "manifest_sha256")
    origin = facts.get("origin")
    if not isinstance(origin, Mapping):
        raise AcceptanceLedgerError("acceptance facts origin must be an object")
    origin_value = dict(origin)
    _exact_fields(origin_value, {"execution_id", "invocation_id"}, "acceptance facts origin")
    execution_id = origin_value["execution_id"]
    invocation_id = origin_value["invocation_id"]
    if not isinstance(execution_id, str) or (
        execution_id and _HEX_32.fullmatch(execution_id) is None
    ):
        raise AcceptanceLedgerError("origin execution_id is invalid")
    if isinstance(invocation_id, bool) or not isinstance(invocation_id, int) or invocation_id < 1:
        raise AcceptanceLedgerError("origin invocation_id must be a positive integer")
    return facts, campaign_id, manifest_sha256, origin_value, _recorded_at(facts)


def _build_transaction_documents(
    state: DevelopmentState,
    changes: list[CriterionChange],
    acceptance_facts: Mapping[str, Any],
    ticket_identity: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], str]:
    if len(changes) > _MAX_CHANGES:
        raise AcceptanceLedgerError("acceptance transaction has too many changes")
    facts, campaign_id, manifest_sha256, origin, recorded_at = _acceptance_fact_identity(
        acceptance_facts
    )
    generation = ticket_identity.get("generation")
    identity = _identity(ticket_identity, generation)
    if not state.slug:
        raise AcceptanceLedgerError("Ticket slug must not be empty")
    envelope = {
        "$schema": _ENVELOPE_SCHEMA,
        "campaign_id": campaign_id,
        "manifest_sha256": manifest_sha256,
        "origin": dict(origin),
        "producer": "simulation_campaign",
        "purpose": "ticket_acceptance",
        "ticket": {"slug": state.slug, "identity": identity, "generation": generation},
        "recorded_at": recorded_at,
        "role_derivation": _ROLE_DERIVATION,
        "changes": [_change_payload(change) for change in changes],
    }
    envelope_bytes = _canonical(envelope)
    if len(envelope_bytes) > _MAX_DOCUMENT_BYTES:
        raise AcceptanceLedgerError("acceptance transaction envelope is too large")
    lookup_key = {
        "campaign_id": campaign_id,
        "manifest_sha256": manifest_sha256,
        "ticket_identity": identity,
        "ticket_generation": generation,
        "producer": "simulation_campaign",
        "purpose": "ticket_acceptance",
    }
    intent = {
        "$schema": _INTENT_SCHEMA,
        "lookup_key": lookup_key,
        "acceptance_facts": facts,
        "acceptance_facts_sha256": _digest(_canonical(facts)),
        "envelope": envelope,
    }
    return lookup_key, intent, hashlib.sha256(envelope_bytes).hexdigest()


def _stable_lookup_key(
    acceptance_facts: Mapping[str, Any], ticket_identity: Mapping[str, Any]
) -> dict[str, Any]:
    _facts, campaign_id, manifest_sha256, _origin, _recorded_at_value = _acceptance_fact_identity(
        acceptance_facts
    )
    generation = ticket_identity.get("generation")
    identity = _identity(ticket_identity, generation)
    return {
        "campaign_id": campaign_id,
        "manifest_sha256": manifest_sha256,
        "ticket_identity": identity,
        "ticket_generation": generation,
        "producer": "simulation_campaign",
        "purpose": "ticket_acceptance",
    }


def _read_json_document(path: Path, limit: int, label: str) -> dict[str, Any]:
    try:
        content = path.read_bytes()
        if len(content) > limit or not content.endswith(b"\n"):
            raise ValueError("invalid size or missing trailing newline")
        value = json.loads(content)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise AcceptanceLedgerError(f"corrupt {label} {path}: {exc}") from exc
    if not isinstance(value, dict) or content != _canonical(value) + b"\n":
        raise AcceptanceLedgerError(f"corrupt {label} {path}: noncanonical JSON")
    return value


def _load_or_publish_intent(
    log_dir: Path, lookup_key: Mapping[str, Any], proposed: Mapping[str, Any]
) -> dict[str, Any]:
    path = _intent_path(log_dir, lookup_key)
    encoded = _canonical(proposed) + b"\n"
    if len(encoded) > _MAX_INTENT_BYTES:
        raise AcceptanceLedgerError("acceptance intent is too large")
    if not path.exists():
        with suppress(WriteOnceConflictError):
            atomic_write_once(path, encoded)
        # A concurrent publisher may win the stable lookup key. Its complete,
        # authenticated intent is authoritative even when mutable inputs drifted.
    return _read_intent(path, lookup_key)


def _intent_path(log_dir: Path, lookup_key: Mapping[str, Any]) -> Path:
    key_digest = hashlib.sha256(_canonical(lookup_key)).hexdigest()
    return log_dir / "acceptance" / "intents" / f"{key_digest}.json"


def _read_intent(path: Path, lookup_key: Mapping[str, Any]) -> dict[str, Any]:
    intent = _read_json_document(path, _MAX_INTENT_BYTES, "acceptance intent")
    _exact_fields(
        intent,
        {"$schema", "lookup_key", "acceptance_facts", "acceptance_facts_sha256", "envelope"},
        "acceptance intent",
    )
    if intent.get("$schema") != _INTENT_SCHEMA or intent.get("lookup_key") != lookup_key:
        raise AcceptanceLedgerError("acceptance intent lookup key mismatch")
    facts = intent.get("acceptance_facts")
    if not isinstance(facts, Mapping) or intent.get("acceptance_facts_sha256") != _digest(
        _canonical(facts)
    ):
        raise AcceptanceLedgerError("acceptance intent facts digest mismatch")
    envelope = intent.get("envelope")
    if not isinstance(envelope, dict):
        raise AcceptanceLedgerError("acceptance intent envelope must be an object")
    _validate_envelope(envelope)
    return intent


def _validate_envelope(envelope: Mapping[str, Any]) -> None:
    _exact_fields(
        envelope,
        {
            "$schema",
            "campaign_id",
            "manifest_sha256",
            "origin",
            "producer",
            "purpose",
            "ticket",
            "recorded_at",
            "role_derivation",
            "changes",
        },
        "acceptance transaction envelope",
    )
    if (
        envelope.get("$schema") != _ENVELOPE_SCHEMA
        or envelope.get("producer") != "simulation_campaign"
        or envelope.get("purpose") != "ticket_acceptance"
        or envelope.get("role_derivation") != _ROLE_DERIVATION
    ):
        raise AcceptanceLedgerError("invalid acceptance transaction envelope identity")
    _campaign_uuid(envelope.get("campaign_id"))
    _sha256_field(envelope.get("manifest_sha256"), "manifest_sha256")
    _canonical_timestamp(envelope.get("recorded_at"), "recorded_at")
    _validate_envelope_origin_and_ticket(envelope)
    _validate_envelope_changes(envelope.get("changes"))


def _validate_envelope_origin_and_ticket(envelope: Mapping[str, Any]) -> None:
    origin = envelope.get("origin")
    ticket = envelope.get("ticket")
    if not isinstance(origin, Mapping) or not isinstance(ticket, Mapping):
        raise AcceptanceLedgerError("invalid acceptance transaction envelope objects")
    _exact_fields(origin, {"execution_id", "invocation_id"}, "transaction origin")
    _exact_fields(ticket, {"slug", "identity", "generation"}, "transaction ticket")
    execution_id, invocation_id = origin.get("execution_id"), origin.get("invocation_id")
    if not isinstance(execution_id, str) or (
        execution_id and _HEX_32.fullmatch(execution_id) is None
    ):
        raise AcceptanceLedgerError("invalid transaction execution_id")
    if isinstance(invocation_id, bool) or not isinstance(invocation_id, int) or invocation_id < 1:
        raise AcceptanceLedgerError("invalid transaction invocation_id")
    if not isinstance(ticket.get("slug"), str) or not ticket["slug"]:
        raise AcceptanceLedgerError("invalid transaction Ticket slug")
    _identity(ticket.get("identity"), ticket.get("generation"))


def _validate_envelope_changes(changes: object) -> None:
    if not isinstance(changes, list) or len(changes) > _MAX_CHANGES:
        raise AcceptanceLedgerError("invalid transaction changes")
    fields = {"key", "met", "reason", "mandatory", "params", "detail", "role"}
    for change in changes:
        if not isinstance(change, Mapping):
            raise AcceptanceLedgerError("transaction change must be an object")
        _exact_fields(change, fields, "transaction change")
        if (
            not isinstance(change.get("key"), str)
            or not change["key"]
            or not isinstance(change.get("met"), bool)
            or not isinstance(change.get("mandatory"), bool)
            or not isinstance(change.get("reason"), str)
            or not isinstance(change.get("params"), dict)
            or not isinstance(change.get("detail"), dict)
            or change.get("role") not in {"baseline", "candidate"}
        ):
            raise AcceptanceLedgerError("invalid transaction change value")


def _v2_record(
    envelope: Mapping[str, Any], transaction_id: str, ordinal: int, sequence: int
) -> dict[str, Any]:
    change = envelope["changes"][ordinal]
    origin = envelope["origin"]
    ticket = envelope["ticket"]
    return {
        "$schema": _V2_SCHEMA,
        "sequence": sequence,
        "transaction_id": transaction_id,
        "transaction_ordinal": ordinal,
        "transaction_size": len(envelope["changes"]),
        "envelope_sha256": _digest(_canonical(envelope)),
        "ticket": ticket["slug"],
        "execution_id": origin["execution_id"],
        "purpose": envelope["purpose"],
        "producer": envelope["producer"],
        "invocation_id": origin["invocation_id"],
        "role": change["role"],
        "criterion": change["key"],
        "met": change["met"],
        "reason": change["reason"],
        "mandatory": change["mandatory"],
        "params": change["params"],
        "detail": change["detail"],
        "ticket_identity": ticket["identity"],
        "recorded_at": envelope["recorded_at"],
    }


def _existing_prefix(
    root: Path, envelope: Mapping[str, Any], transaction_id: str
) -> list[tuple[int, bytes]]:
    found: dict[int, tuple[int, bytes]] = {}
    if not root.exists():
        return []
    suffix = f".tx.{transaction_id}"
    for directory in root.iterdir():
        if not directory.is_dir() or not directory.name.endswith(suffix):
            continue
        sequence_text = directory.name[: -len(suffix)]
        if re.fullmatch(r"[0-9]{9}", sequence_text) is None:
            raise AcceptanceLedgerError("malformed transaction evidence directory")
        record = _read_json_document(directory / "record.json", _MAX_RECORD_BYTES, "V2 record")
        ordinal = record.get("transaction_ordinal")
        if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal in found:
            raise AcceptanceLedgerError("duplicate or invalid transaction ordinal")
        sequence = int(sequence_text)
        expected = (
            _v2_record(envelope, transaction_id, ordinal, sequence)
            if 0 <= ordinal < len(envelope["changes"])
            else None
        )
        content = _canonical(record) + b"\n"
        if expected is None or record != expected:
            raise AcceptanceLedgerError("transaction evidence contradicts its envelope")
        found[ordinal] = (sequence, content)
    if set(found) != set(range(len(found))):
        raise AcceptanceLedgerError("transaction evidence is not a canonical prefix")
    return [found[index] for index in range(len(found))]


def _next_sequence(root: Path) -> int:
    used: set[int] = set()
    if root.exists():
        for path in root.iterdir():
            prefix = path.name.partition(".tx.")[0]
            if re.fullmatch(r"[0-9]{9}", prefix):
                used.add(int(prefix))
    for sequence in range(1, 1_000_001):
        if sequence not in used:
            return sequence
    raise AcceptanceLedgerError(f"Criterion evidence sequence exhausted beneath {root}")


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _publish_v2_record(
    root: Path,
    envelope: Mapping[str, Any],
    transaction_id: str,
    ordinal: int,
) -> tuple[int, bytes]:
    sequence = _next_sequence(root)
    nonce = secrets.token_hex(16)
    temporary = root / (
        f".tmp.acceptance.tx-{transaction_id}.ord-{ordinal:08d}.seq-{sequence:09d}.nonce-{nonce}"
    )
    final = root / f"{sequence:09d}.tx.{transaction_id}"
    record = _v2_record(envelope, transaction_id, ordinal, sequence)
    content = _canonical(record) + b"\n"
    if len(content) > _MAX_RECORD_BYTES:
        raise AcceptanceLedgerError("acceptance transaction record is too large")
    temporary.mkdir()
    try:
        record_path = temporary / "record.json"
        with record_path.open("wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        _fsync_directory(temporary)
        temporary.replace(final)
        _fsync_directory(root)
    except BaseException:
        # A published final is evidence. Only still-hidden staging is disposable.
        if temporary.exists():
            shutil.rmtree(temporary)
        raise
    return sequence, content


def _recover_current_temps(root: Path, envelope: Mapping[str, Any], transaction_id: str) -> None:
    for path in root.iterdir():
        if not path.name.startswith(".tmp.acceptance."):
            continue
        match = _TEMP_PATTERN.fullmatch(path.name)
        if match is None or not path.is_dir():
            raise AcceptanceLedgerError("malformed reserved acceptance temporary path")
        if match.group(1) != transaction_id:
            continue
        ordinal, sequence = int(match.group(2)), int(match.group(3))
        record_path = path / "record.json"
        try:
            content = record_path.read_bytes()
            record = json.loads(content)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            shutil.rmtree(path)
            continue
        expected = (
            _v2_record(envelope, transaction_id, ordinal, sequence)
            if ordinal < len(envelope["changes"])
            else None
        )
        if (
            expected is None
            or not isinstance(record, dict)
            or content != _canonical(record) + b"\n"
            or record != expected
        ):
            raise AcceptanceLedgerError("complete temporary record contradicts its envelope")
        final = root / f"{sequence:09d}.tx.{transaction_id}"
        occupied = [
            candidate
            for candidate in root.iterdir()
            if candidate.name.partition(".tx.")[0] == f"{sequence:09d}" and candidate != path
        ]
        if final.exists():
            if (final / "record.json").read_bytes() != content:
                raise AcceptanceLedgerError("same transaction has conflicting final evidence")
            shutil.rmtree(path)
        elif occupied:
            shutil.rmtree(path)
        else:
            path.replace(final)
            _fsync_directory(root)


def _validate_reserved_temp_names(root: Path) -> None:
    for path in root.iterdir():
        if path.name.startswith(".tmp.acceptance.") and (
            _TEMP_PATTERN.fullmatch(path.name) is None or not path.is_dir()
        ):
            raise AcceptanceLedgerError("malformed reserved acceptance temporary path")


def _commit_document(
    envelope: Mapping[str, Any], transaction_id: str, records: list[tuple[int, bytes]]
) -> dict[str, Any]:
    entries = []
    for ordinal, (sequence, content) in enumerate(records):
        change = envelope["changes"][ordinal]
        entries.append(
            {
                "transaction_ordinal": ordinal,
                "sequence": sequence,
                "sha256": _digest(content),
                "criterion": change["key"],
                "role": change["role"],
            }
        )
    return {
        "$schema": _COMMIT_SCHEMA,
        "transaction_id": transaction_id,
        "envelope": dict(envelope),
        "envelope_sha256": _digest(_canonical(envelope)),
        "record_count": len(records),
        "records": entries,
    }


def _verify_commit(
    evidence_root: Path, path: Path, transaction_id: str
) -> tuple[dict[str, Any], tuple[EvidenceRef, ...]]:
    commit = _read_json_document(path, _MAX_DOCUMENT_BYTES, "acceptance transaction")
    _exact_fields(
        commit,
        {"$schema", "transaction_id", "envelope", "envelope_sha256", "record_count", "records"},
        "acceptance transaction",
    )
    envelope = commit.get("envelope")
    records = commit.get("records")
    if (
        commit.get("$schema") != _COMMIT_SCHEMA
        or commit.get("transaction_id") != transaction_id
        or not isinstance(envelope, dict)
        or commit.get("envelope_sha256") != _digest(_canonical(envelope))
        or not isinstance(records, list)
        or isinstance(commit.get("record_count"), bool)
        or commit.get("record_count") != len(records)
        or len(records) != len(envelope.get("changes", []))
    ):
        raise AcceptanceLedgerError("invalid acceptance transaction manifest")
    _validate_envelope(envelope)
    if hashlib.sha256(_canonical(envelope)).hexdigest() != transaction_id:
        raise AcceptanceLedgerError("acceptance transaction ID does not match its envelope")
    refs = _verify_committed_records(evidence_root, envelope, transaction_id, records)
    _verify_no_extra_transaction_records(evidence_root, transaction_id, records)
    return commit, refs


def _verify_committed_records(
    evidence_root: Path,
    envelope: Mapping[str, Any],
    transaction_id: str,
    records: list[Any],
) -> tuple[EvidenceRef, ...]:
    refs: list[EvidenceRef] = []
    for ordinal, entry in enumerate(records):
        if not isinstance(entry, Mapping):
            raise AcceptanceLedgerError("transaction record reference must be an object")
        _exact_fields(
            entry,
            {"transaction_ordinal", "sequence", "sha256", "criterion", "role"},
            "transaction record reference",
        )
        sequence = entry.get("sequence")
        if (
            entry.get("transaction_ordinal") != ordinal
            or isinstance(sequence, bool)
            or not isinstance(sequence, int)
            or sequence < 1
        ):
            raise AcceptanceLedgerError("invalid transaction record reference position")
        path = evidence_root / f"{sequence:09d}.tx.{transaction_id}" / "record.json"
        record = _read_json_document(path, _MAX_RECORD_BYTES, "V2 record")
        content = _canonical(record) + b"\n"
        expected = _v2_record(envelope, transaction_id, ordinal, sequence)
        if (
            record != expected
            or entry.get("sha256") != _digest(content)
            or entry.get("criterion") != expected["criterion"]
            or entry.get("role") != expected["role"]
        ):
            raise AcceptanceLedgerError("transaction manifest does not bind its record")
        refs.append(
            EvidenceRef(
                sequence, hashlib.sha256(content).hexdigest(), record["criterion"], record["role"]
            )
        )
    return tuple(refs)


def _verify_no_extra_transaction_records(
    evidence_root: Path, transaction_id: str, records: list[Any]
) -> None:
    bound = {f"{entry['sequence']:09d}.tx.{transaction_id}" for entry in records}
    actual = {
        path.name
        for path in evidence_root.iterdir()
        if path.is_dir() and path.name.endswith(f".tx.{transaction_id}")
    }
    if actual != bound:
        raise AcceptanceLedgerError("transaction has evidence outside its commit manifest")


def _commit_matches_lookup(commit: Mapping[str, Any], lookup_key: Mapping[str, Any]) -> bool:
    envelope = commit["envelope"]
    ticket = envelope["ticket"]
    return {
        "campaign_id": envelope["campaign_id"],
        "manifest_sha256": envelope["manifest_sha256"],
        "ticket_identity": ticket["identity"],
        "ticket_generation": ticket["generation"],
        "producer": envelope["producer"],
        "purpose": envelope["purpose"],
    } == lookup_key


def _matching_commits(
    log_dir: Path, evidence_root: Path, lookup_key: Mapping[str, Any]
) -> list[tuple[str, dict[str, Any], tuple[EvidenceRef, ...]]]:
    root = log_dir / "acceptance" / "transactions"
    if not root.exists():
        return []
    matching = []
    for path in root.iterdir():
        if not path.is_file() or re.fullmatch(r"[0-9a-f]{64}\.json", path.name) is None:
            raise AcceptanceLedgerError("malformed acceptance transaction path")
        transaction_id = path.stem
        commit, refs = _verify_commit(evidence_root, path, transaction_id)
        if _commit_matches_lookup(commit, lookup_key):
            matching.append((transaction_id, commit, refs))
    return matching


def _legacy_transaction_records(root: Path, transaction_id: str) -> list[dict[str, Any]]:
    records = []
    suffix = f".tx.{transaction_id}"
    for directory in root.iterdir():
        if not directory.is_dir() or not directory.name.endswith(suffix):
            continue
        payload = _read_legacy_record(directory)
        if payload.get("schema") != SCHEMA_VERSION or "$schema" in payload:
            raise AcceptanceLedgerError("selected legacy transaction contains non-V1 evidence")
        records.append(payload)
    if not records:
        raise AcceptanceLedgerError("selected legacy transaction has no evidence")
    return records


def _read_legacy_record(directory: Path) -> dict[str, Any]:
    try:
        payload = json.loads((directory / "record.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AcceptanceLedgerError(f"corrupt Criterion evidence {directory}: {exc}") from exc
    if not isinstance(payload, dict):
        raise AcceptanceLedgerError(
            f"corrupt Criterion evidence {directory}: record is not an object"
        )
    sequence_name = directory.name.partition(".tx.")[0]
    if payload.get("sequence") != int(sequence_name):
        raise AcceptanceLedgerError(
            f"corrupt Criterion evidence {directory}: record sequence does not match its directory"
        )
    return payload


def _active_evidence_records(
    log_dir: Path, state: DevelopmentState, ticket_identity: Mapping[str, Any]
) -> list[dict[str, Any]]:
    root = log_dir / "acceptance" / "evidence"
    if not root.exists():
        if state.acceptance_transactions:
            raise AcceptanceLedgerError("selected acceptance transaction has no evidence root")
        return []
    records: list[dict[str, Any]] = []
    selected = set(state.acceptance_transactions)
    commits_root = log_dir / "acceptance" / "transactions"
    for transaction_id in selected:
        commit_path = commits_root / f"{transaction_id}.json"
        if commit_path.exists():
            commit, _ = _verify_commit(root, commit_path, transaction_id)
            for entry in commit["records"]:
                directory = root / f"{entry['sequence']:09d}.tx.{transaction_id}"
                records.append(
                    _read_json_document(directory / "record.json", _MAX_RECORD_BYTES, "V2 record")
                )
        else:
            records.extend(_legacy_transaction_records(root, transaction_id))
    records.extend(_plain_legacy_records(root))
    records.sort(key=lambda item: item["sequence"])
    seen: set[int] = set()
    for payload in records:
        sequence = payload.get("sequence")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence in seen:
            raise AcceptanceLedgerError("active Criterion evidence repeats a sequence")
        seen.add(sequence)
        if payload.get("ticket_identity") != ticket_identity:
            raise AcceptanceLedgerError("Criterion evidence names another Ticket identity")
    return records


def _plain_legacy_records(root: Path) -> list[dict[str, Any]]:
    records = []
    for directory in root.iterdir():
        if directory.name.startswith(".tmp.acceptance.") or not directory.is_dir():
            continue
        sequence_name, separator, _transaction = directory.name.partition(".tx.")
        if separator:
            continue
        if re.fullmatch(r"[0-9]{9}", sequence_name) is None:
            raise AcceptanceLedgerError("malformed Criterion evidence directory")
        payload = _read_legacy_record(directory)
        if payload.get("schema") != SCHEMA_VERSION or "$schema" in payload:
            raise AcceptanceLedgerError("plain Criterion evidence is not V1")
        records.append(payload)
    return records


def _replay_projection(
    log_dir: Path, state: DevelopmentState, ticket_identity: Mapping[str, Any]
) -> None:
    for payload in _active_evidence_records(log_dir, state, ticket_identity):
        criterion = payload.get("criterion")
        if not isinstance(criterion, str) or criterion not in state.criteria:
            raise AcceptanceLedgerError("Criterion evidence names an undeclared Criterion")
        entry = state.criteria[criterion]
        met = payload.get("met")
        if not isinstance(met, bool):
            raise AcceptanceLedgerError("Criterion evidence has invalid met value")
        entry.met = met
        entry.mandatory = payload.get("mandatory", entry.mandatory)
        entry.params = dict(payload.get("params") or {})
        entry.detail = dict(payload.get("detail") or {})
        entry.ever_met = entry.ever_met or met
        entry.ever_failed = entry.ever_failed or not met


def _update_live_state(state: DevelopmentState, saved: DevelopmentState) -> None:
    state.slug = saved.slug
    state.ticket_type = saved.ticket_type
    state.strict_criteria = saved.strict_criteria
    state.criteria = saved.criteria
    state.category_map = saved.category_map
    state.flow_key_aliases = saved.flow_key_aliases
    state.timeline = saved.timeline
    state.work_dir = saved.work_dir
    state.last_updated = saved.last_updated
    state.acceptance_transactions = saved.acceptance_transactions
    state.authorized_zero_mandatory_basis_id = saved.authorized_zero_mandatory_basis_id


def _select_transaction(
    log_dir: Path,
    state: DevelopmentState,
    transaction_id: str,
    ticket_identity: Mapping[str, Any],
) -> None:
    shadow = deepcopy(state)
    if transaction_id not in shadow.acceptance_transactions:
        shadow.acceptance_transactions.append(transaction_id)
    _replay_projection(log_dir, shadow, ticket_identity)
    shadow.save()
    if shadow._file_path is not None:
        saved = DevelopmentState.load(shadow._file_path)
        if transaction_id not in saved.acceptance_transactions:
            raise AcceptanceLedgerError("saved state did not select acceptance transaction")
        _replay_projection(log_dir, saved, ticket_identity)
    else:
        saved = shadow
    _update_live_state(state, saved)


def _reconcile_transaction_locked(
    root: Path,
    evidence_root: Path,
    state: DevelopmentState,
    lookup_key: Mapping[str, Any],
    envelope: Mapping[str, Any],
    transaction_id: str,
    ticket_identity: Mapping[str, Any],
    publication_checkpoint: Callable[[str], None],
) -> AcceptanceTransaction:
    _validate_reserved_temp_names(evidence_root)
    matching = _matching_commits(root, evidence_root, lookup_key)
    if len(matching) > 1:
        raise AcceptanceLedgerError("multiple transactions match one acceptance intent")
    if matching:
        found_id, _commit, refs = matching[0]
        if found_id in state.acceptance_transactions:
            _validate_state_projection(root, state, ticket_identity)
        else:
            publication_checkpoint("before:acceptance_state")
            _select_transaction(root, state, found_id, ticket_identity)
            publication_checkpoint("after:acceptance_state")
        return AcceptanceTransaction(found_id, refs)
    _recover_current_temps(evidence_root, envelope, transaction_id)
    prefix = _existing_prefix(evidence_root, envelope, transaction_id)
    for ordinal in range(len(prefix), len(envelope["changes"])):
        publication_checkpoint("before:acceptance_record")
        prefix.append(_publish_v2_record(evidence_root, envelope, transaction_id, ordinal))
        publication_checkpoint("after:acceptance_record")
    commit = _commit_document(envelope, transaction_id, prefix)
    commit_bytes = _canonical(commit) + b"\n"
    if len(commit_bytes) > _MAX_DOCUMENT_BYTES:
        raise AcceptanceLedgerError("acceptance transaction manifest is too large")
    commit_path = root / "acceptance" / "transactions" / f"{transaction_id}.json"
    publication_checkpoint("before:acceptance_commit")
    _write_once(commit_path, commit_bytes)
    publication_checkpoint("after:acceptance_commit")
    _verified, refs = _verify_commit(evidence_root, commit_path, transaction_id)
    publication_checkpoint("before:acceptance_state")
    _select_transaction(root, state, transaction_id, ticket_identity)
    publication_checkpoint("after:acceptance_state")
    return AcceptanceTransaction(transaction_id, refs)


def record_or_verify_transaction(
    log_dir: Path,
    state: DevelopmentState,
    changes: list[CriterionChange],
    *,
    acceptance_facts: Mapping[str, Any],
    ticket_identity: Mapping[str, Any],
    publication_checkpoint: Callable[[str], None] | None = None,
) -> AcceptanceTransaction:
    """Record or verify one intent-frozen Simulation Campaign transaction."""
    root = Path(log_dir)
    lookup_key = _stable_lookup_key(acceptance_facts, ticket_identity)
    checkpoint = publication_checkpoint or (lambda _boundary: None)
    intent_path = _intent_path(root, lookup_key)
    if intent_path.exists():
        intent = _read_intent(intent_path, lookup_key)
    else:
        proposed_lookup, proposed_intent, _proposed_id = _build_transaction_documents(
            state, changes, acceptance_facts, ticket_identity
        )
        if proposed_lookup != lookup_key:
            raise AssertionError("acceptance lookup derivation disagrees")
        checkpoint("before:acceptance_intent")
        intent = _load_or_publish_intent(root, lookup_key, proposed_intent)
        checkpoint("after:acceptance_intent")
    envelope = intent["envelope"]
    transaction_id = hashlib.sha256(_canonical(envelope)).hexdigest()
    evidence_root = root / "acceptance" / "evidence"
    evidence_root.mkdir(parents=True, exist_ok=True)
    lock_path = evidence_root / ".sequence.lock"
    with lock_path.open("a+", encoding="utf-8") as handle:
        wait_for_file_lock(handle, timeout_s=5)
        try:
            return _reconcile_transaction_locked(
                root,
                evidence_root,
                state,
                lookup_key,
                envelope,
                transaction_id,
                ticket_identity,
                checkpoint,
            )
        finally:
            release_file_lock(handle)


def _snapshot_from_payload(payload: Mapping[str, Any], digest: str) -> AcceptanceSnapshot:
    if "acceptance_basis" in payload:
        raise AcceptanceLedgerError("unsupported Ticket format: recreate this Ticket")
    try:
        return AcceptanceSnapshot(
            digest=digest,
            slug=str(payload["slug"]),
            ticket_type=str(payload["ticket_type"]),
            execution_id=str(payload["execution_id"]),
            accepted_at=str(payload["accepted_at"]),
            ticket_identity=dict(payload.get("ticket_identity") or {}),
            participant_heads=_participant_heads(payload["participant_heads"]),
            criteria={key: dict(value) for key, value in dict(payload["criteria"]).items()},
            evidence=tuple(dict(value) for value in payload.get("evidence", [])),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise AcceptanceLedgerError(f"invalid Criteria Satisfaction Record: {exc}") from exc


def _participant_heads(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise AcceptanceLedgerError("participant_heads must be a mapping")
    heads = dict(value)
    if not set(heads) <= {"outer", "project"} or any(
        not isinstance(commit, str) or re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit) is None
        for commit in heads.values()
    ):
        raise AcceptanceLedgerError("participant_heads contains an invalid commit identity")
    if "outer" not in heads:
        raise AcceptanceLedgerError("participant_heads requires an outer commit identity")
    return heads


def _reserve_sequence(root: Path, transaction_id: str = "") -> tuple[int, Path]:
    """Atomically reserve the next bounded evidence sequence directory."""
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".sequence.lock").open("a+", encoding="utf-8") as handle:
        wait_for_file_lock(handle, timeout_s=5)
        try:
            return _allocate_sequence(root, transaction_id)
        finally:
            release_file_lock(handle)


def _allocate_sequence(root: Path, transaction_id: str) -> tuple[int, Path]:
    used = {path.name.partition(".tx.")[0] for path in root.iterdir()}
    for sequence in range(1, 1_000_001):
        name = f"{sequence:09d}"
        if name in used:
            continue
        directory = root / (f"{name}.tx.{transaction_id}" if transaction_id else name)
        try:
            directory.mkdir()
        except FileExistsError:
            continue
        return sequence, directory
    raise AcceptanceLedgerError(f"Criterion evidence sequence exhausted beneath {root}")


def _read_evidence_records(
    log_dir: Path, state: DevelopmentState, ticket_identity: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Read and structurally validate every immutable observation."""
    records = _active_evidence_records(Path(log_dir), state, ticket_identity)
    for payload in records:
        try:
            if "acceptance_basis" in payload:
                raise ValueError("unsupported Ticket format: recreate this Ticket")
            criterion = payload["criterion"]
            role = payload["role"]
            if not isinstance(criterion, str) or role not in {"baseline", "candidate"}:
                raise ValueError("record has invalid criterion identity or role")
            if payload.get("ticket_identity") != ticket_identity:
                raise ValueError("Criterion evidence names another Ticket identity")
        except (KeyError, TypeError, ValueError) as exc:
            raise AcceptanceLedgerError(f"corrupt Criterion evidence: {exc}") from exc
    return records


def _read_evidence_refs(
    log_dir: Path, state: DevelopmentState, ticket_identity: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Return integrity-checked references to every immutable observation."""
    return [
        {
            "sequence": payload["sequence"],
            "digest": hashlib.sha256(_canonical(payload)).hexdigest(),
            "criterion": payload["criterion"],
            "role": payload["role"],
        }
        for payload in _read_evidence_records(log_dir, state, ticket_identity)
    ]


def _validate_state_projection(
    log_dir: Path, state: DevelopmentState, ticket_identity: Mapping[str, Any]
) -> None:
    """Reject mutable Criterion values that conflict with ledger-observed values."""
    latest: dict[str, dict[str, Any]] = {}
    for payload in _read_evidence_records(log_dir, state, ticket_identity):
        latest[payload["criterion"]] = payload
    for criterion, payload in latest.items():
        entry = state.criteria.get(criterion)
        agrees = entry is not None and (
            entry.met is payload.get("met")
            and entry.mandatory is payload.get("mandatory")
            and entry.params == payload.get("params")
            and entry.detail == payload.get("detail")
        )
        if not agrees:
            raise AcceptanceLedgerError(
                f"mutable Criterion {criterion!r} disagrees with its latest evidence"
            )


def record_changes(
    log_dir: Path,
    state: DevelopmentState,
    changes: list[CriterionChange],
    *,
    invocation_id: str,
    producer: str,
    execution_id: str,
    ticket_identity: Mapping[str, Any] | None = None,
    recorded_at: str | None = None,
    transaction_id: str = "",
) -> tuple[EvidenceRef, ...]:
    """Persist normalized effective Criterion changes in completion order."""
    if transaction_id and re.fullmatch(r"[0-9a-f]{64}", transaction_id) is None:
        raise AcceptanceLedgerError("invalid acceptance transaction identity")
    evidence_root = Path(log_dir) / "acceptance" / "evidence"
    refs: list[EvidenceRef] = []
    timestamp = recorded_at or utc_now_rfc3339()
    for change in changes:
        role: Literal["baseline", "candidate"] = (
            "baseline"
            if change.params.get("from_state") == "fail" and not change.met
            else "candidate"
        )
        sequence, directory = _reserve_sequence(evidence_root, transaction_id)
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
            "ticket_identity": dict(ticket_identity or {}),
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
    ticket_identity: Mapping[str, Any] | None,
    participant_heads: Mapping[str, str],
    accepted_at: str | None = None,
) -> AcceptanceSnapshot:
    """Freeze and select one Criteria Satisfaction Record for the current Ticket epoch."""
    identity = dict(ticket_identity or {})
    _validate_state_projection(log_dir, state, identity)
    payload = {
        "schema": SCHEMA_VERSION,
        "slug": state.slug,
        "ticket_type": state.ticket_type,
        "execution_id": execution_id,
        "accepted_at": accepted_at or utc_now_rfc3339(),
        "ticket_identity": identity,
        "participant_heads": _participant_heads(participant_heads),
        "criteria": {key: entry.to_dict() for key, entry in state.criteria.items()},
        "evidence": _read_evidence_refs(log_dir, state, identity),
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


def bind_review_package(
    log_dir: Path, snapshot: AcceptanceSnapshot, *, replace_existing: bool = False
) -> bool:
    """Bind an already verified review package to its Criteria Satisfaction Record."""
    root = Path(log_dir)
    manifest_path = root / ".runtime" / "triage-prep" / "manifest.json"
    if not manifest_path.exists():
        return False
    try:
        manifest_bytes = manifest_path.read_bytes()
        manifest = json.loads(manifest_bytes)
        if manifest.get("status") != "ready":
            raise ValueError("review package manifest is not ready")
        _validate_manifest_identity(manifest, snapshot)
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
    path = root / "acceptance" / "review-package.json"
    if replace_existing:
        # The caller holds the Ticket publication lock; acceptance stays write-once.
        accepted = read_acceptance(root)
        if accepted.snapshot != snapshot:
            raise AcceptanceLedgerError("cannot rebind a different Criteria Satisfaction Record")
        atomic_replace_bytes(path, binding + b"\n")
    else:
        _write_once(path, binding + b"\n")
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
        _validate_manifest_identity(manifest, snapshot)
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


def _validate_manifest_identity(manifest: Mapping[str, Any], snapshot: AcceptanceSnapshot) -> None:
    manifest_generation = manifest.get("ticket_generation")
    snapshot_generation = snapshot.ticket_identity.get("generation")
    if not isinstance(manifest_generation, str) or manifest_generation != snapshot_generation:
        raise ValueError("review package names a different Ticket generation")
    heads = {"outer": manifest.get("head_sha")}
    if "project_head_sha" in manifest:
        heads["project"] = manifest.get("project_head_sha")
    if _participant_heads(heads) != snapshot.participant_heads:
        raise ValueError("review package heads disagree with the Criteria Satisfaction Record")


def read_acceptance(log_dir: Path) -> AcceptanceReadResult:
    """Read and integrity-check the Criteria Satisfaction Record for one Ticket."""
    root = Path(log_dir) / "acceptance"
    reference_path = root / "accepted.json"
    if not reference_path.exists():
        return AcceptanceReadResult(
            "unavailable", reason="Criteria Satisfaction Record is unavailable"
        )
    try:
        reference = json.loads(reference_path.read_text(encoding="utf-8"))
        digest = reference["snapshot_digest"]
        if not isinstance(digest, str) or len(digest) != 64:
            raise ValueError("accepted reference has an invalid digest")
        payload = json.loads((root / "snapshots" / f"{digest}.json").read_text(encoding="utf-8"))
        actual = hashlib.sha256(_canonical(payload)).hexdigest()
        if actual != digest:
            raise ValueError("Criteria Satisfaction Record digest mismatch")
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

"""Durable, content-addressed Criterion evidence beneath one log directory.

The ledger owns two write paths and the readers both rely on:

* V1 :func:`record_changes` appends one immutable ``record.json`` per
  effective Criterion change, in completion order, under a sequence lock.
* V2 :func:`record_or_verify_transaction` freezes a Simulation Campaign's
  changes as an intent, publishes each change as a sequenced record,
  commits a manifest, and selects the transaction into the live state. Every
  durable step is a named publication checkpoint, and a retry after any of
  them recovers (staged temporaries, a committed prefix, an unselected
  commit) instead of duplicating evidence.

What the evidence is *about* is not the ledger's business. A caller passes an
:class:`EvidenceScope`: the ``purpose`` stamped into every record and lookup
key, and an :class:`EvidenceIdentityCodec` that validates and canonicalises
the identity, names it inside lookup keys, envelopes, and records, and
judges stored observations. V2 additionally takes an
:class:`EvidenceProjection` that applies the caller's completion fencing to
replayed state and to the projection check. Ticket Mode composes these in
``booley.ticket_board.acceptance_ledger``, Goal Mode in
``booley.goals.recorder``.

A V2 intent is read back only when its envelope names the very identity its
lookup key does, so a transaction frozen under one identity is never
selected under another.

On-disk layout beneath ``<log_dir>/acceptance/``: ``evidence/`` (sequenced
records plus ``.sequence.lock``), ``intents/`` (one frozen intent per lookup
key), and ``transactions/`` (one commit manifest per transaction id).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import suppress
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Protocol, cast
from uuid import UUID

from booley.core.file_lock import release_file_lock, wait_for_file_lock
from booley.criteria.state import CriterionChange, DevelopmentState
from booley.runtime.atomic_files import WriteOnceConflictError, atomic_write_once
from booley.runtime.timefmt import utc_now_rfc3339

SCHEMA_VERSION = 1
#: Size bound for one sequenced evidence record.
MAX_RECORD_BYTES = 1 << 20


class AcceptanceLedgerError(RuntimeError):
    """Durable Criterion evidence could not be written safely."""


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


class EvidenceIdentityCodec(Protocol):
    """Per-purpose rules for the identity evidence is recorded under.

    The codec owns everything identity-shaped the ledger persists: the
    lookup-key fields, the envelope's subject object (stored under
    ``subject_field`` with exactly ``subject_fields``), the subject fields
    copied into each record (the identity itself under ``identity_field``),
    and the validation of identities read back from stored observations.
    Every method raises :class:`AcceptanceLedgerError` on invalid input.
    """

    @property
    def subject_field(self) -> str:
        """Envelope key holding the subject object."""
        ...

    @property
    def subject_fields(self) -> frozenset[str]:
        """Exact field set of the envelope's subject object."""
        ...

    @property
    def identity_field(self) -> str:
        """Record field carrying the identity an observation was made under."""
        ...

    def lookup_identity(self, identity: Mapping[str, Any]) -> dict[str, Any]:
        """Validate and canonicalise *identity*; return its lookup-key fields."""
        ...

    def envelope_subject(
        self, state: DevelopmentState, lookup_identity: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Build the envelope subject from the state and validated lookup fields."""
        ...

    def validate_envelope_subject(self, subject: Mapping[str, Any]) -> None:
        """Validate the values of a stored subject whose field set already matched."""
        ...

    def subject_lookup_identity(self, subject: Mapping[str, Any]) -> dict[str, Any]:
        """Lookup-key fields named by a validated envelope subject."""
        ...

    def record_subject(self, subject: Mapping[str, Any]) -> dict[str, Any]:
        """Fields one V2 record copies from a validated envelope subject."""
        ...

    def append_subject(
        self, state: DevelopmentState, identity: Mapping[str, Any] | None
    ) -> dict[str, Any]:
        """Fields one V1 appended record carries for *state* and *identity*."""
        ...

    def validate_observation_identity(self, value: object, current: Mapping[str, Any]) -> None:
        """Validate a stored observation's identity before it is filtered or replayed."""
        ...


class EvidenceProjection(Protocol):
    """Completion fencing applied when evidence is projected into live state."""

    def current_observations(
        self,
        log_dir: Path,
        state: DevelopmentState,
        identity: Mapping[str, Any],
        records: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """The observations to replay or validate for *identity*'s own active *records*.

        A purpose whose identity groups may overlap substitutes, per
        Criterion, a newer observation recorded under another valid group;
        a purpose whose groups never overlap returns *records* unchanged.
        """
        ...

    def project_state(
        self, state: DevelopmentState, log_dir: Path, identity: Mapping[str, Any]
    ) -> None:
        """Fence *state* after every active observation has been replayed into it."""
        ...

    def effective_met(
        self,
        log_dir: Path,
        criterion: str,
        met: object,
        detail: object,
        identity: Mapping[str, Any],
    ) -> object:
        """Effective ``met`` value compared between mutable state and evidence."""
        ...


@dataclass(frozen=True)
class EvidenceScope:
    """The purpose evidence is recorded for and the codec of its identity."""

    purpose: str
    codec: EvidenceIdentityCodec


_HEX_32 = re.compile(r"[0-9a-f]{32}")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_V2_SCHEMA = "booley.acceptance-record/v2"
_COMMIT_SCHEMA = "booley.acceptance-transaction/v1"
_ENVELOPE_SCHEMA = "booley.acceptance-transaction-envelope/v1"
_INTENT_SCHEMA = "booley.simulation-acceptance-intent/v1"
_ROLE_DERIVATION = "booley.acceptance-role/v1"
_PRODUCER = "simulation_campaign"
_MAX_CHANGES = 4_000
_MAX_DOCUMENT_BYTES = 16 << 20
_MAX_INTENT_BYTES = 32 << 20
_TEMP_PATTERN = re.compile(
    r"\.tmp\.acceptance\.tx-([0-9a-f]{64})\.ord-([0-9]{8})\."
    r"seq-([0-9]{9})\.nonce-([0-9a-f]{32})"
)
_ENVELOPE_FIELDS = frozenset(
    {
        "$schema",
        "campaign_id",
        "manifest_sha256",
        "origin",
        "producer",
        "purpose",
        "recorded_at",
        "role_derivation",
        "changes",
    }
)


def canonical_json(value: Mapping[str, Any]) -> bytes:
    """Encode *value* as the ledger's canonical JSON bytes (no trailing newline)."""
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode(
            "utf-8"
        )
    except (TypeError, ValueError) as exc:
        raise AcceptanceLedgerError(f"acceptance value is not canonical JSON: {exc}") from exc


def write_once(path: Path, content: bytes) -> None:
    """Atomically create *path*, accepting an identical existing value."""
    try:
        atomic_write_once(path, content)
    except WriteOnceConflictError as exc:
        raise AcceptanceLedgerError(f"conflicting acceptance record: {path}") from exc


def _digest(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _exact_fields(
    value: Mapping[str, Any], expected: set[str] | frozenset[str], label: str
) -> None:
    if set(value) != expected:
        raise AcceptanceLedgerError(f"{label} has unexpected fields")


def plain_json_value(value: Any) -> Any:
    """Copy immutable mapping/tuple codecs into plain canonical JSON values."""
    if isinstance(value, Mapping):
        mapping = cast("Mapping[str, Any]", value)
        return {key: plain_json_value(item) for key, item in mapping.items()}
    if isinstance(value, (list, tuple)):
        return [plain_json_value(item) for item in cast("list[Any] | tuple[Any, ...]", value)]
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


def _change_role(change: CriterionChange) -> Literal["baseline", "candidate"]:
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
        "role": _change_role(change),
    }


def _recorded_at(facts: Mapping[str, Any]) -> str:
    consumed = facts.get("consumed_results")
    if not isinstance(consumed, list) or not consumed:
        raise AcceptanceLedgerError("acceptance facts require consumed terminal results")
    results = cast("list[Any]", consumed)
    if len(results) > 1_000:
        raise AcceptanceLedgerError("acceptance facts contain too many consumed results")
    timestamps: list[str] = []
    for item in results:
        if not isinstance(item, Mapping):
            raise AcceptanceLedgerError("consumed result must be an object")
        finished_at = cast("Mapping[str, Any]", item).get("finished_at")
        timestamps.append(_canonical_timestamp(finished_at, "finished_at"))
    return max(timestamps)


def _acceptance_fact_identity(
    acceptance_facts: Mapping[str, Any],
) -> tuple[dict[str, Any], str, str, dict[str, Any], str]:
    facts: dict[str, Any] = plain_json_value(acceptance_facts)
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
    origin_value = dict(cast("Mapping[str, Any]", origin))
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


def _lookup_key(
    scope: EvidenceScope,
    campaign_id: str,
    manifest_sha256: str,
    lookup_identity: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "campaign_id": campaign_id,
        "manifest_sha256": manifest_sha256,
        **lookup_identity,
        "producer": _PRODUCER,
        "purpose": scope.purpose,
    }


def _build_transaction_documents(
    scope: EvidenceScope,
    state: DevelopmentState,
    changes: list[CriterionChange],
    acceptance_facts: Mapping[str, Any],
    identity: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], str]:
    if len(changes) > _MAX_CHANGES:
        raise AcceptanceLedgerError("acceptance transaction has too many changes")
    facts, campaign_id, manifest_sha256, origin, recorded_at = _acceptance_fact_identity(
        acceptance_facts
    )
    lookup_identity = scope.codec.lookup_identity(identity)
    envelope = {
        "$schema": _ENVELOPE_SCHEMA,
        "campaign_id": campaign_id,
        "manifest_sha256": manifest_sha256,
        "origin": dict(origin),
        "producer": _PRODUCER,
        "purpose": scope.purpose,
        scope.codec.subject_field: scope.codec.envelope_subject(state, lookup_identity),
        "recorded_at": recorded_at,
        "role_derivation": _ROLE_DERIVATION,
        "changes": [_change_payload(change) for change in changes],
    }
    envelope_bytes = canonical_json(envelope)
    if len(envelope_bytes) > _MAX_DOCUMENT_BYTES:
        raise AcceptanceLedgerError("acceptance transaction envelope is too large")
    lookup_key = _lookup_key(scope, campaign_id, manifest_sha256, lookup_identity)
    intent = {
        "$schema": _INTENT_SCHEMA,
        "lookup_key": lookup_key,
        "acceptance_facts": facts,
        "acceptance_facts_sha256": _digest(canonical_json(facts)),
        "envelope": envelope,
    }
    return lookup_key, intent, hashlib.sha256(envelope_bytes).hexdigest()


def _stable_lookup_key(
    scope: EvidenceScope, acceptance_facts: Mapping[str, Any], identity: Mapping[str, Any]
) -> dict[str, Any]:
    _facts, campaign_id, manifest_sha256, _origin, _recorded_at_value = _acceptance_fact_identity(
        acceptance_facts
    )
    return _lookup_key(scope, campaign_id, manifest_sha256, scope.codec.lookup_identity(identity))


def read_json_document(path: Path, limit: int, label: str) -> dict[str, Any]:
    """Read one bounded canonical JSON object written by this ledger."""
    try:
        content = path.read_bytes()
        if len(content) > limit or not content.endswith(b"\n"):
            raise ValueError("invalid size or missing trailing newline")
        value = json.loads(content)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise AcceptanceLedgerError(f"corrupt {label} {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise AcceptanceLedgerError(f"corrupt {label} {path}: noncanonical JSON")
    document = cast("dict[str, Any]", value)
    if content != canonical_json(document) + b"\n":
        raise AcceptanceLedgerError(f"corrupt {label} {path}: noncanonical JSON")
    return document


def _load_or_publish_intent(
    scope: EvidenceScope,
    log_dir: Path,
    lookup_key: Mapping[str, Any],
    proposed: Mapping[str, Any],
) -> dict[str, Any]:
    path = _intent_path(log_dir, lookup_key)
    encoded = canonical_json(proposed) + b"\n"
    if len(encoded) > _MAX_INTENT_BYTES:
        raise AcceptanceLedgerError("acceptance intent is too large")
    if not path.exists():
        with suppress(WriteOnceConflictError):
            atomic_write_once(path, encoded)
        # A concurrent publisher may win the stable lookup key. Its complete,
        # authenticated intent is authoritative even when mutable inputs drifted.
    return _read_intent(scope, path, lookup_key)


def _intent_path(log_dir: Path, lookup_key: Mapping[str, Any]) -> Path:
    key_digest = hashlib.sha256(canonical_json(lookup_key)).hexdigest()
    return log_dir / "acceptance" / "intents" / f"{key_digest}.json"


def _read_intent(
    scope: EvidenceScope, path: Path, lookup_key: Mapping[str, Any]
) -> dict[str, Any]:
    intent = read_json_document(path, _MAX_INTENT_BYTES, "acceptance intent")
    _exact_fields(
        intent,
        {"$schema", "lookup_key", "acceptance_facts", "acceptance_facts_sha256", "envelope"},
        "acceptance intent",
    )
    if intent.get("$schema") != _INTENT_SCHEMA or intent.get("lookup_key") != lookup_key:
        raise AcceptanceLedgerError("acceptance intent lookup key mismatch")
    facts = intent.get("acceptance_facts")
    if not isinstance(facts, Mapping) or intent.get("acceptance_facts_sha256") != _digest(
        canonical_json(cast("Mapping[str, Any]", facts))
    ):
        raise AcceptanceLedgerError("acceptance intent facts digest mismatch")
    envelope = intent.get("envelope")
    if not isinstance(envelope, dict):
        raise AcceptanceLedgerError("acceptance intent envelope must be an object")
    _validate_envelope(scope, cast("dict[str, Any]", envelope))
    # The envelope is what publication commits and selects, so it must name the
    # very identity the lookup key does: an envelope frozen under another
    # identity (say an older Goal specification revision) is never selectable.
    if _envelope_lookup_key(scope, cast("dict[str, Any]", envelope)) != lookup_key:
        raise AcceptanceLedgerError("acceptance intent envelope does not match its lookup key")
    return intent


def _validate_envelope(scope: EvidenceScope, envelope: Mapping[str, Any]) -> None:
    _exact_fields(
        envelope,
        _ENVELOPE_FIELDS | {scope.codec.subject_field},
        "acceptance transaction envelope",
    )
    if (
        envelope.get("$schema") != _ENVELOPE_SCHEMA
        or envelope.get("producer") != _PRODUCER
        or envelope.get("purpose") != scope.purpose
        or envelope.get("role_derivation") != _ROLE_DERIVATION
    ):
        raise AcceptanceLedgerError("invalid acceptance transaction envelope identity")
    _campaign_uuid(envelope.get("campaign_id"))
    _sha256_field(envelope.get("manifest_sha256"), "manifest_sha256")
    _canonical_timestamp(envelope.get("recorded_at"), "recorded_at")
    _validate_envelope_origin_and_subject(scope, envelope)
    _validate_envelope_changes(envelope.get("changes"))


def _validate_envelope_origin_and_subject(
    scope: EvidenceScope, envelope: Mapping[str, Any]
) -> None:
    codec = scope.codec
    raw_origin = envelope.get("origin")
    raw_subject = envelope.get(codec.subject_field)
    if not isinstance(raw_origin, Mapping) or not isinstance(raw_subject, Mapping):
        raise AcceptanceLedgerError("invalid acceptance transaction envelope objects")
    origin = cast("Mapping[str, Any]", raw_origin)
    subject = cast("Mapping[str, Any]", raw_subject)
    _exact_fields(origin, {"execution_id", "invocation_id"}, "transaction origin")
    _exact_fields(subject, codec.subject_fields, f"transaction {codec.subject_field}")
    execution_id, invocation_id = origin.get("execution_id"), origin.get("invocation_id")
    if not isinstance(execution_id, str) or (
        execution_id and _HEX_32.fullmatch(execution_id) is None
    ):
        raise AcceptanceLedgerError("invalid transaction execution_id")
    if isinstance(invocation_id, bool) or not isinstance(invocation_id, int) or invocation_id < 1:
        raise AcceptanceLedgerError("invalid transaction invocation_id")
    codec.validate_envelope_subject(subject)


def _validate_envelope_changes(changes: object) -> None:
    if not isinstance(changes, list) or len(cast("list[Any]", changes)) > _MAX_CHANGES:
        raise AcceptanceLedgerError("invalid transaction changes")
    fields = {"key", "met", "reason", "mandatory", "params", "detail", "role"}
    for item in cast("list[Any]", changes):
        if not isinstance(item, Mapping):
            raise AcceptanceLedgerError("transaction change must be an object")
        change = cast("Mapping[str, Any]", item)
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
    scope: EvidenceScope,
    envelope: Mapping[str, Any],
    transaction_id: str,
    ordinal: int,
    sequence: int,
) -> dict[str, Any]:
    change = envelope["changes"][ordinal]
    origin = envelope["origin"]
    return {
        "$schema": _V2_SCHEMA,
        "sequence": sequence,
        "transaction_id": transaction_id,
        "transaction_ordinal": ordinal,
        "transaction_size": len(envelope["changes"]),
        "envelope_sha256": _digest(canonical_json(envelope)),
        **scope.codec.record_subject(envelope[scope.codec.subject_field]),
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
        "recorded_at": envelope["recorded_at"],
    }


def _existing_prefix(
    scope: EvidenceScope, root: Path, envelope: Mapping[str, Any], transaction_id: str
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
        record = read_json_document(directory / "record.json", MAX_RECORD_BYTES, "V2 record")
        ordinal = record.get("transaction_ordinal")
        if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal in found:
            raise AcceptanceLedgerError("duplicate or invalid transaction ordinal")
        sequence = int(sequence_text)
        expected = (
            _v2_record(scope, envelope, transaction_id, ordinal, sequence)
            if 0 <= ordinal < len(envelope["changes"])
            else None
        )
        content = canonical_json(record) + b"\n"
        if expected is None or record != expected:
            raise AcceptanceLedgerError("transaction evidence contradicts its envelope")
        found[ordinal] = (sequence, content)
    if set(found) != set(range(len(found))):
        raise AcceptanceLedgerError("transaction evidence is not a canonical prefix")
    return [found[index] for index in range(len(found))]


def _free_sequences(root: Path) -> Iterator[int]:
    """Yield, in order, every bounded sequence no entry beneath *root* uses yet."""
    used = {path.name.partition(".tx.")[0] for path in root.iterdir()} if root.exists() else set()
    for sequence in range(1, 1_000_001):
        if f"{sequence:09d}" not in used:
            yield sequence


def _sequence_exhausted(root: Path) -> AcceptanceLedgerError:
    return AcceptanceLedgerError(f"Criterion evidence sequence exhausted beneath {root}")


def _next_sequence(root: Path) -> int:
    for sequence in _free_sequences(root):
        return sequence
    raise _sequence_exhausted(root)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _publish_v2_record(
    scope: EvidenceScope,
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
    record = _v2_record(scope, envelope, transaction_id, ordinal, sequence)
    content = canonical_json(record) + b"\n"
    if len(content) > MAX_RECORD_BYTES:
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


def _recover_current_temps(
    scope: EvidenceScope, root: Path, envelope: Mapping[str, Any], transaction_id: str
) -> None:
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
            _v2_record(scope, envelope, transaction_id, ordinal, sequence)
            if ordinal < len(envelope["changes"])
            else None
        )
        if (
            expected is None
            or not isinstance(record, dict)
            or content != canonical_json(cast("dict[str, Any]", record)) + b"\n"
            or record != expected
        ):
            raise AcceptanceLedgerError("complete temporary record contradicts its envelope")
        _promote_temp(root, path, sequence, transaction_id, content)


def _promote_temp(
    root: Path, path: Path, sequence: int, transaction_id: str, content: bytes
) -> None:
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
    entries: list[dict[str, Any]] = []
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
        "envelope_sha256": _digest(canonical_json(envelope)),
        "record_count": len(records),
        "records": entries,
    }


def _verify_commit(
    scope: EvidenceScope, evidence_root: Path, path: Path, transaction_id: str
) -> tuple[dict[str, Any], tuple[EvidenceRef, ...]]:
    commit = read_json_document(path, _MAX_DOCUMENT_BYTES, "acceptance transaction")
    _exact_fields(
        commit,
        {"$schema", "transaction_id", "envelope", "envelope_sha256", "record_count", "records"},
        "acceptance transaction",
    )
    raw_envelope = commit.get("envelope")
    raw_records = commit.get("records")
    if not isinstance(raw_envelope, dict) or not isinstance(raw_records, list):
        raise AcceptanceLedgerError("invalid acceptance transaction manifest")
    envelope = cast("dict[str, Any]", raw_envelope)
    records = cast("list[Any]", raw_records)
    if (
        commit.get("$schema") != _COMMIT_SCHEMA
        or commit.get("transaction_id") != transaction_id
        or commit.get("envelope_sha256") != _digest(canonical_json(envelope))
        or isinstance(commit.get("record_count"), bool)
        or commit.get("record_count") != len(records)
        or len(records) != len(envelope.get("changes", []))
    ):
        raise AcceptanceLedgerError("invalid acceptance transaction manifest")
    _validate_envelope(scope, envelope)
    if hashlib.sha256(canonical_json(envelope)).hexdigest() != transaction_id:
        raise AcceptanceLedgerError("acceptance transaction ID does not match its envelope")
    refs = _verify_committed_records(scope, evidence_root, envelope, transaction_id, records)
    _verify_no_extra_transaction_records(evidence_root, transaction_id, records)
    return commit, refs


def _verify_committed_records(
    scope: EvidenceScope,
    evidence_root: Path,
    envelope: Mapping[str, Any],
    transaction_id: str,
    records: list[Any],
) -> tuple[EvidenceRef, ...]:
    refs: list[EvidenceRef] = []
    for ordinal, item in enumerate(records):
        if not isinstance(item, Mapping):
            raise AcceptanceLedgerError("transaction record reference must be an object")
        entry = cast("Mapping[str, Any]", item)
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
        record = read_json_document(path, MAX_RECORD_BYTES, "V2 record")
        content = canonical_json(record) + b"\n"
        expected = _v2_record(scope, envelope, transaction_id, ordinal, sequence)
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


def _envelope_lookup_key(scope: EvidenceScope, envelope: Mapping[str, Any]) -> dict[str, Any]:
    """The lookup key a validated envelope names."""
    subject = envelope[scope.codec.subject_field]
    return {
        "campaign_id": envelope["campaign_id"],
        "manifest_sha256": envelope["manifest_sha256"],
        **scope.codec.subject_lookup_identity(subject),
        "producer": envelope["producer"],
        "purpose": envelope["purpose"],
    }


def _commit_matches_lookup(
    scope: EvidenceScope, commit: Mapping[str, Any], lookup_key: Mapping[str, Any]
) -> bool:
    return _envelope_lookup_key(scope, commit["envelope"]) == lookup_key


def _matching_commits(
    scope: EvidenceScope, log_dir: Path, evidence_root: Path, lookup_key: Mapping[str, Any]
) -> list[tuple[str, dict[str, Any], tuple[EvidenceRef, ...]]]:
    root = log_dir / "acceptance" / "transactions"
    if not root.exists():
        return []
    matching: list[tuple[str, dict[str, Any], tuple[EvidenceRef, ...]]] = []
    for path in root.iterdir():
        if not path.is_file() or re.fullmatch(r"[0-9a-f]{64}\.json", path.name) is None:
            raise AcceptanceLedgerError("malformed acceptance transaction path")
        transaction_id = path.stem
        commit, refs = _verify_commit(scope, evidence_root, path, transaction_id)
        if _commit_matches_lookup(scope, commit, lookup_key):
            matching.append((transaction_id, commit, refs))
    return matching


def legacy_transaction_records(root: Path, transaction_id: str) -> list[dict[str, Any]]:
    """Read the V1 records appended under one transaction id, in sequence order."""
    records: list[dict[str, Any]] = []
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
    return sorted(records, key=lambda record: record["sequence"])


def _read_legacy_record(directory: Path) -> dict[str, Any]:
    try:
        payload = json.loads((directory / "record.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AcceptanceLedgerError(f"corrupt Criterion evidence {directory}: {exc}") from exc
    if not isinstance(payload, dict):
        raise AcceptanceLedgerError(
            f"corrupt Criterion evidence {directory}: record is not an object"
        )
    record = cast("dict[str, Any]", payload)
    sequence_name = directory.name.partition(".tx.")[0]
    if record.get("sequence") != int(sequence_name):
        raise AcceptanceLedgerError(
            f"corrupt Criterion evidence {directory}: record sequence does not match its directory"
        )
    return record


def _plain_legacy_records(root: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
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


def _selected_records(
    scope: EvidenceScope, log_dir: Path, root: Path, state: DevelopmentState
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    commits_root = log_dir / "acceptance" / "transactions"
    for transaction_id in set(state.acceptance_transactions):
        commit_path = commits_root / f"{transaction_id}.json"
        if commit_path.exists():
            commit, _ = _verify_commit(scope, root, commit_path, transaction_id)
            for entry in commit["records"]:
                directory = root / f"{entry['sequence']:09d}.tx.{transaction_id}"
                records.append(
                    read_json_document(directory / "record.json", MAX_RECORD_BYTES, "V2 record")
                )
        else:
            records.extend(legacy_transaction_records(root, transaction_id))
    return records


def validated_evidence_records(
    scope: EvidenceScope, log_dir: Path, state: DevelopmentState, identity: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Every selected and plain observation, validated, under any identity."""
    root = log_dir / "acceptance" / "evidence"
    if not root.exists():
        if state.acceptance_transactions:
            raise AcceptanceLedgerError("selected acceptance transaction has no evidence root")
        return []
    records = _selected_records(scope, log_dir, root, state)
    records.extend(_plain_legacy_records(root))
    records.sort(key=lambda item: item["sequence"])
    seen: set[int] = set()
    for payload in records:
        sequence = payload.get("sequence")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence in seen:
            raise AcceptanceLedgerError("active Criterion evidence repeats a sequence")
        seen.add(sequence)
        scope.codec.validate_observation_identity(
            payload.get(scope.codec.identity_field), identity
        )
        _validate_observation(payload)
    return records


def _active_evidence_records(
    scope: EvidenceScope, log_dir: Path, state: DevelopmentState, identity: Mapping[str, Any]
) -> list[dict[str, Any]]:
    return [
        payload
        for payload in validated_evidence_records(scope, log_dir, state, identity)
        if payload[scope.codec.identity_field] == identity
    ]


def _validate_observation(payload: Mapping[str, Any]) -> None:
    if (
        "acceptance_basis" in payload
        or not isinstance(payload.get("criterion"), str)
        or payload.get("role") not in {"baseline", "candidate"}
        or not isinstance(payload.get("met"), bool)
        or not isinstance(payload.get("mandatory"), bool)
        or not isinstance(payload.get("params"), dict)
        or not isinstance(payload.get("detail"), dict)
    ):
        raise AcceptanceLedgerError("corrupt Criterion evidence: invalid observation")


def read_evidence_records(
    scope: EvidenceScope, log_dir: Path, state: DevelopmentState, identity: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Read every validated immutable observation recorded under *identity*.

    ``validated_evidence_records`` already rejects each observation with a
    malformed shape or identity, and ``_active_evidence_records`` keeps only
    those whose identity equals *identity*.
    """
    return _active_evidence_records(scope, Path(log_dir), state, identity)


def replay_projection(
    scope: EvidenceScope,
    projection: EvidenceProjection,
    log_dir: Path,
    state: DevelopmentState,
    identity: Mapping[str, Any],
) -> None:
    """Rebuild *state*'s Criteria from active evidence, then apply completion fencing."""
    active = _active_evidence_records(scope, log_dir, state, identity)
    for payload in projection.current_observations(log_dir, state, identity, active):
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
        if entry.params.get("from_state") == "fail":
            transition = {
                "met": met,
                "recorded_at": payload.get("recorded_at"),
                "detail": dict(entry.detail),
            }
            if transition not in entry.transition_evidence:
                entry.transition_evidence.append(transition)
    projection.project_state(state, log_dir, identity)


#: Every field a selected transaction's saved state hands back to the live state.
_LIVE_STATE_FIELDS = (
    "slug",
    "ticket_type",
    "strict_criteria",
    "criteria",
    "category_map",
    "flow_key_aliases",
    "timeline",
    "work_dir",
    "last_updated",
    "acceptance_transactions",
    "authorized_zero_mandatory_basis_id",
)


def _select_transaction(
    scope: EvidenceScope,
    projection: EvidenceProjection,
    log_dir: Path,
    state: DevelopmentState,
    transaction_id: str,
    identity: Mapping[str, Any],
) -> None:
    shadow = deepcopy(state)
    if transaction_id not in shadow.acceptance_transactions:
        shadow.acceptance_transactions.append(transaction_id)
    replay_projection(scope, projection, log_dir, shadow, identity)
    shadow.save()
    # The ledger shares DevelopmentState's persistence contract: a state bound
    # to a file is re-read after save so the replay sees exactly what landed.
    file_path = shadow.file_path
    if file_path is not None:
        saved = DevelopmentState.load(file_path, shadow.persistence)
        if transaction_id not in saved.acceptance_transactions:
            raise AcceptanceLedgerError("saved state did not select acceptance transaction")
        replay_projection(scope, projection, log_dir, saved, identity)
    else:
        saved = shadow
    state.take_saved(saved, _LIVE_STATE_FIELDS)


def validate_state_projection(
    scope: EvidenceScope,
    projection: EvidenceProjection,
    log_dir: Path,
    state: DevelopmentState,
    identity: Mapping[str, Any],
) -> None:
    """Reject mutable Criterion values that conflict with ledger-observed values."""
    latest: dict[str, dict[str, Any]] = {}
    active = read_evidence_records(scope, log_dir, state, identity)
    for payload in projection.current_observations(log_dir, state, identity, active):
        latest[payload["criterion"]] = payload
    for criterion, payload in latest.items():
        entry = state.criteria.get(criterion)
        agrees = entry is not None and (
            projection.effective_met(log_dir, criterion, entry.met, entry.detail, identity)
            is projection.effective_met(
                log_dir, criterion, payload.get("met"), payload.get("detail"), identity
            )
            and entry.mandatory is payload.get("mandatory")
            and entry.params == payload.get("params")
            and entry.detail == payload.get("detail")
        )
        if not agrees:
            raise AcceptanceLedgerError(
                f"mutable Criterion {criterion!r} disagrees with its latest evidence"
            )


@dataclass(frozen=True)
class _Reconciliation:
    """Everything one locked V2 reconciliation needs besides the live state."""

    scope: EvidenceScope
    projection: EvidenceProjection
    root: Path
    evidence_root: Path
    lookup_key: Mapping[str, Any]
    envelope: Mapping[str, Any]
    transaction_id: str
    identity: Mapping[str, Any]
    checkpoint: Callable[[str], None]


def _select_existing(
    plan: _Reconciliation,
    state: DevelopmentState,
    match: tuple[str, dict[str, Any], tuple[EvidenceRef, ...]],
) -> AcceptanceTransaction:
    found_id, _commit, refs = match
    if found_id in state.acceptance_transactions:
        validate_state_projection(plan.scope, plan.projection, plan.root, state, plan.identity)
    else:
        plan.checkpoint("before:acceptance_state")
        _select_transaction(plan.scope, plan.projection, plan.root, state, found_id, plan.identity)
        plan.checkpoint("after:acceptance_state")
    return AcceptanceTransaction(found_id, refs)


def _publish_transaction(plan: _Reconciliation, state: DevelopmentState) -> AcceptanceTransaction:
    scope, evidence_root, transaction_id = plan.scope, plan.evidence_root, plan.transaction_id
    _recover_current_temps(scope, evidence_root, plan.envelope, transaction_id)
    prefix = _existing_prefix(scope, evidence_root, plan.envelope, transaction_id)
    for ordinal in range(len(prefix), len(plan.envelope["changes"])):
        plan.checkpoint("before:acceptance_record")
        prefix.append(
            _publish_v2_record(scope, evidence_root, plan.envelope, transaction_id, ordinal)
        )
        plan.checkpoint("after:acceptance_record")
    commit = _commit_document(plan.envelope, transaction_id, prefix)
    commit_bytes = canonical_json(commit) + b"\n"
    if len(commit_bytes) > _MAX_DOCUMENT_BYTES:
        raise AcceptanceLedgerError("acceptance transaction manifest is too large")
    commit_path = plan.root / "acceptance" / "transactions" / f"{transaction_id}.json"
    plan.checkpoint("before:acceptance_commit")
    write_once(commit_path, commit_bytes)
    plan.checkpoint("after:acceptance_commit")
    _verified, refs = _verify_commit(scope, evidence_root, commit_path, transaction_id)
    plan.checkpoint("before:acceptance_state")
    _select_transaction(scope, plan.projection, plan.root, state, transaction_id, plan.identity)
    plan.checkpoint("after:acceptance_state")
    return AcceptanceTransaction(transaction_id, refs)


def _reconcile_transaction_locked(
    plan: _Reconciliation, state: DevelopmentState
) -> AcceptanceTransaction:
    _validate_reserved_temp_names(plan.evidence_root)
    matching = _matching_commits(plan.scope, plan.root, plan.evidence_root, plan.lookup_key)
    if len(matching) > 1:
        raise AcceptanceLedgerError("multiple transactions match one acceptance intent")
    if matching:
        return _select_existing(plan, state, matching[0])
    return _publish_transaction(plan, state)


def _frozen_intent(
    scope: EvidenceScope,
    root: Path,
    state: DevelopmentState,
    changes: list[CriterionChange],
    acceptance_facts: Mapping[str, Any],
    identity: Mapping[str, Any],
    checkpoint: Callable[[str], None],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return the stable lookup key and its intent, publishing the intent once."""
    lookup_key = _stable_lookup_key(scope, acceptance_facts, identity)
    intent_path = _intent_path(root, lookup_key)
    if intent_path.exists():
        return lookup_key, _read_intent(scope, intent_path, lookup_key)
    proposed_lookup, proposed_intent, _proposed_id = _build_transaction_documents(
        scope, state, changes, acceptance_facts, identity
    )
    if proposed_lookup != lookup_key:
        raise AssertionError("acceptance lookup derivation disagrees")
    checkpoint("before:acceptance_intent")
    intent = _load_or_publish_intent(scope, root, lookup_key, proposed_intent)
    checkpoint("after:acceptance_intent")
    return lookup_key, intent


def _no_checkpoint(_boundary: str) -> None:
    """Publication checkpoint used when the caller observes none."""


def record_or_verify_transaction(
    log_dir: Path,
    state: DevelopmentState,
    changes: list[CriterionChange],
    *,
    scope: EvidenceScope,
    projection: EvidenceProjection,
    acceptance_facts: Mapping[str, Any],
    identity: Mapping[str, Any],
    publication_checkpoint: Callable[[str], None] | None = None,
) -> AcceptanceTransaction:
    """Record or verify one intent-frozen Simulation Campaign transaction."""
    root = Path(log_dir)
    checkpoint = publication_checkpoint or _no_checkpoint
    lookup_key, intent = _frozen_intent(
        scope, root, state, changes, acceptance_facts, identity, checkpoint
    )
    envelope = intent["envelope"]
    evidence_root = root / "acceptance" / "evidence"
    evidence_root.mkdir(parents=True, exist_ok=True)
    plan = _Reconciliation(
        scope,
        projection,
        root,
        evidence_root,
        lookup_key,
        envelope,
        hashlib.sha256(canonical_json(envelope)).hexdigest(),
        identity,
        checkpoint,
    )
    lock_path = evidence_root / ".sequence.lock"
    with lock_path.open("a+", encoding="utf-8") as handle:
        wait_for_file_lock(handle, timeout_s=5)
        try:
            return _reconcile_transaction_locked(plan, state)
        finally:
            release_file_lock(handle)


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
    for sequence in _free_sequences(root):
        name = f"{sequence:09d}"
        directory = root / (f"{name}.tx.{transaction_id}" if transaction_id else name)
        try:
            directory.mkdir()
        except FileExistsError:
            continue
        return sequence, directory
    raise _sequence_exhausted(root)


def record_changes(
    log_dir: Path,
    state: DevelopmentState,
    changes: list[CriterionChange],
    *,
    scope: EvidenceScope,
    invocation_id: str,
    producer: str,
    execution_id: str,
    identity: Mapping[str, Any] | None = None,
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
        role = _change_role(change)
        sequence, directory = _reserve_sequence(evidence_root, transaction_id)
        payload = {
            "schema": SCHEMA_VERSION,
            "sequence": sequence,
            **scope.codec.append_subject(state, identity),
            "execution_id": execution_id,
            "purpose": scope.purpose,
            "producer": producer,
            "invocation_id": invocation_id,
            "role": role,
            "criterion": change.key,
            "met": change.met,
            "reason": change.reason,
            "mandatory": change.mandatory,
            "params": change.params,
            "detail": change.detail,
            "recorded_at": timestamp,
        }
        encoded = canonical_json(payload)
        digest = hashlib.sha256(encoded).hexdigest()
        write_once(directory / "record.json", encoded + b"\n")
        refs.append(EvidenceRef(sequence, digest, change.key, role))
    return tuple(refs)


@dataclass(frozen=True)
class PreparedObservation:
    """Neutral immutable publication bytes for an owning lifecycle transaction.

    The owner must serialize preparation and publication with all writers of
    this ledger. No sequence is reserved and no observation is published here.
    A record-wide apply barrier keeps the captured destinations exclusive from
    the owner's intent until recovery finishes.
    """

    relative_path: str
    content: bytes


def prepare_observations(
    log_dir: Path,
    state: DevelopmentState,
    *,
    scope: EvidenceScope,
    observations: Sequence[tuple[CriterionChange, str, Mapping[str, Any]]],
    transaction_id: str,
    invocation_id: str,
    producer: str,
) -> tuple[PreparedObservation, ...]:
    """Build selected V1 observation bytes without Simulation-specific V2 facts."""
    if re.fullmatch(r"[0-9a-f]{64}", transaction_id) is None:
        raise AcceptanceLedgerError("invalid acceptance transaction identity")
    root = log_dir / "acceptance" / "evidence"
    sequences = _free_sequences(root) if root.exists() else iter(range(1, 1_000_001))
    prepared: list[PreparedObservation] = []
    for change, timestamp, identity in observations:
        sequence = next(sequences, None)
        if sequence is None:
            raise _sequence_exhausted(root)
        payload = {
            "schema": SCHEMA_VERSION,
            "sequence": sequence,
            **scope.codec.append_subject(state, identity),
            "execution_id": "",
            "purpose": scope.purpose,
            "producer": producer,
            "invocation_id": invocation_id,
            "role": _change_role(change),
            "criterion": change.key,
            "met": change.met,
            "reason": change.reason,
            "mandatory": change.mandatory,
            "params": change.params,
            "detail": change.detail,
            "recorded_at": timestamp,
        }
        relative = f"acceptance/evidence/{sequence:09d}.tx.{transaction_id}/record.json"
        prepared.append(PreparedObservation(relative, canonical_json(payload) + b"\n"))
    return tuple(prepared)

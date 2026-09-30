"""Per-Ticket Waiver Candidate store (ADR 0066).

The Coverage Analyst's non-model code records the Waiver Candidates it screened
for a live Ticket in the ignored record ``<tickets>/waiver-candidates/<slug>.json``
(see :mod:`booley.ticket_board.board_layout` for the paths). The human accepts
or rejects each candidate at review; rejections are remembered so later
proposals for the same Target, Coverage Point, and source SHA-256 are dropped.

Accumulation: candidates are keyed by ``(target_identity, point_id)``. A new
proposal with the same Campaign binding and source SHA-256 bumps the entry's
``proposal_count`` and replaces its reason, justification, evidence, and proof
reference. A proposal from any other binding replaces the entry and restarts
the count at 1; the old one could no longer be promoted anyway.

The record sits in the Sandbox, so it is checked rather than trusted (ADR 0066
"Trust boundary"): this module only guarantees a well-formed record, and
approval re-derives every candidate from the Campaign evidence.

Failure semantics follow ADR 0065 state records:

- an absent file is an empty record; a file that is unreadable, not JSON, of
  an unknown schema, for another slug, or with any invalid field raises
  :class:`WaiverCandidateRecordError` and nothing is changed;
- every mutation is a read-modify-write under a cross-process lock at
  ``<tickets>/locks/waiver-candidates-<slug>.lock`` with a bounded wait, and
  publishes the whole record with one atomic replace (or one durable unlink
  once it is empty), so a crash leaves the old or the new record, never a mix.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, TypeVar

from booley.core.boundary import (
    BoundaryError,
    is_str_list,
    require_dict,
    require_finite_number_value,
    require_int,
    require_list,
    require_sha256_digest,
    require_str_value,
)
from booley.core.file_lock import LockTimeoutError, release_file_lock, wait_for_file_lock
from booley.runtime.timefmt import MACHINE_TIMESTAMP_FORMAT, rfc3339_from_datetime

from .board_layout import waiver_candidates_lock_path, waiver_candidates_path
from .persistence import atomic_replace_bytes, durable_unlink

WAIVER_CANDIDATE_SCHEMA = 1
REASONS = frozenset({"excluded", "unreachable"})
LOCK_TIMEOUT_SECONDS = 30.0

_POINT_ID = re.compile(r"cp1:[A-Za-z0-9_-]+")
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
_RECORD_FIELDS = frozenset({"schema", "slug", "candidates", "rejections"})
_BINDING_FIELDS = (
    "campaign_id",
    "manifest_sha256",
    "point_store_sha256",
    "campaign_path",
    "target_identity",
    "target_selector",
)
_CANDIDATE_FIELDS = frozenset(
    {
        *_BINDING_FIELDS,
        "candidate_id",
        "point_id",
        "source",
        "source_sha256",
        "reason",
        "justification",
        "evidence_refs",
        "proof_reference",
        "proposal_count",
        "first_recorded_at",
        "last_recorded_at",
        "last_invocation_id",
    }
)
_REJECTION_FIELDS = frozenset(
    {"target_identity", "point_id", "source_sha256", "rejected_at", "rejected_by"}
)

_Result = TypeVar("_Result")

CandidateKey = tuple[str, str]
"""``(target_identity, point_id)``: at most one candidate per key."""


class WaiverCandidateRecordError(RuntimeError):
    """A Waiver Candidate record exists but cannot be trusted; nothing was changed."""


class WaiverCandidateLockError(TimeoutError):
    """Another writer held the Waiver Candidate record lock past the bounded wait."""


# Field validation -------------------------------------------------------------


def candidate_id(target_identity: str, point_id: str, source_sha256: str) -> str:
    """Return the stable candidate id: ``wc-`` + 12 hex of the binding's SHA-256."""
    digest = hashlib.sha256(f"{target_identity}|{point_id}|{source_sha256}".encode())
    return f"wc-{digest.hexdigest()[:12]}"


def _text(value: Any, name: str) -> str:
    return require_str_value(value, field=repr(name))


def _relative_path(value: Any, name: str) -> str:
    """Require a normalized relative POSIX path that cannot escape its root."""
    text = _text(value, name)
    parts = text.split("/")
    if "\\" in text or PurePosixPath(text).is_absolute() or {"", ".", ".."} & set(parts):
        raise BoundaryError(f"{name!r} must be a normalized relative POSIX path, got {text!r}")
    return text


def _point_id(value: Any) -> str:
    """Require a ``cp1:`` Coverage Point id; approval re-derives the point itself."""
    text = _text(value, "point_id")
    if _POINT_ID.fullmatch(text) is None:
        raise BoundaryError(f"'point_id' must be a cp1 Coverage Point id, got {text!r}")
    return text


def _timestamp(value: Any, name: str) -> str:
    """Require a canonical second-resolution UTC ``...Z`` timestamp."""
    text = _text(value, name)
    problem = f"{name!r} must be a canonical UTC timestamp, got {text!r}"
    if _TIMESTAMP.fullmatch(text) is None:
        raise BoundaryError(problem)
    try:
        datetime.strptime(text, MACHINE_TIMESTAMP_FORMAT)
    except ValueError as exc:
        raise BoundaryError(problem) from exc
    return text


def _reason(value: Any) -> str:
    text = _text(value, "reason")
    if text not in REASONS:
        raise BoundaryError(f"'reason' must be one of {sorted(REASONS)}, got {text!r}")
    return text


def _evidence_refs(value: Any) -> tuple[str, ...]:
    refs = list(value) if isinstance(value, tuple) else value
    if not is_str_list(refs) or not all(refs):
        raise BoundaryError("'evidence_refs' must be a list of non-empty strings")
    return tuple(refs)


def _canonical_now(now: datetime) -> str:
    if now.tzinfo is None:
        raise ValueError("'now' must be timezone-aware")
    return rfc3339_from_datetime(now)


# Records ------------------------------------------------------------------------


@dataclass(frozen=True)
class CampaignBinding:
    """The persisted Campaign one batch of proposals was screened against (R4).

    ``campaign_path`` is relative to the runtime ``flow-reports`` root.
    """

    campaign_id: str
    manifest_sha256: str
    point_store_sha256: str
    campaign_path: str
    target_identity: str
    target_selector: str

    def __post_init__(self) -> None:
        _text(self.campaign_id, "campaign_id")
        require_sha256_digest(self.manifest_sha256, field="'manifest_sha256'")
        require_sha256_digest(self.point_store_sha256, field="'point_store_sha256'")
        _relative_path(self.campaign_path, "campaign_path")
        _text(self.target_identity, "target_identity")
        _text(self.target_selector, "target_selector")

    def to_json(self) -> dict[str, str]:
        return {name: getattr(self, name) for name in _BINDING_FIELDS}


@dataclass(frozen=True)
class WaiverProposal:
    """One screened Analyst candidate offered for recording."""

    point_id: str
    source: str
    source_sha256: str
    reason: str
    justification: str
    evidence_refs: tuple[str, ...] = ()
    proof_reference: str = ""

    def __post_init__(self) -> None:
        _point_id(self.point_id)
        _relative_path(self.source, "source")
        require_sha256_digest(self.source_sha256, field="'source_sha256'")
        _reason(self.reason)
        _text(self.justification, "justification")
        object.__setattr__(self, "evidence_refs", _evidence_refs(self.evidence_refs))
        require_str_value(self.proof_reference, field="'proof_reference'", allow_empty=True)


@dataclass(frozen=True)
class WaiverCandidate:
    """One recorded Waiver Candidate: a proposal bound to its Campaign."""

    binding: CampaignBinding
    proposal: WaiverProposal
    proposal_count: int
    first_recorded_at: str
    last_recorded_at: str
    last_invocation_id: str

    def __post_init__(self) -> None:
        if require_int(self.proposal_count, field="'proposal_count'") < 1:
            raise BoundaryError("'proposal_count' must be at least 1")
        _timestamp(self.first_recorded_at, "first_recorded_at")
        _timestamp(self.last_recorded_at, "last_recorded_at")
        if self.last_recorded_at < self.first_recorded_at:
            raise BoundaryError("'last_recorded_at' precedes 'first_recorded_at'")
        _text(self.last_invocation_id, "last_invocation_id")

    @property
    def candidate_id(self) -> str:
        return candidate_id(
            self.binding.target_identity, self.proposal.point_id, self.proposal.source_sha256
        )

    @property
    def key(self) -> CandidateKey:
        return (self.binding.target_identity, self.proposal.point_id)

    def to_json(self) -> dict[str, Any]:
        proposal = self.proposal
        return {
            **self.binding.to_json(),
            "candidate_id": self.candidate_id,
            "point_id": proposal.point_id,
            "source": proposal.source,
            "source_sha256": proposal.source_sha256,
            "reason": proposal.reason,
            "justification": proposal.justification,
            "evidence_refs": list(proposal.evidence_refs),
            "proof_reference": proposal.proof_reference,
            "proposal_count": self.proposal_count,
            "first_recorded_at": self.first_recorded_at,
            "last_recorded_at": self.last_recorded_at,
            "last_invocation_id": self.last_invocation_id,
        }


@dataclass(frozen=True)
class Rejection:
    """A human rejection of one Coverage Point at one source SHA-256."""

    target_identity: str
    point_id: str
    source_sha256: str
    rejected_at: str
    rejected_by: str

    def __post_init__(self) -> None:
        _text(self.target_identity, "target_identity")
        _point_id(self.point_id)
        require_sha256_digest(self.source_sha256, field="'source_sha256'")
        _timestamp(self.rejected_at, "rejected_at")
        _text(self.rejected_by, "rejected_by")

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.target_identity, self.point_id, self.source_sha256)

    def to_json(self) -> dict[str, str]:
        return {name: getattr(self, name) for name in sorted(_REJECTION_FIELDS)}


def _rejects(rejections: Iterable[Rejection], candidate: WaiverCandidate) -> bool:
    key = (*candidate.key, candidate.proposal.source_sha256)
    return any(rejection.key == key for rejection in rejections)


@dataclass(frozen=True)
class CandidateRecord:
    """One Ticket's Waiver Candidates and rejections; empty when never recorded."""

    slug: str
    candidates: Mapping[CandidateKey, WaiverCandidate] = field(default_factory=dict)
    rejections: tuple[Rejection, ...] = ()

    def __post_init__(self) -> None:
        for key, candidate in self.candidates.items():
            if key != candidate.key:
                raise BoundaryError(f"candidate {candidate.candidate_id} is filed under {key}")
        keys = [rejection.key for rejection in self.rejections]
        if len(set(keys)) != len(keys):
            raise BoundaryError("rejections contain duplicates")
        ids = [candidate.candidate_id for candidate in self.candidates.values()]
        if len(set(ids)) != len(ids):
            raise BoundaryError("candidate ids collide")
        ordered = MappingProxyType(dict(sorted(self.candidates.items())))
        object.__setattr__(self, "candidates", ordered)
        object.__setattr__(self, "rejections", tuple(sorted(self.rejections, key=_key_of)))

    @property
    def is_empty(self) -> bool:
        return not self.candidates and not self.rejections

    def by_id(self, wanted_id: str) -> WaiverCandidate | None:
        """Return the candidate whose id is *wanted_id*, if recorded."""
        return next(
            (item for item in self.candidates.values() if item.candidate_id == wanted_id), None
        )

    def is_rejected(self, target_identity: str, point_id: str, source_sha256: str) -> bool:
        return any(
            rejection.key == (target_identity, point_id, source_sha256)
            for rejection in self.rejections
        )

    def to_bytes(self) -> bytes:
        """Return the canonical on-disk encoding (sorted keys, trailing newline)."""
        document = {
            "schema": WAIVER_CANDIDATE_SCHEMA,
            "slug": self.slug,
            "candidates": {item.candidate_id: item.to_json() for item in self.candidates.values()},
            "rejections": [rejection.to_json() for rejection in self.rejections],
        }
        return (json.dumps(document, indent=2, sort_keys=True) + "\n").encode()


def _key_of(rejection: Rejection) -> tuple[str, str, str]:
    return rejection.key


@dataclass(frozen=True)
class RecordOutcome:
    """What :func:`record_proposals` did with one batch."""

    recorded: int
    filtered_by_rejection: int
    candidate_ids: tuple[str, ...] = ()


# Parsing ------------------------------------------------------------------------


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    document = dict(pairs)
    if len(document) != len(pairs):
        raise BoundaryError("duplicate JSON object key")
    return document


def _exact_fields(document: dict[str, Any], expected: frozenset[str], name: str) -> None:
    if set(document) != expected:
        different = ", ".join(sorted(set(document) ^ expected))
        raise BoundaryError(f"{name} has unexpected or missing fields: {different}")


def _parse_candidate(stored_id: str, value: Any) -> WaiverCandidate:
    document = require_dict(value, field=f"candidate {stored_id!r}")
    _exact_fields(document, _CANDIDATE_FIELDS, f"candidate {stored_id!r}")
    candidate = WaiverCandidate(
        binding=CampaignBinding(**{name: document[name] for name in _BINDING_FIELDS}),
        proposal=WaiverProposal(
            point_id=document["point_id"],
            source=document["source"],
            source_sha256=document["source_sha256"],
            reason=document["reason"],
            justification=document["justification"],
            evidence_refs=document["evidence_refs"],
            proof_reference=document["proof_reference"],
        ),
        proposal_count=document["proposal_count"],
        first_recorded_at=document["first_recorded_at"],
        last_recorded_at=document["last_recorded_at"],
        last_invocation_id=document["last_invocation_id"],
    )
    if stored_id != candidate.candidate_id or document["candidate_id"] != stored_id:
        raise BoundaryError(f"candidate {stored_id!r} does not match its binding")
    return candidate


def _parse_rejection(value: Any) -> Rejection:
    document = require_dict(value, field="rejection")
    _exact_fields(document, _REJECTION_FIELDS, "rejection")
    return Rejection(**document)


def _parse_record(value: Any, slug: str) -> CandidateRecord:
    document = require_dict(value, field="record")
    _exact_fields(document, _RECORD_FIELDS, "record")
    schema = document["schema"]
    try:
        supported = require_finite_number_value(schema, field="schema") == WAIVER_CANDIDATE_SCHEMA
    except BoundaryError:
        supported = False
    if not supported:
        raise BoundaryError(f"schema {schema!r} is unsupported")
    if document["slug"] != slug:
        raise BoundaryError(f"record belongs to {document['slug']!r}")
    stored = require_dict(document["candidates"], field="'candidates'")
    candidates = [_parse_candidate(key, item) for key, item in stored.items()]
    by_key = {candidate.key: candidate for candidate in candidates}
    if len(by_key) != len(candidates):
        raise BoundaryError("two candidates share one Target and Coverage Point")
    rejections = tuple(
        _parse_rejection(item) for item in require_list(document["rejections"], field="rejections")
    )
    return CandidateRecord(slug, by_key, rejections)


def load(tickets_dir: Path, slug: str) -> CandidateRecord:
    """Return the Waiver Candidate record for *slug*; an absent file is empty.

    Raises :class:`WaiverCandidateRecordError` for a record that exists but is
    unreadable, not JSON, of an unknown schema, for another slug, or invalid.
    """
    path = waiver_candidates_path(tickets_dir, slug)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return CandidateRecord(slug)
    except OSError as exc:
        raise WaiverCandidateRecordError(
            f"Waiver Candidate record for {slug!r} is unreadable: {path}: {exc}"
        ) from exc
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError, BoundaryError) as exc:
        raise WaiverCandidateRecordError(
            f"Waiver Candidate record for {slug!r} is not valid JSON: {path}: {exc}"
        ) from exc
    try:
        return _parse_record(value, slug)
    except (BoundaryError, TypeError) as exc:
        raise WaiverCandidateRecordError(
            f"Waiver Candidate record for {slug!r} is invalid: {path}: {exc}"
        ) from exc


# Mutations ----------------------------------------------------------------------


@contextmanager
def _record_lock(tickets_dir: Path, slug: str) -> Iterator[None]:
    """Hold the cross-process lock of *slug*'s record, waiting a bounded time."""
    path = waiver_candidates_lock_path(tickets_dir, slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        try:
            wait_for_file_lock(handle, timeout_s=LOCK_TIMEOUT_SECONDS)
        except LockTimeoutError as exc:
            raise WaiverCandidateLockError(
                f"Waiver Candidate record for {slug!r} stayed locked for "
                f"{LOCK_TIMEOUT_SECONDS:g}s by another writer: {path}"
            ) from exc
        try:
            yield
        finally:
            release_file_lock(handle)


def _publish(tickets_dir: Path, record: CandidateRecord) -> None:
    """Atomically replace the record, or durably remove it once it is empty."""
    path = waiver_candidates_path(tickets_dir, record.slug)
    if record.is_empty:
        durable_unlink(path)
        return
    atomic_replace_bytes(path, record.to_bytes(), mode=0o644)


def _mutate(
    tickets_dir: Path,
    slug: str,
    change: Callable[[CandidateRecord], tuple[CandidateRecord, _Result]],
) -> _Result:
    """Read, change, and publish *slug*'s record under its lock; return *change*'s result.

    Publishes only when the record changed, so no-op mutations leave the file alone.
    """
    with _record_lock(tickets_dir, slug):
        current = load(tickets_dir, slug)
        updated, result = change(current)
        if updated != current:
            _publish(tickets_dir, updated)
        return result


def _accumulate(
    existing: WaiverCandidate | None,
    fresh: WaiverCandidate,
) -> WaiverCandidate:
    """Merge *fresh* into *existing* per the ADR 0066 accumulation rule."""
    if (
        existing is None
        or existing.binding != fresh.binding
        or existing.proposal.source_sha256 != fresh.proposal.source_sha256
    ):
        return fresh
    return replace(
        fresh,
        proposal_count=existing.proposal_count + 1,
        first_recorded_at=existing.first_recorded_at,
    )


def record_proposals(
    tickets_dir: Path,
    slug: str,
    binding: CampaignBinding,
    proposals: Iterable[WaiverProposal],
    *,
    invocation_id: str,
    now: datetime,
) -> RecordOutcome:
    """Record one Analyst run's screened proposals for *slug* against *binding*.

    A proposal matching a rejection (Target, point, source SHA-256) is dropped
    and counted in ``filtered_by_rejection``. Raises :class:`ValueError` when
    the batch names one Coverage Point twice.
    """
    batch = tuple(proposals)
    if len({proposal.point_id for proposal in batch}) != len(batch):
        raise ValueError("a proposal batch must name each Coverage Point once")
    _text(invocation_id, "invocation_id")
    stamp = _canonical_now(now)

    def change(record: CandidateRecord) -> tuple[CandidateRecord, RecordOutcome]:
        candidates = dict(record.candidates)
        recorded: list[str] = []
        for proposal in batch:
            fresh = WaiverCandidate(binding, proposal, 1, stamp, stamp, invocation_id)
            if _rejects(record.rejections, fresh):
                continue
            candidates[fresh.key] = _accumulate(candidates.get(fresh.key), fresh)
            recorded.append(fresh.candidate_id)
        outcome = RecordOutcome(len(recorded), len(batch) - len(recorded), tuple(recorded))
        return replace(record, candidates=candidates), outcome

    return _mutate(tickets_dir, slug, change)


def record_rejections(tickets_dir: Path, slug: str, rejections: Iterable[Rejection]) -> int:
    """Remember *rejections* for *slug* and drop the candidates they match.

    Idempotent: a rejection already recorded for the same Target, point, and
    source SHA-256 keeps its original stamps. Returns how many were new.
    """
    wanted = tuple(rejections)

    def change(record: CandidateRecord) -> tuple[CandidateRecord, int]:
        known = {rejection.key: rejection for rejection in record.rejections}
        added = 0
        for rejection in wanted:
            if rejection.key not in known:
                known[rejection.key] = rejection
                added += 1
        kept = {
            key: candidate
            for key, candidate in record.candidates.items()
            if not _rejects(known.values(), candidate)
        }
        return CandidateRecord(record.slug, kept, tuple(known.values())), added

    return _mutate(tickets_dir, slug, change)


def clear_candidates(tickets_dir: Path, slug: str) -> None:
    """Drop every candidate of *slug* but keep its rejections (return-to-draft, reset)."""

    def change(record: CandidateRecord) -> tuple[CandidateRecord, None]:
        return CandidateRecord(record.slug, {}, record.rejections), None

    _mutate(tickets_dir, slug, change)


def discard(tickets_dir: Path, slug: str) -> bool:
    """Delete *slug*'s record when the Ticket closes; return whether it existed.

    A corrupt record is deleted too: it is disposable state (ADR 0066), and
    closing must not stall on it. The lock file stays, because unlinking a lock
    others may be waiting on would let two writers hold different inodes.
    """
    with _record_lock(tickets_dir, slug):
        return durable_unlink(waiver_candidates_path(tickets_dir, slug))

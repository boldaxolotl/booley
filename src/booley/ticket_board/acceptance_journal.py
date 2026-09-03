"""Validated persistence model for recoverable Ticket acceptance."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from enum import StrEnum
from pathlib import Path
from typing import Any, TypedDict

from booley.core.boundary import BoundaryError, require_dict, require_list, require_str

_SHA_RE = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_PARTICIPANT_FIELDS = {
    "role",
    "sealed_sha",
    "ticket_ref",
    "destination_ref",
    "destination_sha",
}
_BASE_JOURNAL_FIELDS = {
    "schema",
    "transaction",
    "ticket",
    "state",
    "participants",
    "sources",
    "candidates",
    "published",
}
_CLEANUP_FIELDS = {"policy", "cleaned"}
_FINALIZATION_FIELDS = {
    "removal_targets",
    "removal_digest",
    "finalized",
}
_JOURNAL_FIELDS = (
    _BASE_JOURNAL_FIELDS
    | _CLEANUP_FIELDS
    | {
        "removal_targets",
        "removal_digest",
    }
)


class Candidate(TypedDict):
    """One validated pair of prepared and finalized acceptance identities."""

    prepared_sha: str | None
    finalized_sha: str | None
    staging_ref: str
    expected_destination_sha: str


def _removal_digest(removal_targets: tuple[str, ...]) -> str:
    payload = json.dumps(list(removal_targets), separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


class AcceptanceJournalError(ValueError):
    """An Acceptance Journal is unreadable or violates its schema."""


class JournalState(StrEnum):
    """One durable acceptance checkpoint and its progress categories."""

    INITIALIZING = "initializing"
    PREPARED = "prepared"
    PUBLISHED_PROJECT = "published-project"
    PUBLISHED_OUTER = "published-outer"
    ACCEPTED = "accepted"
    CLEANUP_PROJECT = "cleanup-project"
    CLEANUP_OUTER = "cleanup-outer"
    DONE = "done"

    @property
    def publication_pending(self) -> bool:
        return self in {
            self.INITIALIZING,
            self.PREPARED,
            self.PUBLISHED_PROJECT,
            self.PUBLISHED_OUTER,
        }

    @property
    def cleanup_pending(self) -> bool:
        return self in {
            self.PUBLISHED_OUTER,
            self.ACCEPTED,
            self.CLEANUP_PROJECT,
            self.CLEANUP_OUTER,
        }

    def expected_published(self, order: list[str]) -> list[str] | None:
        if self is self.INITIALIZING:
            return None
        if self is self.PREPARED:
            return []
        if self is self.PUBLISHED_PROJECT:
            return ["project"]
        return order

    def expected_cleaned(self, order: list[str], cleanup: bool) -> list[str] | None:
        if self is self.INITIALIZING:
            return None
        if self in {self.PREPARED, self.PUBLISHED_PROJECT, self.PUBLISHED_OUTER, self.ACCEPTED}:
            return []
        if self is self.CLEANUP_PROJECT:
            return ["project"]
        if self is self.CLEANUP_OUTER:
            return order
        return order if cleanup else []


class AcceptancePolicy(TypedDict):
    """The immutable publication and cleanup choices for one acceptance."""

    merge: bool
    cleanup: bool


class AcceptanceJournal(TypedDict):
    """Validated mutable state for one recoverable acceptance transaction."""

    schema: int
    transaction: str
    ticket: str
    state: JournalState
    policy: AcceptancePolicy
    participants: list[dict[str, str]]
    sources: dict[str, str]
    candidates: dict[str, Candidate]
    published: list[str]
    cleaned: list[str]
    removal_targets: list[str]
    removal_digest: str


def initial_journal(
    slug: str,
    participants: list[dict[str, str]],
    *,
    cleanup: bool,
    removal_targets: tuple[str, ...] = (),
) -> AcceptanceJournal:
    """Return a new journal before any repository mutation."""
    return {
        "schema": 4,
        "transaction": uuid.uuid4().hex,
        "ticket": slug,
        "state": JournalState.INITIALIZING,
        "policy": {"merge": True, "cleanup": cleanup},
        "participants": participants,
        "sources": {},
        "candidates": {},
        "published": [],
        "cleaned": [],
        "removal_targets": list(removal_targets),
        "removal_digest": _removal_digest(removal_targets),
    }


def _validated_participants(value: Any) -> list[dict[str, str]]:
    rows = require_list(value, field="acceptance journal participants")
    result: list[dict[str, str]] = []
    for index, raw in enumerate(rows):
        item = require_dict(raw, field=f"acceptance journal participants[{index}]")
        if set(item) != _PARTICIPANT_FIELDS:
            raise BoundaryError(f"acceptance journal participant {index} has invalid fields")
        strings = {key: require_str(item, key) for key in _PARTICIPANT_FIELDS}
        if strings["role"] not in {"outer", "project"}:
            raise BoundaryError(f"acceptance journal participant {index} has invalid role")
        for key in ("sealed_sha", "destination_sha"):
            if not _SHA_RE.fullmatch(strings[key]):
                raise BoundaryError(f"acceptance journal participant {index}.{key} is invalid")
        for key in ("ticket_ref", "destination_ref"):
            if not strings[key].startswith("refs/"):
                raise BoundaryError(f"acceptance journal participant {index}.{key} is invalid")
        result.append(strings)
    roles = [item["role"] for item in result]
    if roles not in (["outer"], ["outer", "project"]):
        raise BoundaryError("acceptance journal participants are out of order or duplicated")
    return result


def _validated_string_map(value: Any, field: str, roles: set[str]) -> dict[str, str]:
    mapping = require_dict(value, field=field)
    if not set(mapping) <= roles:
        raise BoundaryError(f"{field} contains an unknown participant role")
    result = {role: require_str(mapping, role) for role in mapping}
    if any(not _SHA_RE.fullmatch(item) for item in result.values()):
        raise BoundaryError(f"{field} values must be full Git commit SHAs")
    return result


def _validated_candidates(value: Any, roles: set[str], transaction: str) -> dict[str, Candidate]:
    mapping = require_dict(value, field="acceptance journal candidates")
    if not set(mapping) <= roles:
        raise BoundaryError("acceptance journal candidates contains an unknown role")
    result: dict[str, Candidate] = {}
    for role, raw in mapping.items():
        candidate = require_dict(raw, field=f"acceptance journal candidates.{role}")
        expected = {
            "prepared_sha",
            "finalized_sha",
            "staging_ref",
            "expected_destination_sha",
        }
        if set(candidate) != expected:
            raise BoundaryError(f"acceptance journal candidate {role!r} has invalid fields")
        result[role] = {
            "prepared_sha": _optional_sha(candidate, role, "prepared_sha"),
            "finalized_sha": _optional_sha(candidate, role, "finalized_sha"),
            "staging_ref": require_str(candidate, "staging_ref"),
            "expected_destination_sha": require_str(candidate, "expected_destination_sha"),
        }
        _validate_candidate(result[role], role, transaction)
    return result


def _optional_sha(candidate: dict[str, Any], role: str, key: str) -> str | None:
    if candidate.get(key) is None:
        return None
    value = require_str(candidate, key)
    if not _SHA_RE.fullmatch(value):
        raise BoundaryError(f"acceptance journal candidates.{role}.{key} is invalid")
    return value


def _validate_candidate(candidate: Candidate, role: str, transaction: str) -> None:
    if candidate["prepared_sha"] is None and candidate["finalized_sha"] is None:
        raise BoundaryError(f"acceptance journal candidate {role!r} has no recorded identity")
    expected_destination = candidate["expected_destination_sha"]
    if not _SHA_RE.fullmatch(expected_destination):
        raise BoundaryError(
            f"acceptance journal candidates.{role}.expected_destination_sha is invalid"
        )
    expected_ref = f"refs/booley/acceptance/{transaction}/{role}"
    if candidate["staging_ref"] != expected_ref:
        raise BoundaryError(
            f"acceptance journal candidates.{role}.staging_ref must be {expected_ref!r}"
        )


def _validated_policy(value: Any, *, cleanup: bool | None) -> bool:
    policy = require_dict(value, field="acceptance journal policy")
    if set(policy) != {"merge", "cleanup"}:
        raise BoundaryError("acceptance journal policy has invalid fields")
    if policy.get("merge") is not True or not isinstance(policy.get("cleanup"), bool):
        raise BoundaryError("acceptance journal policy is invalid")
    if cleanup is not None and policy["cleanup"] != cleanup:
        raise BoundaryError("acceptance journal cleanup policy changed after acceptance began")
    return policy["cleanup"]


def _validated_state(value: Any) -> JournalState:
    raw = require_str(value, "state")
    try:
        return JournalState(raw)
    except ValueError as exc:
        raise BoundaryError(f"acceptance journal state {raw!r} is invalid") from exc


def _validate_progress(
    state: JournalState,
    roles: set[str],
    cleanup: bool,
    sources: dict[str, str],
    candidates: dict[str, Candidate],
    published: list[Any],
    cleaned: list[Any],
    *,
    has_removals: bool,
) -> None:
    order = [role for role in ("project", "outer") if role in roles]
    if published != order[: len(published)]:
        raise BoundaryError("acceptance journal published roles are out of order")
    if cleaned != order[: len(cleaned)]:
        raise BoundaryError("acceptance journal cleaned roles are out of order")
    _validate_checkpoint_dependencies(
        state, roles, cleanup, sources, candidates, published, cleaned
    )
    if state.expected_published(order) not in (None, published):
        raise BoundaryError(
            f"acceptance journal state {str(state)!r} conflicts with published roles"
        )
    if state.expected_cleaned(order, cleanup) not in (None, cleaned):
        raise BoundaryError(
            f"acceptance journal state {str(state)!r} conflicts with cleaned roles"
        )
    finalized = bool(candidates) and all(
        candidate["finalized_sha"] is not None for candidate in candidates.values()
    )
    if published and not finalized:
        raise BoundaryError("acceptance journal cannot publish unfinalized candidates")
    if candidates and not has_removals and not finalized:
        raise BoundaryError("acceptance journal without removals must already be finalized")


def _validate_checkpoint_dependencies(
    state: JournalState,
    roles: set[str],
    cleanup: bool,
    sources: dict[str, str],
    candidates: dict[str, Candidate],
    published: list[Any],
    cleaned: list[Any],
) -> None:
    if candidates and not sources:
        raise BoundaryError("acceptance journal candidates require pinned sources")
    if sources and set(sources) != roles:
        raise BoundaryError("acceptance journal sources must pin every participant")
    if set(candidates) - set(sources) or set(published) - set(candidates):
        raise BoundaryError("acceptance journal checkpoints are inconsistent")
    if set(cleaned) - set(published):
        raise BoundaryError("acceptance journal cannot clean unpublished participants")
    if state is JournalState.INITIALIZING and (published or cleaned):
        raise BoundaryError("initializing acceptance journal cannot contain terminal progress")
    if state is not JournalState.INITIALIZING and set(candidates) != roles:
        raise BoundaryError(f"acceptance journal state {str(state)!r} requires every candidate")
    if cleaned and not cleanup:
        raise BoundaryError("acceptance journal cleaned roles require cleanup policy")


def validate_journal(
    value: Any,
    slug: str,
    participants: list[dict[str, str]],
    *,
    cleanup: bool | None,
    removal_targets: tuple[str, ...] | None = None,
) -> AcceptanceJournal:
    """Validate external journal data against its immutable identity."""
    journal = require_dict(value, field="acceptance journal")
    if require_str(journal, "ticket") != slug:
        raise BoundaryError(f"acceptance journal does not belong to Ticket {slug!r}")
    if journal.get("participants") != participants:
        raise BoundaryError("sealed repository participants changed after acceptance began")
    if set(journal) != _JOURNAL_FIELDS:
        raise BoundaryError("acceptance journal has invalid fields")
    if journal.get("schema") != 4:
        raise BoundaryError("acceptance journal schema must be 4")
    transaction = require_str(journal, "transaction")
    if not re.fullmatch(r"[0-9a-f]{32}", transaction):
        raise BoundaryError("acceptance journal transaction is invalid")
    state = _validated_state(journal)
    actual_cleanup = _validated_policy(journal.get("policy"), cleanup=cleanup)
    stored_removals = _validated_removals(journal, removal_targets)
    roles = {item["role"] for item in participants}
    sources = _validated_string_map(journal.get("sources"), "acceptance journal sources", roles)
    candidates = _validated_candidates(journal.get("candidates"), roles, transaction)
    published = require_list(journal.get("published"), field="acceptance journal published")
    cleaned = require_list(journal.get("cleaned"), field="acceptance journal cleaned")
    _validate_progress(
        state,
        roles,
        actual_cleanup,
        sources,
        candidates,
        published,
        cleaned,
        has_removals=bool(stored_removals),
    )
    return _normalized(
        journal,
        state,
        actual_cleanup,
        sources,
        candidates,
        published,
        cleaned,
        stored_removals,
    )


def _validated_removals(
    journal: dict[str, Any], expected: tuple[str, ...] | None
) -> tuple[str, ...]:
    raw = require_list(journal.get("removal_targets"), field="acceptance journal removals")
    removals = tuple(require_str({"target": item}, "target") for item in raw)
    if expected is not None and removals != expected:
        raise BoundaryError("acceptance journal removal policy changed after acceptance began")
    if journal.get("removal_digest") != _removal_digest(removals):
        raise BoundaryError("acceptance journal removal digest is invalid")
    return removals


def _normalized(
    journal: dict[str, Any],
    state: JournalState,
    cleanup: bool,
    sources: dict[str, str],
    candidates: dict[str, Candidate],
    published: list[Any],
    cleaned: list[Any],
    removal_targets: tuple[str, ...],
) -> AcceptanceJournal:
    return {
        "schema": 4,
        "transaction": journal["transaction"],
        "ticket": journal["ticket"],
        "state": state,
        "policy": {"merge": True, "cleanup": cleanup},
        "participants": journal["participants"],
        "sources": sources,
        "candidates": candidates,
        "published": published,
        "cleaned": cleaned,
        "removal_targets": list(removal_targets),
        "removal_digest": _removal_digest(removal_targets),
    }


def upgrade_legacy_journal(
    value: Any, *, cleanup: bool, removal_targets: tuple[str, ...] = ()
) -> Any:
    """Upgrade historical journal shapes without inventing a prepared identity."""
    if not isinstance(value, dict) or value.get("schema") not in {1, 2, 3}:
        return value
    fields = set(value)
    schema = value["schema"]
    is_schema_one = schema == 1 and fields == _BASE_JOURNAL_FIELDS
    is_cleanup_two = schema == 2 and fields == _BASE_JOURNAL_FIELDS | _CLEANUP_FIELDS
    is_finalization_two = schema == 2 and fields == _BASE_JOURNAL_FIELDS | _FINALIZATION_FIELDS
    is_schema_three = (
        schema == 3 and fields == _BASE_JOURNAL_FIELDS | _CLEANUP_FIELDS | _FINALIZATION_FIELDS
    )
    if not (is_schema_one or is_cleanup_two or is_finalization_two or is_schema_three):
        return value
    upgraded = dict(value)
    if is_finalization_two:
        stored = tuple(upgraded.get("removal_targets", ()))
        if removal_targets and stored != removal_targets:
            return value
        upgraded.update(policy={"merge": True, "cleanup": cleanup}, cleaned=[])
    elif is_schema_one or is_cleanup_two:
        if removal_targets:
            return value
        if is_schema_one:
            upgraded.update(policy={"merge": True, "cleanup": cleanup}, cleaned=[])
        upgraded.update(
            removal_targets=[],
            removal_digest=_removal_digest(()),
            finalized=True,
        )
    elif removal_targets and tuple(upgraded.get("removal_targets", ())) != removal_targets:
        return value
    stored_removals = tuple(upgraded.get("removal_targets", ()))
    finalized = upgraded.pop("finalized")
    candidates = upgraded.get("candidates")
    if isinstance(candidates, dict):
        upgraded["candidates"] = {
            role: _upgrade_legacy_candidate(raw, bool(stored_removals), finalized)
            for role, raw in candidates.items()
        }
    upgraded["schema"] = 4
    if upgraded["state"] == "done" and cleanup and (is_schema_one or is_cleanup_two):
        upgraded["state"] = JournalState.ACCEPTED
    return upgraded


def _upgrade_legacy_candidate(raw: Any, has_removals: bool, finalized: Any) -> Any:
    if not isinstance(raw, dict) or set(raw) != {
        "sha",
        "staging_ref",
        "expected_destination_sha",
    }:
        return raw
    sha = raw["sha"]
    return {
        "prepared_sha": None if has_removals and finalized is True else sha,
        "finalized_sha": sha if finalized is True else None,
        "staging_ref": raw["staging_ref"],
        "expected_destination_sha": raw["expected_destination_sha"],
    }


def read_json(path: Path) -> Any:
    """Read one journal file, failing loudly on filesystem or JSON errors."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AcceptanceJournalError(f"acceptance journal is unreadable: {path}: {exc}") from exc


def load_journal(
    path: Path,
    slug: str,
    participants: list[dict[str, str]],
    *,
    cleanup: bool,
    removal_targets: tuple[str, ...] = (),
) -> AcceptanceJournal:
    """Read and validate one Ticket's journal, including schema-1 recovery."""
    value = upgrade_legacy_journal(
        read_json(path), cleanup=cleanup, removal_targets=removal_targets
    )
    try:
        return validate_journal(
            value,
            slug,
            participants,
            cleanup=cleanup,
            removal_targets=removal_targets,
        )
    except BoundaryError as exc:
        raise AcceptanceJournalError(f"acceptance journal is malformed: {path}: {exc}") from exc


def load_persisted_journal(path: Path) -> AcceptanceJournal:
    """Read and fully validate a journal when its Ticket contract is unavailable."""
    value = read_json(path)
    try:
        mapping = require_dict(value, field="acceptance journal")
        slug = require_str(mapping, "ticket")
        participants = _validated_participants(mapping.get("participants"))
        cleanup = _persisted_cleanup(mapping)
        upgraded = upgrade_legacy_journal(mapping, cleanup=cleanup)
        return validate_journal(
            upgraded,
            slug,
            participants,
            cleanup=cleanup,
            removal_targets=None,
        )
    except BoundaryError as exc:
        raise AcceptanceJournalError(f"acceptance journal is malformed: {path}: {exc}") from exc


def _persisted_cleanup(journal: dict[str, Any]) -> bool:
    schema = journal.get("schema")
    fields = set(journal)
    if schema == 1 or (schema == 2 and fields == _BASE_JOURNAL_FIELDS | _FINALIZATION_FIELDS):
        return False
    if schema not in {2, 3, 4}:
        raise BoundaryError("acceptance journal schema must be 1, 2, 3, or 4")
    return _validated_policy(journal.get("policy"), cleanup=None)


def acceptance_state(tickets_dir: Path, slug: str) -> JournalState | None:
    """Return validated acceptance progress for one Ticket Board entry."""
    path = tickets_dir.parent / ".runtime" / "acceptance" / f"{slug}.json"
    if not path.exists():
        return None
    return JournalState(load_persisted_journal(path)["state"])

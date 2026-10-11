"""Test composition for generic ledger recovery with a legacy byte fixture.

This codec and deterministic completion fence are test inputs to the generic
ledger. Interactive production recording rejects every V2 transaction.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from booley.criteria import evidence_ledger
from booley.criteria.evidence_ledger import (
    AcceptanceLedgerError,
    AcceptanceTransaction,
    EvidenceRef,
    EvidenceScope,
    canonical_json,
    plain_json_value,
    read_evidence_records,
)
from booley.criteria.state import CriterionChange, DevelopmentState

_HEX_32 = re.compile(r"[0-9a-f]{32}")


def _identity(value: object, generation: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise AcceptanceLedgerError("ticket identity must be an object")
    identity = plain_json_value(value)
    if (
        not isinstance(generation, str)
        or _HEX_32.fullmatch(generation) is None
        or identity.get("generation") != generation
    ):
        raise AcceptanceLedgerError("ticket generation must match the canonical identity")
    canonical_json(identity)
    return identity


class FixtureIdentity:
    """Identity codec for ``ticket_acceptance`` evidence.

    A V2 identity is a JSON object whose ``generation`` is 32 lowercase hex
    digits; envelopes carry ``ticket: {slug, identity, generation}`` and
    records carry ``ticket`` (the slug) and ``ticket_identity``.
    """

    subject_field = "ticket"
    subject_fields = frozenset({"slug", "identity", "generation"})
    identity_field = "ticket_identity"

    def lookup_identity(self, identity: Mapping[str, Any]) -> dict[str, Any]:
        generation = identity.get("generation")
        return {
            "ticket_identity": _identity(identity, generation),
            "ticket_generation": generation,
        }

    def envelope_subject(
        self, state: DevelopmentState, lookup_identity: Mapping[str, Any]
    ) -> dict[str, Any]:
        if not state.slug:
            raise AcceptanceLedgerError("Ticket slug must not be empty")
        return {
            "slug": state.slug,
            "identity": lookup_identity["ticket_identity"],
            "generation": lookup_identity["ticket_generation"],
        }

    def validate_envelope_subject(self, subject: Mapping[str, Any]) -> None:
        if not isinstance(subject.get("slug"), str) or not subject["slug"]:
            raise AcceptanceLedgerError("invalid transaction Ticket slug")
        _identity(subject.get("identity"), subject.get("generation"))

    def subject_lookup_identity(self, subject: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "ticket_identity": subject["identity"],
            "ticket_generation": subject["generation"],
        }

    def record_subject(self, subject: Mapping[str, Any]) -> dict[str, Any]:
        return {"ticket": subject["slug"], "ticket_identity": subject["identity"]}

    def append_subject(
        self, state: DevelopmentState, identity: Mapping[str, Any] | None
    ) -> dict[str, Any]:
        return {"ticket": state.slug, "ticket_identity": dict(identity or {})}

    def validate_observation_identity(self, value: object, current: Mapping[str, Any]) -> None:
        if not isinstance(value, dict):
            raise AcceptanceLedgerError("observation identity must be an object")


def record_changes(
    log_dir: Path,
    state: DevelopmentState,
    changes: list[CriterionChange],
    *,
    invocation_id: str,
    producer: str,
    execution_id: str,
    identity: Mapping[str, Any] | None = None,
    recorded_at: str | None = None,
    transaction_id: str = "",
) -> tuple[EvidenceRef, ...]:
    """Persist normalized effective Criterion changes in completion order."""
    return evidence_ledger.record_changes(
        log_dir,
        state,
        changes,
        scope=FIXTURE_SCOPE,
        invocation_id=invocation_id,
        producer=producer,
        execution_id=execution_id,
        identity=identity,
        recorded_at=recorded_at,
        transaction_id=transaction_id,
    )


def record_or_verify_transaction(
    log_dir: Path,
    state: DevelopmentState,
    changes: list[CriterionChange],
    *,
    acceptance_facts: Mapping[str, Any],
    identity: Mapping[str, Any],
    publication_checkpoint: Callable[[str], None] | None = None,
) -> AcceptanceTransaction:
    """Record or verify one intent-frozen Simulation Campaign transaction."""
    return evidence_ledger.record_or_verify_transaction(
        log_dir,
        state,
        changes,
        scope=FIXTURE_SCOPE,
        projection=FIXTURE_PROJECTION,
        acceptance_facts=acceptance_facts,
        identity=identity,
        publication_checkpoint=publication_checkpoint,
    )


def current_evidence_records(
    log_dir: Path, state: DevelopmentState, identity: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Read validated observations for one identity without replaying history."""
    return read_evidence_records(FIXTURE_SCOPE, log_dir, state, identity)


class CompletionFence:
    """A deterministic owner policy used to exercise projection during recovery."""

    def current_observations(self, log_dir, state, identity, records):
        return records

    def project_state(self, state, log_dir, identity):
        entry = state.criteria.get("_report_submitted")
        if entry is not None:
            entry.met = False

    def effective_met(self, log_dir, criterion, met, detail, identity):
        return False if criterion == "_report_submitted" else met


FIXTURE_SCOPE = EvidenceScope("ticket_acceptance", FixtureIdentity())
FIXTURE_PROJECTION = CompletionFence()


class FixtureRecorder:
    """Test owner supplying V2 identity to generic Flow recording seams."""

    def __init__(self, *, log_dir=None, identity=None, execution_id=None):
        self.log_dir = log_dir
        self.identity = identity or {}
        self.execution_id = execution_id

    def acceptance_identity(self):
        return self.identity

    def _logs(self):
        import os

        value = self.log_dir or os.environ.get("BOOLEY_LOGS_DIR")
        return Path(value) if value else None

    def record_changes(
        self, state, changes, *, invocation_id=None, producer=None, transaction_id=""
    ):
        root = self._logs()
        if root is None:
            return []
        return record_changes(
            root,
            state,
            changes,
            invocation_id=invocation_id,
            producer=producer,
            execution_id=self.execution_id,
            identity=self.identity,
            transaction_id=transaction_id,
        )

    def record_or_verify_transaction(self, state, changes, **kwargs):
        root = self._logs()
        if root is None:
            return None
        return record_or_verify_transaction(root, state, changes, **kwargs)

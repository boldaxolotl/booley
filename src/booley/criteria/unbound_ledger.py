"""V1 evidence composition for Interactive calls without a Goal binding."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, NoReturn

from booley.criteria import evidence_ledger
from booley.criteria.evidence_ledger import AcceptanceLedgerError, EvidenceRef, EvidenceScope
from booley.criteria.state import CriterionChange, DevelopmentState


class UnboundIdentity:
    """Preserve the V1 envelope fields; unbound calls have no V2 authority."""

    subject_field = "ticket"
    subject_fields = frozenset({"slug", "identity", "generation"})
    identity_field = "ticket_identity"

    def append_subject(
        self, state: DevelopmentState, identity: Mapping[str, Any] | None
    ) -> dict[str, Any]:
        return {"ticket": state.slug, "ticket_identity": {}}

    def lookup_identity(self, identity: Mapping[str, Any]) -> NoReturn:
        reject_transaction(identity)

    def envelope_subject(
        self, state: DevelopmentState, lookup_identity: Mapping[str, Any]
    ) -> NoReturn:
        reject_transaction(lookup_identity)

    def validate_envelope_subject(self, subject: Mapping[str, Any]) -> NoReturn:
        reject_transaction(subject)

    def subject_lookup_identity(self, subject: Mapping[str, Any]) -> NoReturn:
        reject_transaction(subject)

    def record_subject(self, subject: Mapping[str, Any]) -> NoReturn:
        reject_transaction(subject)

    def validate_observation_identity(self, value: object, current: Mapping[str, Any]) -> None:
        if value != {} or current:
            raise AcceptanceLedgerError("unbound evidence must have an empty identity")


UNBOUND_SCOPE = EvidenceScope("ticket_acceptance", UnboundIdentity())


def reject_transaction(identity: Mapping[str, Any]) -> NoReturn:
    if not identity:
        raise AcceptanceLedgerError("ticket generation must match the canonical identity")
    raise AcceptanceLedgerError("unbound recording cannot authorize a V2 transaction")


def record_changes(
    log_dir: Path,
    state: DevelopmentState,
    changes: list[CriterionChange],
    *,
    invocation_id: str,
    producer: str,
    execution_id: str,
    transaction_id: str = "",
) -> tuple[EvidenceRef, ...]:
    return evidence_ledger.record_changes(
        log_dir,
        state,
        changes,
        scope=UNBOUND_SCOPE,
        invocation_id=invocation_id,
        producer=producer,
        execution_id=execution_id,
        transaction_id=transaction_id,
    )


def record_or_verify_transaction(
    log_dir: Path,
    state: DevelopmentState,
    changes: list[CriterionChange],
    *,
    acceptance_facts: Mapping[str, Any],
    ticket_identity: Mapping[str, Any],
) -> NoReturn:
    reject_transaction(ticket_identity)

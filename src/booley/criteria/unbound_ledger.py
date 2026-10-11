"""V1 evidence composition for Interactive calls without a Goal binding."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from booley.criteria import evidence_ledger
from booley.criteria.evidence_ledger import (
    AcceptanceLedgerError,  # noqa: F401 - shared error re-export
    EvidenceAppendScope,
    EvidenceRef,
)
from booley.criteria.state import CriterionChange, DevelopmentState


class UnboundIdentity:
    """Preserve the V1 envelope fields; unbound calls have no V2 authority."""

    def append_subject(
        self, state: DevelopmentState, identity: Mapping[str, Any] | None
    ) -> dict[str, Any]:
        return {"ticket": state.slug, "ticket_identity": {}}


UNBOUND_SCOPE = EvidenceAppendScope("ticket_acceptance", UnboundIdentity())


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

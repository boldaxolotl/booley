"""Record Interactive evidence and leave freshness policy with its producer."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from booley.criteria import unbound_ledger
from booley.criteria.state import CriterionChange, DevelopmentState
from booley.flows.execution_persistence import AcceptanceRecordingError


class UnboundAcceptanceRecorder:
    """Persist V1 Criterion changes using the Interactive log layout."""

    def __init__(self, *, log_dir: Path | None = None, execution_id: str | None = None) -> None:
        self._log_dir = log_dir
        self._execution_id = execution_id

    def _logs(self) -> Path | None:
        if self._log_dir is not None:
            return self._log_dir
        raw = os.environ.get("BOOLEY_LOGS_DIR", "")
        return Path(raw) if raw else None

    def acceptance_identity(self) -> dict[str, Any]:
        return {}

    def record_changes(
        self,
        state: DevelopmentState,
        changes: list[CriterionChange],
        *,
        invocation_id: str,
        producer: str,
        transaction_id: str | None = None,
    ) -> None:
        log_dir = self._logs()
        if log_dir is None:
            return
        try:
            unbound_ledger.record_changes(
                log_dir,
                state,
                changes,
                invocation_id=os.environ.get("BOOLEY_RUN_ID") or invocation_id,
                producer=producer,
                execution_id=(
                    self._execution_id
                    if self._execution_id is not None
                    else os.environ.get("BOOLEY_EXECUTION_ID", "")
                ),
                transaction_id=transaction_id or "",
            )
        except unbound_ledger.AcceptanceLedgerError as exc:
            raise AcceptanceRecordingError(str(exc)) from exc

    def record_or_verify_transaction(
        self,
        state: DevelopmentState,
        changes: list[CriterionChange],
        *,
        acceptance_facts: Mapping[str, Any],
        ticket_identity: Mapping[str, Any],
    ) -> None:
        log_dir = self._logs()
        if log_dir is None:
            return
        try:
            unbound_ledger.record_or_verify_transaction(
                log_dir,
                state,
                changes,
                acceptance_facts=acceptance_facts,
                ticket_identity=ticket_identity,
            )
        except unbound_ledger.AcceptanceLedgerError as exc:
            raise AcceptanceRecordingError(str(exc)) from exc

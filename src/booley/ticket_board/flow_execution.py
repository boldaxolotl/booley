"""Ticket Board adapter for deterministic Flow execution and recording."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from booley.core.boundary import BoundaryError, require_dict
from booley.criteria.state import CriterionChange, DevelopmentState
from booley.evidence.acceptance import (
    PairedProjectBaseline,
    ResolvedFlowAcceptance,
)
from booley.flows.request import FlowRequest
from booley.runtime.endpoint_execution import EXIT_ERROR, EndpointOutcome
from booley.runtime.project_dir import resolve_checkout_project_dir
from booley.runtime.project_repositories import paired_project_repository

from . import acceptance_ledger
from .acceptance_basis import BLOCK_REASON, AcceptanceBasis, AcceptanceBasisError
from .acceptance_targets import resolve_commit
from .acceptance_validation import assert_ticket_worktree_inputs_unchanged
from .frontmatter import parse_frontmatter
from .helpers import TicketSlugError, detect_project_root, resolve_runtime_ticket_slug
from .io import TicketIO
from .paths import ticket_runtime_dir


class TicketAcceptanceRecorder:
    """Persist Criterion changes using the existing Ticket ledger layout."""

    def __init__(
        self,
        *,
        log_dir: Path | None = None,
        execution_id: str | None = None,
        acceptance_basis: dict[str, Any] | None = None,
    ) -> None:
        self._log_dir = log_dir
        self._execution_id = execution_id
        self._basis_record = acceptance_basis

    def _acceptance_basis_record(self) -> dict[str, Any]:
        if self._basis_record is not None:
            return self._basis_record
        ticket_file = os.environ.get("BOOLEY_TICKET_FILE", "")
        if not ticket_file or not Path(ticket_file).is_file():
            return {}
        fields, _body = parse_frontmatter(Path(ticket_file).read_text(encoding="utf-8"))
        raw_basis = fields.get("acceptance_basis")
        if raw_basis is None:
            return {}
        try:
            return require_dict(raw_basis, field="acceptance_basis")
        except BoundaryError as exc:
            from booley.flows.execution_persistence import AcceptanceRecordingError

            raise AcceptanceRecordingError(str(exc)) from exc

    def record_changes(
        self,
        state: DevelopmentState,
        changes: list[CriterionChange],
        *,
        invocation_id: str,
        producer: str,
        transaction_id: str | None = None,
    ) -> None:
        log_dir = self._log_dir
        if log_dir is None:
            raw_logs_dir = os.environ.get("BOOLEY_LOGS_DIR", "")
            log_dir = Path(raw_logs_dir) if raw_logs_dir else None
        if log_dir is None:
            if os.environ.get("BOOLEY_TICKET_FILE"):
                from booley.flows.execution_persistence import AcceptanceRecordingError

                raise AcceptanceRecordingError(
                    "ticket execution has no Criterion evidence directory"
                )
            return
        try:
            acceptance_ledger.record_changes(
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
                acceptance_basis=self._acceptance_basis_record(),
                transaction_id=transaction_id,
            )
        except acceptance_ledger.AcceptanceLedgerError as exc:
            from booley.flows.execution_persistence import AcceptanceRecordingError

            raise AcceptanceRecordingError(str(exc)) from exc


class TicketBoardFlowExecution(TicketAcceptanceRecorder):
    """Validate Ticket authority and adapt it to Flow-owned execution values."""

    def validate_and_resolve(
        self,
        request: FlowRequest,
    ) -> ResolvedFlowAcceptance | EndpointOutcome:
        try:
            basis, project_root = self._load_basis()
            assert_ticket_worktree_inputs_unchanged(project_root, basis, request.work_dir)
            paired = self._paired_project_baseline(request.work_dir, basis.project_sha)
            self._configure_runtime(request)
            return ResolvedFlowAcceptance(tuple(basis.bindings), paired, ticket_backed=True)
        except TicketSlugError as exc:
            return self._blocked(f"{BLOCK_REASON}: {exc}")
        except (OSError, AcceptanceBasisError) as exc:
            return self._blocked(str(exc))

    def _load_basis(self) -> tuple[AcceptanceBasis, Path]:
        raw_ticket_path = os.environ.get("BOOLEY_TICKET_FILE", "")
        if not raw_ticket_path:
            raise AcceptanceBasisError("ticket snapshot is unavailable")
        ticket_path = Path(raw_ticket_path)
        if not ticket_path.is_file():
            raise AcceptanceBasisError("ticket snapshot is unavailable")
        slug = resolve_runtime_ticket_slug(ticket_path)
        project_root = detect_project_root()
        basis = TicketIO(
            resolve_checkout_project_dir(project_root) / "tickets",
            project_root=project_root,
        ).load_basis(slug, runtime_ticket_path=ticket_path)
        self._basis_record = basis.as_dict()
        return basis, project_root

    @staticmethod
    def _paired_project_baseline(work_dir: Path, project_sha: str) -> PairedProjectBaseline:
        repository = paired_project_repository(Path(work_dir))
        if repository is None:
            return PairedProjectBaseline.absent()
        if not project_sha:
            raise AcceptanceBasisError(
                "paired Project Ticket execution requires a pinned Acceptance Basis commit"
            )
        try:
            sha = resolve_commit(repository.worktree, project_sha)
        except ValueError as exc:
            raise AcceptanceBasisError(
                f"recorded paired Project Acceptance Basis cannot be resolved: {exc}"
            ) from exc
        return PairedProjectBaseline.ticket_pinned(sha)

    @staticmethod
    def _configure_runtime(request: FlowRequest) -> None:
        logs_dir = os.environ.get("BOOLEY_LOGS_DIR", "")
        if not logs_dir:
            raise AcceptanceBasisError("ticket execution has no Criterion evidence directory")
        runtime_env = os.environ.get("BOOLEY_RUNTIME_DIR", "")
        if not runtime_env:
            runtime_env = str(ticket_runtime_dir(logs_dir))
            os.environ["BOOLEY_RUNTIME_DIR"] = runtime_env
        if request.report_dir is None:
            request.report_dir = Path(runtime_env) / "flow-reports"

    @staticmethod
    def _blocked(reason: str) -> EndpointOutcome:
        return EndpointOutcome(exit_code=EXIT_ERROR, report_text=f"BLOCKED: {reason}")

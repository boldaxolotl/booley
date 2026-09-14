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
from .acceptance_targets import resolve_commit
from .acceptance_validation import assert_ticket_worktree_inputs_unchanged
from .frontmatter import parse_frontmatter
from .helpers import TicketSlugError, detect_project_root, resolve_runtime_ticket_slug
from .io import TicketIO
from .paths import ticket_runtime_dir
from .ticket_baseline import BLOCK_REASON, TicketBaseline, TicketBaselineError


class TicketAcceptanceRecorder:
    """Persist Criterion changes using the existing Ticket ledger layout."""

    def __init__(
        self,
        *,
        log_dir: Path | None = None,
        execution_id: str | None = None,
        ticket_identity: dict[str, Any] | None = None,
    ) -> None:
        self._log_dir = log_dir
        self._execution_id = execution_id
        self._ticket_identity = ticket_identity

    def _validated_ticket_identity(self) -> dict[str, Any]:
        if self._ticket_identity is not None:
            return self._ticket_identity
        ticket_file = os.environ.get("BOOLEY_TICKET_FILE", "")
        if not ticket_file or not Path(ticket_file).is_file():
            return {}
        fields, body = parse_frontmatter(Path(ticket_file).read_text(encoding="utf-8"))
        try:
            from .ticket_baseline import ticket_baseline_from_fields

            ticket_baseline_from_fields(fields, body)
            return require_dict(fields["machine"], field="machine")
        except (BoundaryError, TicketBaselineError) as exc:
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
                ticket_identity=self._validated_ticket_identity(),
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
            basis, project_root, slug, ticket_path = self._load_basis()
            assert_ticket_worktree_inputs_unchanged(
                project_root,
                basis,
                request.work_dir,
                slug=slug,
                ticket_path=ticket_path,
            )
            paired = self._paired_project_baseline(request.work_dir, basis.project_sha)
            self._configure_runtime(request)
            return ResolvedFlowAcceptance(tuple(basis.bindings), paired, ticket_backed=True)
        except TicketSlugError as exc:
            return self._blocked(f"{BLOCK_REASON}: {exc}")
        except (OSError, TicketBaselineError) as exc:
            return self._blocked(str(exc))

    def _load_basis(self) -> tuple[TicketBaseline, Path, str, Path]:
        raw_ticket_path = os.environ.get("BOOLEY_TICKET_FILE", "")
        if not raw_ticket_path:
            raise TicketBaselineError("ticket snapshot is unavailable")
        ticket_path = Path(raw_ticket_path)
        if not ticket_path.is_file():
            raise TicketBaselineError("ticket snapshot is unavailable")
        slug = resolve_runtime_ticket_slug(ticket_path)
        project_root = detect_project_root()
        basis = TicketIO(
            resolve_checkout_project_dir(project_root) / "tickets",
            project_root=project_root,
        ).load_basis(slug, runtime_ticket_path=ticket_path)
        self._ticket_identity = basis.ticket_identity()
        return basis, project_root, slug, ticket_path

    @staticmethod
    def _paired_project_baseline(work_dir: Path, project_sha: str) -> PairedProjectBaseline:
        repository = paired_project_repository(Path(work_dir))
        if repository is None:
            return PairedProjectBaseline.absent()
        if not project_sha:
            raise TicketBaselineError(
                "paired Project Ticket execution requires a pinned Ticket baseline commit"
            )
        try:
            sha = resolve_commit(repository.worktree, project_sha)
        except ValueError as exc:
            raise TicketBaselineError(
                f"recorded paired Project Ticket baseline cannot be resolved: {exc}"
            ) from exc
        return PairedProjectBaseline.ticket_pinned(sha)

    @staticmethod
    def _configure_runtime(request: FlowRequest) -> None:
        logs_dir = os.environ.get("BOOLEY_LOGS_DIR", "")
        if not logs_dir:
            raise TicketBaselineError("ticket execution has no Criterion evidence directory")
        runtime_env = os.environ.get("BOOLEY_RUNTIME_DIR", "")
        if not runtime_env:
            runtime_env = str(ticket_runtime_dir(logs_dir))
            os.environ["BOOLEY_RUNTIME_DIR"] = runtime_env
        if request.report_dir is None:
            request.report_dir = Path(runtime_env) / "flow-reports"

    @staticmethod
    def _blocked(reason: str) -> EndpointOutcome:
        return EndpointOutcome(exit_code=EXIT_ERROR, report_text=f"BLOCKED: {reason}")

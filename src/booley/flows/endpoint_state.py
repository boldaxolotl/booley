"""Per-invocation execution state and shared endpoint services.

Parsing, acceptance, admission and publication have separate owners. Structured Flow sessions and legacy adapters use the same recording and
publication operations; this module does not construct parsers.
"""

from __future__ import annotations

import logging
import uuid
from abc import ABC, abstractmethod
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, ClassVar

from booley.criteria.state import (
    CriterionChange,
    DevelopmentState,
)
from booley.flows import (
    endpoint_acceptance,
    endpoint_admission,
    endpoint_reporting,
    endpoint_session,
    execution,
)
from booley.flows.endpoint_session import PreparedExecution
from booley.runtime import job_slots
from booley.runtime.endpoint_execution import (
    EndpointOutcome,
    ExecutionResult,
    execute_endpoint,
)

logger = logging.getLogger(__name__)


class EndpointState(ABC):
    """Per-invocation state and services, independent of request transport."""

    endpoint_kind: ClassVar[str] = "mcp_tool"

    # --- Class-level metadata (override in subclasses) ---
    name: str = ""
    description: str = ""
    # Does this endpoint modify code? If True, post-run git diff triggers invalidation.
    code_modifying: bool = False
    # Category of code this endpoint modifies (rtl, tb, or None for auto-detect)
    modifies_category: str | None = None
    # Does this endpoint operate per Target? False suppresses the Target in display headers.
    config_aware: bool = True
    # Target-aware deterministic Flows override this so both argparse and the
    # generated MCP schema require an explicit selection.
    target_required: bool = False
    # Source-scoped endpoints may opt out of Target selection entirely.
    accepts_target: bool = True
    # Preview-only dry runs opt in to bypassing admission and persistence.
    non_persisting_dry_run: bool = False
    # F-14: on a human/standalone (no-state-file) run, ``report_text`` is only
    # surfaced on *failure* — the PASS verdict lives in ``display_lines``, which
    # the harness UI renders but a bare CLI run drops. For an endpoint whose success
    # is otherwise indistinguishable from a no-op (fpga_impl: a passing route
    # prints zero bytes, exit 0), set this True to also print ``report_text`` on
    # success, so PASS is never silent.
    announce_success_report: bool = False
    # Class-overridable because argparse help is also the MCP ``target`` schema
    # description. The default is endpoint-neutral; a Flow that drives a sim
    # sub-loop can override it with narrower wording.
    target_help: str = (
        "FuseSoC .core Target name(s) this run applies to, comma-separated. "
        "Run with --target <name> (list them with `booley targets`)."
    )
    # Criteria this endpoint can satisfy (must match names in criteria.toml)
    satisfies: ClassVar[list[str]] = []
    # Per-criterion CLI args hint for the developer (criterion -> args string)
    satisfies_args: ClassVar[dict[str, str]] = {}

    def _flow_enabled(self) -> bool:
        """Return whether this Flow is enabled in the scoped config."""
        work_dir = Path(getattr(self.args, "work_dir", ".")) if self.args else None
        return execution.flow_enabled(self.name, work_dir)

    @property
    def _selected_target(self) -> str:
        """The selected Target name used by shared report/display plumbing."""
        return getattr(self.args, "target", "") or ""

    @property
    def display_tag(self) -> str | None:
        """Optional tag shown in the endpoint box header (e.g. "rtl", "tb").

        Overrides config_aware when set. Available after arg parsing.
        """
        return None

    def __init__(self) -> None:
        self._args: Any = None
        self._state: DevelopmentState | None = None
        self._start_time: float = 0.0
        self._pre_run_head: str | None = None
        self._raw_argv: list[str] | None = None
        self._invocation_id = uuid.uuid4().hex
        self._reserved_invocation_dir: Path | None = None
        # Set for the duration of _run(); read by _post_run to avoid echoing a
        # verdict block the endpoint already printed itself (F-28).
        self._stdout_witness: endpoint_reporting._StdoutWitness | None = None
        self._pending_criteria_set: tuple[str, ...] | None = None
        # The underlying EDA tool that actually ran (e.g. "verilator",
        # "verible", "yosys", "vivado"). Endpoints set this at target-resolution
        # time; write_report() emits it as ``eda_tool`` so reports say which
        # binary produced the result — distinct from the Booley Flow name.
        self._eda_tool: str | None = None

    @property
    def args(self) -> Any:
        if self._args is None:
            raise RuntimeError("execution input has not been prepared")
        return self._args

    # --- State access ---

    def read_state(self) -> DevelopmentState:
        """Load development state from disk.

        When state_file is None (human mode), returns an empty in-memory state.
        """
        sf = self.args.state_file
        if sf is None:
            self._state = DevelopmentState()  # no file path => save() is a no-op
        else:
            self._state = DevelopmentState.load(sf)
        return self._state

    @property
    def state(self) -> DevelopmentState:
        if self._state is None:
            raise RuntimeError("read_state() not called")
        return self._state

    def set_criterion(
        self,
        key: str,
        met: bool,
        *,
        detail: dict[str, Any] | None = None,
        source_target: str | None = None,
    ) -> None:
        return endpoint_acceptance.set_criterion(
            self, key, met, detail=detail, source_target=source_target
        )

    def _record_acceptance_changes(self, changes: list[CriterionChange]) -> None:
        return endpoint_acceptance._record_acceptance_changes(self, changes)

    def _criterion_key_for_source(self, key: str, source_target: str | None) -> str:
        return endpoint_acceptance._criterion_key_for_source(self, key, source_target)

    def _stamp_source_fingerprint(
        self,
        key: str,
        met: bool,
        detail: dict[str, Any] | None,
        *,
        source_target: str | None,
    ) -> dict[str, Any] | None:
        return endpoint_acceptance._stamp_source_fingerprint(
            self, key, met, detail, source_target=source_target
        )

    def emit_progress(self, line: str) -> None:
        return endpoint_reporting.emit_progress(self, line)

    def emit_completion(self, line: str, *, repeats_at_end: bool = False) -> None:
        return endpoint_reporting.emit_completion(self, line, repeats_at_end=repeats_at_end)

    # --- Report ---

    def _next_invocation_dir(self, report_dir: Path) -> Path:
        return endpoint_reporting._next_invocation_dir(self, report_dir)

    def reserve_invocation_dir(self) -> Path | None:
        return endpoint_reporting.reserve_invocation_dir(self)

    def write_report(self, result: EndpointOutcome) -> Path | None:
        return endpoint_reporting.write_report(self, result)

    def _warn_no_report_artifact(self) -> None:
        return endpoint_reporting._warn_no_report_artifact(self)

    # --- Git helpers ---

    def _get_head_sha(self) -> str | None:
        return endpoint_reporting._get_head_sha(self)

    # --- Git diff classification ---

    def classify_git_diff(self) -> set[str]:
        return endpoint_acceptance.classify_git_diff(self)

    def invalidate_dependent_criteria(self) -> list[str]:
        return endpoint_acceptance.invalidate_dependent_criteria(self)

    # --- Pre-run guardrails ---

    def _default_target_args(self) -> None:
        return endpoint_acceptance._default_target_args(self)

    def _requested_targets(self) -> list[str]:
        return endpoint_acceptance._requested_targets(self)

    def _bound_criterion_keys(self, target: str) -> list[str]:
        return endpoint_acceptance._bound_criterion_keys(self, target)

    def _criterion_target_matches(
        self,
        params: dict[str, Any],
        invoked: str,
        invoked_identity: str | None,
    ) -> bool:
        return endpoint_acceptance._criterion_target_matches(
            self, params, invoked, invoked_identity
        )

    def _criterion_binding_gate(self) -> EndpointOutcome | None:
        return endpoint_acceptance._criterion_binding_gate(self)

    def _is_non_persisting_dry_run(self) -> bool:
        """Return whether this invocation is an opted-in preview-only run."""
        return self.non_persisting_dry_run and bool(getattr(self.args, "dry_run", False))

    # --- Main execution ---

    @abstractmethod
    def _run(self) -> EndpointOutcome:
        """Execute the endpoint's core logic. Implemented by subclasses."""

    def _finalize_result(self, result: EndpointOutcome) -> None:
        return endpoint_reporting._finalize_result(self, result)

    def _stamp_git_diff_stats(self, result: EndpointOutcome) -> None:
        return endpoint_reporting._stamp_git_diff_stats(self, result)

    def _pre_state_gate(self) -> EndpointOutcome | None:
        """Reject before loading mutable ticket state; subclasses may override."""
        return None

    def _apply_pre_state_gate(self) -> EndpointOutcome | None:
        return endpoint_session._apply_pre_state_gate(self)

    def admission(self, prepared: PreparedExecution) -> AbstractContextManager[None]:
        return endpoint_admission.admission(self, prepared)

    def invoke_endpoint(
        self,
        prepared: PreparedExecution,
        *,
        started: float,
    ) -> EndpointOutcome:
        return endpoint_session.invoke_endpoint(self, prepared, started=started)

    def finish_execution(
        self,
        prepared: PreparedExecution,
        outcome: EndpointOutcome,
        *,
        started: float | None,
        acceptance_recorded: bool,
    ) -> int:
        return endpoint_session.finish_execution(
            self, prepared, outcome, started=started, acceptance_recorded=acceptance_recorded
        )

    def record_acceptance(
        self,
        prepared: PreparedExecution,
        outcome: EndpointOutcome,
    ) -> None:
        return endpoint_acceptance.record_acceptance(self, prepared, outcome)

    def _finish_main(
        self,
        result: EndpointOutcome,
        display_target: str | None,
        display_label: str | None,
        started: float | None,
        *,
        acceptance_recorded: bool,
        dry_run: bool,
        non_persisting_dry_run: bool,
    ) -> int:
        return endpoint_reporting._finish_main(
            self,
            result,
            display_target,
            display_label,
            started,
            acceptance_recorded=acceptance_recorded,
            dry_run=dry_run,
            non_persisting_dry_run=non_persisting_dry_run,
        )

    # Admission class for this endpoint. None = quick endpoint, no admission.
    JOB_CLASS: ClassVar[str | None] = None

    def _resolve_job_class(self) -> str | None:
        """The Job Class this call belongs to, or None for unclassed endpoints."""
        return self.JOB_CLASS

    def _acquire_job_slot(self) -> tuple[job_slots.SlotStore | None, object | None]:
        return endpoint_admission._acquire_job_slot(self)

    def _resolve_display_config(self) -> str | None:
        return endpoint_reporting._resolve_display_config(self)

    def _resolve_display_label(self) -> str | None:
        return endpoint_reporting._resolve_display_label(self)

    def _post_run(self, result: EndpointOutcome, duration: float) -> None:
        return endpoint_reporting._post_run(self, result, duration)

    def _pre_save_hook(self, result: EndpointOutcome) -> None:
        """Hook for subclasses to mutate ``self.state`` (or *result*) just before
        ``state.save()`` runs.  Default no-op."""
        return

    def _adapt_outcome(self, outcome: EndpointOutcome) -> EndpointOutcome:
        return outcome

    def execute_prepared(self) -> ExecutionResult:
        """Run configured input through the same gates and coordinator as CLI."""
        prepared = endpoint_session.prepare_execution(self)
        if isinstance(prepared, EndpointOutcome):
            return ExecutionResult(exit_code=prepared.exit_code, outcome=prepared)
        return execute_endpoint(self, prepared)

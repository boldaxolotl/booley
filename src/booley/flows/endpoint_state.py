"""Per-invocation execution state and shared endpoint services.

Parsing, admission and publication have separate owners. Built-in Flows and
legacy adapters use the same publication operation; this module does not
construct parsers.
"""

from __future__ import annotations

import logging
import os
import uuid
from abc import ABC, abstractmethod
from collections.abc import Sequence
from contextlib import AbstractContextManager
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from booley.flows import (
    endpoint_admission,
    endpoint_reporting,
    endpoint_session,
    execution,
)
from booley.flows.cli_help import CliHelp, SurfaceHelp
from booley.flows.endpoint_session import PreparedExecution
from booley.flows.job_estimates import JobRequest
from booley.flows.memory_admission import MemoryAdmissionPlan, measure_memory_claim
from booley.flows.prepared_invocation import OrdinaryRunInvocation, PreparedInvocation
from booley.flows.progress_lifecycle import retire_progress_of_requeued_attempt
from booley.runtime import job_slots
from booley.runtime.display_identity import DisplayIdentity
from booley.runtime.endpoint_execution import (
    OOM_REQUEUE_MARKER,
    EndpointOutcome,
    ExecutionResult,
    execute_endpoint,
)

if TYPE_CHECKING:
    from booley.flows.terminal_progress import TerminalProgress

logger = logging.getLogger(__name__)


def _booley_dir_or_none() -> Path | None:
    """The Booley Directory entry the report roots may sit behind, or ``None`` without one."""
    from booley.runtime.booley_dir import resolve_booley_dir

    try:
        return resolve_booley_dir()
    except (OSError, RuntimeError, ValueError):
        return None


class EndpointState(ABC):
    """Per-invocation state and services, independent of request transport."""

    endpoint_kind: ClassVar[str] = "mcp_tool"
    # The ``$schema`` the report document declares. Project-local endpoints
    # declare none; each shipped endpoint kind (and the Simulation Flow) names its own.
    report_schema: ClassVar[str | None] = None

    # --- Class-level metadata (override in subclasses) ---
    name: str = ""
    description: str = ""
    # Human `--help` wording; ``description`` stays the agent-facing tool text.
    cli_help: ClassVar[CliHelp | None] = None
    # Does this endpoint modify code? If True, the report carries the run's change statistics.
    code_modifying: bool = False
    # Target-aware deterministic Flows override this so both argparse and the
    # generated MCP schema require an explicit selection.
    target_required: bool = False
    # Source-scoped endpoints may opt out of Target selection entirely.
    accepts_target: bool = True
    # Preview-only dry runs opt in to bypassing admission and persistence.
    non_persisting_dry_run: bool = False
    # Successful non-Flow endpoints opt in to publishing report_text. CLI Flows
    # publish successful verdicts by default; failures publish for every endpoint.
    announce_success_report: bool = False
    # Class-overridable because argparse help is also the MCP ``target`` schema
    # description. The default is endpoint-neutral; a Flow that drives a sim
    # sub-loop can override it with narrower wording.
    target_metavar: str = "TARGET[,TARGET...]"  # the CLI's `--target` metavar
    target_help: str = SurfaceHelp(
        "FuseSoC .core Target name(s) this run applies to. The CLI flag may be "
        "repeated or its value comma-separated; MCP accepts one comma-separated string. "
        "List available Targets with `booley targets`.",
        cli=(
            "FuseSoC .core Target name(s) to run. Repeat the flag or separate names "
            "with commas. List available Targets with `booley targets`."
        ),
    )

    def _flow_enabled(self) -> bool:
        """Return whether this Flow is enabled in the scoped config."""
        work_dir = Path(getattr(self.args, "work_dir", ".")) if self.args else None
        return execution.flow_enabled(self.name, work_dir)

    @property
    def _selected_target(self) -> str:
        """The selected Target name used by shared report plumbing."""
        return getattr(self.args, "target", "") or ""

    def __init__(self) -> None:
        self._args: Any = None
        self._start_time: float = 0.0
        self._pre_run_head: str | None = None
        self._raw_argv: list[str] | None = None
        invocation_id = (
            os.environ.get("BOOLEY_RUN_ID")
            or os.environ.get("BOOLEY_DISPLAY_INVOCATION_ID")
            or uuid.uuid4().hex
        )
        self._display_identity = DisplayIdentity.current(invocation_id)
        self._invocation_id = self._display_identity.invocation_id
        self._reserved_invocation_dir: Path | None = None
        # The checkout and existing Booley Directory a run must not recreate.
        self._run_roots: tuple[Path, Path | None] | None = None
        # Failures retiring a killed attempt's progress; the final result reports them.
        self._requeue_cleanup_errors: list[Exception] = []
        self._skip_report_detail_refresh = False
        # The live terminal observer; only a CLI-attached Flow sets one.
        self.terminal_progress: TerminalProgress | None = None
        # An omitted-Target selection announced once on the CLI (see prepare_target_endpoint).
        self._automatic_target_notice = ""
        self._automatic_target_notice_published = False
        # Transport adapters opt in to automatic stdout/stderr verdict
        # publication. Typed in-process execution stays side-effect free.
        self._console_publication_requested = False
        # Set for the duration of _run(); console publication reads it to avoid
        # echoing a verdict block the endpoint already printed itself (F-28).
        self._stdout_witness: endpoint_reporting._StdoutWitness | None = None
        # The underlying EDA tool that actually ran (e.g. "verilator",
        # "verible", "yosys", "vivado"). Endpoints set this at target-resolution
        # time; write_report() emits it as ``eda_tool`` so reports say which
        # binary produced the result — distinct from the Booley Flow name.
        self._eda_tool: str | None = None
        self._eda_tools: dict[str, str | None] = {}

    def reset_report_metadata(self) -> None:
        """Start resolved EDA identity afresh for every invocation."""
        self._eda_tool = None
        self._eda_tools = {}
        self._automatic_target_notice = ""
        self._automatic_target_notice_published = False
        if self._args is None:
            return
        target = self._selected_target
        targets = target.split(",") if isinstance(target, str) else target
        self.set_report_targets([item.strip() for item in targets if item.strip()])

    def refresh_report_detail(self, result: EndpointOutcome) -> None:
        """Refresh derived report detail before it is written; most endpoints have none."""
        return

    def transcript_path(self) -> Path | None:
        """Path of this invocation's agent transcript, or ``None`` when it keeps none."""
        return None

    def prepare_target_endpoint(self) -> EndpointOutcome | None:
        """Resolve Target selection before admission; only built-in Flows select Targets."""
        return None

    def prepare_endpoint_invocation(self) -> PreparedInvocation | EndpointOutcome:
        """Return the prepared invocation, or the refusal that ends the run.

        The default wraps the endpoint's ordinary run.
        """
        return OrdinaryRunInvocation(self._run)

    def manifest_names_targets(self) -> bool:
        """Whether this invocation's Targets come from a manifest, not Target selection."""
        return False

    def set_report_targets(self, targets: list[str]) -> None:
        """Preserve selection cardinality even when some Targets never resolve."""
        self._eda_tools = {target: self._eda_tools.get(target) for target in targets}
        self._eda_tool = (
            next(iter(self._eda_tools.values())) if len(self._eda_tools) == 1 else None
        )

    def record_eda_tool(self, target: str, eda_tool: str | None) -> None:
        """Record the raw simulator or EDA identity resolved for a selected Target."""
        self._eda_tools[target] = eda_tool or None
        if len(self._eda_tools) == 1:
            self._eda_tool = eda_tool or None

    @property
    def args(self) -> Any:
        if self._args is None:
            raise RuntimeError("execution input has not been prepared")
        return self._args

    def set_criterion(
        self,
        key: str,
        met: bool,
        *,
        detail: dict[str, Any] | None = None,
        source_target: str | None = None,
    ) -> None:
        """Accept a Criterion verdict from a Project endpoint and record nothing."""
        return

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

    # --- Pre-run guardrails ---

    def _default_target_args(self) -> None:
        """Default well-known CLI arguments from the selected Target."""
        if hasattr(self.args, "tb_top") and not getattr(self.args, "tb_top", None):
            target = getattr(self.args, "target", "")
            if target:
                from booley.flows.flow_config import tb_top_for_target

                tb_top = tb_top_for_target(
                    target,
                    getattr(self.args, "work_dir", None),
                )
                if tb_top:
                    self.args.tb_top = tb_top

    def _requested_targets(self) -> list[str]:
        """Return normalized Target tokens without resolving or invoking EDA."""
        raw = getattr(self.args, "target", "")
        values = raw if isinstance(raw, list) else [raw]
        targets: list[str] = []
        for value in values:
            targets.extend(part.strip() for part in str(value or "").split(",") if part.strip())
        return targets

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
        """Reject before the Target is prepared and the run admitted; subclasses may override."""
        return None

    def _apply_pre_state_gate(self) -> EndpointOutcome | None:
        return endpoint_session._apply_pre_state_gate(self)

    def admission(
        self, prepared: PreparedExecution, *, requeued: bool = False
    ) -> AbstractContextManager[endpoint_admission.AdmissionContext | None]:
        return endpoint_admission.admission(self, prepared, requeued=requeued)

    def may_requeue_after_oom(self, prepared: PreparedExecution, admission: object | None) -> bool:
        """Whether an own OOM kill of this admitted Job's first attempt requeues it (ADR 0087).

        Only a Flow Job holding its own Memory Reservation: not a dry run, not a
        child under its parent's claim, not a Specialist or a tool. A companion
        claim is never requeued.
        """
        if self.endpoint_kind != "flow" or not isinstance(
            admission, endpoint_admission.AdmissionContext
        ):
            return False
        token = admission.outer_token
        if not isinstance(token, job_slots.SlotToken) or token.job_class != job_slots.CLASS_MEMORY:
            return False
        # a companion coordinator that came back run-alone would never run beside Specialists
        return token.memory is None or not token.memory.companion

    def begin_requeued_attempt(self) -> None:
        """Mark the stdout the killed attempt wrote and start the next attempt's report afresh.

        The second attempt reports into its own numbered directory; the killed
        attempt's directory keeps its partial artifacts and no report, and its
        progress document is retired so no caller reads it for this Job. A failed
        retirement does not stop the requeue: the final result reports it.
        """
        print(OOM_REQUEUE_MARKER, flush=True)
        killed_dir = self._reserved_invocation_dir
        if killed_dir is not None:
            try:
                retire_progress_of_requeued_attempt(
                    killed_dir / "progress.json",
                    endpoint=self.name,
                    run_id=os.environ.get("BOOLEY_RUN_ID", ""),
                    booley_dir=_booley_dir_or_none(),
                )
            except (OSError, ValueError) as exc:
                self._requeue_cleanup_errors.append(exc)
        self._reserved_invocation_dir = None

    def invoke_endpoint(
        self,
        prepared: PreparedExecution,
        *,
        admission: object | None,
        started: float,
    ) -> EndpointOutcome:
        return endpoint_session.invoke_endpoint(
            self, prepared, admission=admission, started=started
        )

    def finish_execution(
        self,
        prepared: PreparedExecution,
        outcome: EndpointOutcome,
        *,
        started: float | None,
    ) -> ExecutionResult:
        return endpoint_session.finish_execution(self, prepared, outcome, started=started)

    def _finish_main(
        self,
        result: EndpointOutcome,
        *,
        persist_report: bool,
    ) -> int:
        return endpoint_reporting._finish_main(self, result, persist_report=persist_report)

    # Admission class for this endpoint. None = quick endpoint, no admission.
    JOB_CLASS: ClassVar[str | None] = None

    def _resolve_job_class(self) -> str | None:
        """The Job Class this call belongs to, or None for unclassed endpoints."""
        return self.JOB_CLASS

    def _licensed_tools(self) -> tuple[str, ...]:
        """The licensed EDA tools one Job of this endpoint runs (it holds one seat of each).

        Memory admission judges each against its Seat Cap (ADR 0087); an endpoint
        that runs no licensed tool owes no seat.
        """
        return ()

    def _memory_admitted(self) -> bool:
        """Whether this Job is admitted by Memory Reservation (ADR 0087).

        Sim, synth and fpga Jobs do; lint and Custom Flows override this.
        """
        return self._resolve_job_class() == job_slots.CLASS_MEMORY

    def _memory_admission_plan(
        self, project: Path | None, requests: Sequence[JobRequest] | None
    ) -> MemoryAdmissionPlan:
        """The claim this memory-admitted Job makes: sized from its requests by default.

        Specialists and the Mutation Flow override it with a companion claim.
        """
        return measure_memory_claim(project, requests)

    def _publish_console_report(self, result: EndpointOutcome) -> None:
        return endpoint_reporting._publish_console_report(self, result)

    def execute_prepared(self) -> ExecutionResult:
        """Run configured input through the same gates and coordinator as CLI."""
        prepared = endpoint_session.prepare_execution(self)
        if isinstance(prepared, EndpointOutcome):
            return ExecutionResult(exit_code=prepared.exit_code, outcome=prepared)
        return execute_endpoint(self, prepared)

"""Preparation and invocation stages for the shared coordinator."""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from booley.flows.endpoint_cli import runtime_report_root
from booley.flows.endpoint_reporting import _StdoutWitness, removed_root, report_root_removed
from booley.flows.run_root_watchdog import RunRootWatchdog
from booley.runtime.endpoint_execution import (
    EXIT_ERROR,
    EndpointOutcome,
    ExecutionResult,
    normalize_completion_error,
)
from booley.runtime.exception_diagnostics import (
    exception_report_text,
    print_exception_diagnostic,
    write_exception_diagnostic,
)
from booley.runtime.filesystem_utils import run_roots
from booley.runtime.sandbox_layout import canonical_project_alias_path

if TYPE_CHECKING:
    from booley.flows.endpoint_state import EndpointState
    from booley.flows.prepared_invocation import PreparedInvocation


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PreparedExecution:
    """Adapter-owned state carried through the neutral execution sequence."""

    non_persisting_dry_run: bool
    invocation: PreparedInvocation


def _apply_pre_state_gate(endpoint: EndpointState) -> EndpointOutcome | None:
    """Render an early gate rejection without reading or writing ticket state."""
    result = endpoint._pre_state_gate()
    if result is None:
        return None
    endpoint._publish_console_report(result)
    return result


def _booley_dir_report_root(endpoint_kind: str, work_dir: Path) -> Path:
    from booley.runtime.booley_dir import resolve_checkout_booley_dir

    leaf = "flow-reports" if endpoint_kind == "flow" else "mcp-tool-reports"
    return resolve_checkout_booley_dir(work_dir) / leaf


def default_report_root(endpoint_kind: str, work_dir: Path) -> Path:
    """Report root a run without ``--report-dir`` writes to from this environment."""
    return runtime_report_root(endpoint_kind) or _booley_dir_report_root(endpoint_kind, work_dir)


def _apply_default_endpoint_report_root(endpoint: EndpointState) -> EndpointOutcome | None:
    """Select durable Booley Directory after adapters can supply a runtime root."""
    if (
        endpoint.endpoint_kind not in {"flow", "specialist"}
        or endpoint.args.report_dir is not None
    ):
        return None
    try:
        root = _booley_dir_report_root(endpoint.endpoint_kind, Path(endpoint.args.work_dir))
    except (OSError, RuntimeError, ValueError) as exc:
        result = EndpointOutcome(
            exit_code=EXIT_ERROR,
            report_text=f"Endpoint report directory could not be resolved: {exc}",
        )
        endpoint._publish_console_report(result)
        return result
    endpoint.args.report_dir = root
    return None


def _canonicalize_flow_report_root(endpoint: EndpointState) -> EndpointOutcome | None:
    """Normalize the image's authenticated Project alias before side effects."""
    if endpoint.endpoint_kind != "flow":
        return None
    try:
        endpoint.args.report_dir = canonical_project_alias_path(
            Path(endpoint.args.report_dir).absolute()
        )
    except ValueError as exc:
        result = EndpointOutcome(exit_code=EXIT_ERROR, report_text=str(exc))
        endpoint._publish_console_report(result)
        return result
    return None


def _prepare_endpoint_report_root(endpoint: EndpointState) -> EndpointOutcome | None:
    """Select the default root and canonicalize Flow report inputs."""
    result = _apply_default_endpoint_report_root(endpoint)
    return result if result is not None else _canonicalize_flow_report_root(endpoint)


def _record_run_roots(endpoint: EndpointState) -> None:
    """Remember the checkout and its existing Booley Directory, which the run must not recreate."""
    from booley.runtime.booley_dir import resolve_checkout_booley_dir

    checkout = Path(endpoint.args.work_dir).absolute()
    booley_dir: Path | None
    try:
        booley_dir = resolve_checkout_booley_dir(checkout)
    except (OSError, RuntimeError, ValueError):
        booley_dir = None
    if booley_dir is not None and not booley_dir.is_dir():
        booley_dir = None
    endpoint._run_roots = (checkout, booley_dir)
    endpoint._removed_run_root = None


def _live_roots(endpoint: EndpointState) -> list[Path]:
    """The recorded checkout and Booley Directory, which the run must not recreate."""
    return [root for root in endpoint._run_roots or () if root is not None]


def prepare_execution(
    endpoint: EndpointState,
) -> PreparedExecution | EndpointOutcome:
    """Adapt CLI arguments into one prepared execution request."""
    endpoint.reset_report_metadata()
    endpoint._requeue_cleanup_errors.clear()
    endpoint._stdout_witness = None
    if (early_outcome := endpoint._apply_pre_state_gate()) is not None:
        return early_outcome
    if (early_outcome := _prepare_endpoint_report_root(endpoint)) is not None:
        return early_outcome
    _record_run_roots(endpoint)
    endpoint._default_target_args()
    if (selection_error := _prepare_target(endpoint)) is not None:
        return selection_error
    invocation = endpoint.prepare_endpoint_invocation()
    if isinstance(invocation, EndpointOutcome):
        endpoint._publish_console_report(invocation)
        return invocation
    return _prepared_execution(endpoint, invocation)


def _prepare_target(endpoint: EndpointState) -> EndpointOutcome | None:
    """Resolve the Target selection, publishing a refusal."""
    target_error = endpoint.prepare_target_endpoint()
    if target_error is not None:
        endpoint.write_report(target_error)
        endpoint._publish_console_report(target_error)
    return target_error


def _prepared_execution(
    endpoint: EndpointState, invocation: PreparedInvocation
) -> PreparedExecution:
    """Carry the prepared invocation once all preparation gates have succeeded."""
    return PreparedExecution(
        non_persisting_dry_run=endpoint._is_non_persisting_dry_run(),
        invocation=invocation,
    )


def _transcript_diagnostic_path(endpoint: EndpointState) -> Path | None:
    if getattr(endpoint.args, "transcript_dir", None) is None:
        return None
    try:
        return endpoint.transcript_path()
    except Exception:
        logger.debug("Could not resolve endpoint transcript diagnostic path", exc_info=True)
        return None


def _exception_outcome(
    endpoint: EndpointState,
    prepared: PreparedExecution,
    exc: Exception,
) -> EndpointOutcome:
    logger.debug("Endpoint %s failed with exception", endpoint.name, exc_info=True)
    persist = not prepared.non_persisting_dry_run
    transcript_path = _transcript_diagnostic_path(endpoint) if persist else None
    diagnostic_path = write_exception_diagnostic(
        exc,
        endpoint_name=endpoint.name,
        invocation_id=endpoint._invocation_id,
        report_dir=endpoint.args.report_dir,
        transcript_path=transcript_path,
        persist=persist,
    )
    if removed_root(endpoint) is not None and (diagnostic_path is not None or not persist):
        # The final text names the removed root, so the traceback goes to the terminal.
        print_exception_diagnostic(exc)
    return EndpointOutcome(
        exit_code=EXIT_ERROR,
        report_text=exception_report_text(endpoint.name, exc, diagnostic_path),
    )


def _run_watched(
    endpoint: EndpointState, prepared: PreparedExecution, admission: object | None
) -> EndpointOutcome:
    """Run the invocation, stopping it when a run root is removed.

    ``finish_execution`` names the removed root, so the stopped run fails; it is
    not cancelled, unlike one the user interrupted.
    """
    watchdog = RunRootWatchdog(lambda: removed_root(endpoint))
    result = EndpointOutcome(exit_code=EXIT_ERROR)
    try:
        with watchdog:
            try:
                invocation = prepared.invocation
                result = invocation.run(admission if invocation.borrows_admission else None)
            except Exception as exc:  # noqa: BLE001 — normalize the endpoint plugin boundary
                result = _exception_outcome(endpoint, prepared, exc)
    except KeyboardInterrupt:
        watchdog.close()  # the interrupt may have cut the exit's join short
        if watchdog.removed is None:
            raise
    return result


def invoke_endpoint(
    endpoint: EndpointState,
    prepared: PreparedExecution,
    *,
    admission: object | None,
    started: float,
) -> EndpointOutcome:
    """Run the prepared invocation under the neutral coordinator."""
    endpoint._start_time = started
    witness = _StdoutWitness(sys.stdout)
    endpoint._stdout_witness = witness
    sys.stdout = witness  # type: ignore[assignment]
    try:
        with run_roots(*_live_roots(endpoint)):
            result = _run_watched(endpoint, prepared, admission)
    finally:
        sys.stdout = witness.wrapped
    _attach_memory_warning(result, admission)
    endpoint._finalize_result(result)
    return result


def _attach_memory_warning(result: EndpointOutcome, admission: object | None) -> None:
    """Put the Job's memory warning, then its work items' distinct ones, in the result."""
    # Local import: ``endpoint_admission`` imports this module at load.
    from booley.flows.endpoint_admission import AdmissionContext

    if result.refused or not isinstance(admission, AdmissionContext):
        return
    item_warnings = [] if admission.memory_sizing is None else admission.memory_sizing.warnings
    warnings = list(dict.fromkeys(filter(None, (admission.memory_warning, *item_warnings))))
    if not warnings:
        return
    text = "\n".join(warnings)
    result.detail["memory_admission_warning"] = text
    result.report_text = "\n".join(filter(None, (result.report_text, text)))


def finish_execution(
    endpoint: EndpointState,
    prepared: PreparedExecution,
    outcome: EndpointOutcome,
) -> ExecutionResult:
    """Publish completion: refresh the report, then the console report."""
    for cleanup_error in endpoint._requeue_cleanup_errors:
        normalize_completion_error(outcome, cleanup_error, "retire the killed attempt's progress")
    endpoint._requeue_cleanup_errors.clear()
    removed = removed_root(endpoint)
    if removed is not None:
        outcome.exit_code = EXIT_ERROR
        outcome.report_text = f"{endpoint.name} failed: {removed} was removed during the run"
    persist_report = not prepared.non_persisting_dry_run and not report_root_removed(endpoint)
    with run_roots(*_live_roots(endpoint)):
        exit_code = endpoint._finish_main(outcome, persist_report=persist_report)
    return ExecutionResult(exit_code=exit_code, outcome=outcome)

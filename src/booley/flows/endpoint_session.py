"""Preparation and invocation stages for the shared coordinator."""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from booley.flows.endpoint_events import (
    _endpoint_start_event,
    _write_display_event,
)
from booley.flows.endpoint_report_criteria import ReportCriteria, freeze, project
from booley.flows.endpoint_reporting import _StdoutWitness, report_discarded_evidence
from booley.runtime.endpoint_execution import (
    EXIT_ERROR,
    EndpointOutcome,
    ExecutionResult,
)
from booley.runtime.exception_diagnostics import (
    exception_report_text,
    write_exception_diagnostic,
)
from booley.runtime.sandbox_layout import canonical_project_alias_path

if TYPE_CHECKING:
    from booley.flows.endpoint_state import EndpointState


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PreparedExecution:
    """Adapter-owned state carried through the neutral execution sequence."""

    display_target: str | None
    display_label: str | None
    dry_run: bool
    non_persisting_dry_run: bool
    simulation: object | None = None


def _apply_pre_state_gate(endpoint: EndpointState) -> EndpointOutcome | None:
    """Render an early gate rejection without reading or writing ticket state."""
    result = endpoint._pre_state_gate()
    if result is None:
        return None
    endpoint._publish_console_report(result)
    return result


def _apply_default_endpoint_report_root(endpoint: EndpointState) -> EndpointOutcome | None:
    """Select durable Project data after adapters can supply a runtime root."""
    if (
        endpoint.endpoint_kind not in {"flow", "specialist"}
        or endpoint.args.report_dir is not None
    ):
        return None
    from booley.runtime.project_dir import resolve_checkout_project_dir

    try:
        project_data = resolve_checkout_project_dir(Path(endpoint.args.work_dir))
    except (OSError, RuntimeError, ValueError) as exc:
        result = EndpointOutcome(
            exit_code=EXIT_ERROR,
            report_text=f"Endpoint report directory could not be resolved: {exc}",
        )
        endpoint._publish_console_report(result)
        return result
    endpoint.args.report_dir = project_data / (
        "flow-reports" if endpoint.endpoint_kind == "flow" else "mcp-tool-reports"
    )
    return None


def _canonicalize_simulation_report_root(endpoint: EndpointState) -> EndpointOutcome | None:
    """Normalize the image's authenticated Project alias before side effects."""
    if endpoint.endpoint_kind != "flow" or endpoint.name != "sim":
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
    """Select the default root and canonicalize Simulation report inputs."""
    result = _apply_default_endpoint_report_root(endpoint)
    return result if result is not None else _canonicalize_simulation_report_root(endpoint)


def prepare_execution(
    endpoint: EndpointState,
) -> PreparedExecution | EndpointOutcome:
    """Adapt CLI arguments into one prepared execution request."""
    reset = getattr(endpoint, "reset_report_metadata", None)
    if callable(reset):
        reset()
    endpoint._report_criteria = ReportCriteria()
    endpoint.reset_evidence_discard()
    endpoint._stdout_witness = None
    if (early_outcome := endpoint._apply_pre_state_gate()) is not None:
        if endpoint.endpoint_kind in {"flow", "specialist"}:
            endpoint._report_criteria.project(early_outcome)
        return early_outcome
    if (early_outcome := _prepare_endpoint_report_root(endpoint)) is not None:
        if endpoint.endpoint_kind in {"flow", "specialist"}:
            endpoint._report_criteria.project(early_outcome)
        return early_outcome
    endpoint.read_state()
    endpoint._default_target_args()
    flow = getattr(endpoint, "flow", None)
    if endpoint.endpoint_kind == "flow":
        binding_error = endpoint._criterion_binding_gate()
        if binding_error is not None:
            endpoint._publish_console_report(binding_error)
            endpoint._report_criteria.project(binding_error)
            return binding_error
    if hasattr(endpoint, "prepare_target_endpoint"):
        target_error = endpoint.prepare_target_endpoint()
        if target_error is not None:
            endpoint.write_report(target_error)
            endpoint._publish_console_report(target_error)
            return target_error
    simulation: object | None = None
    if endpoint.name == "sim" and hasattr(flow, "prepare_simulation_endpoint"):
        simulation = flow.prepare_simulation_endpoint()
        if isinstance(simulation, EndpointOutcome):
            endpoint._publish_console_report(simulation)
            endpoint._report_criteria.project(simulation)
            return simulation
    if endpoint.endpoint_kind in {"flow", "specialist"}:
        freeze(endpoint, simulation)
    return _prepared_execution(endpoint, simulation)


def _prepared_execution(endpoint: EndpointState, simulation: object | None) -> PreparedExecution:
    """Publish the start event after all preparation gates have succeeded."""
    display_target = endpoint._resolve_display_config()
    display_label = endpoint._resolve_display_label()
    dry_run = bool(getattr(endpoint.args, "dry_run", False))
    non_persisting_dry_run = endpoint._is_non_persisting_dry_run()
    _write_display_event(
        _endpoint_start_event(
            endpoint.name,
            display_target,
            display_label=display_label,
            dry_run=dry_run,
            identity=endpoint._display_identity,
        )
    )
    return PreparedExecution(
        display_target=display_target,
        display_label=display_label,
        dry_run=dry_run,
        non_persisting_dry_run=non_persisting_dry_run,
        simulation=simulation,
    )


def _transcript_diagnostic_path(endpoint: EndpointState) -> Path | None:
    if getattr(endpoint.args, "transcript_dir", None) is None:
        return None
    resolver = getattr(endpoint, "_transcript_path", None)
    if not callable(resolver):
        return None
    try:
        return resolver()
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
    return EndpointOutcome(
        exit_code=EXIT_ERROR,
        report_text=exception_report_text(endpoint.name, exc, diagnostic_path),
    )


def invoke_endpoint(
    endpoint: EndpointState,
    prepared: PreparedExecution,
    *,
    admission: object | None,
    started: float,
) -> EndpointOutcome:
    """Run the legacy implementation hook under the neutral coordinator."""
    endpoint._start_time = started
    result = EndpointOutcome(exit_code=EXIT_ERROR)
    witness = _StdoutWitness(sys.stdout)
    endpoint._stdout_witness = witness
    sys.stdout = witness  # type: ignore[assignment]
    try:
        try:
            flow = getattr(endpoint, "flow", None)
            if prepared.simulation is not None and hasattr(flow, "run_prepared_simulation"):
                raw = flow.run_prepared_simulation(prepared.simulation, admission)
            else:
                raw = endpoint._run()
            result = endpoint._adapt_outcome(raw)
        except Exception as exc:  # noqa: BLE001 — normalize the endpoint plugin boundary
            result = _exception_outcome(endpoint, prepared, exc)
    finally:
        sys.stdout = witness.wrapped
    result = endpoint._adapt_outcome(result)
    endpoint._finalize_result(result)
    return result


def finish_execution(
    endpoint: EndpointState,
    prepared: PreparedExecution,
    outcome: EndpointOutcome,
    *,
    started: float | None,
    acceptance_recorded: bool,
) -> ExecutionResult:
    """Publish completion, persisting only after acceptance succeeds."""
    final_outcome = endpoint._adapt_outcome(outcome)
    report_discarded_evidence(endpoint, final_outcome)
    project(endpoint, final_outcome)
    exit_code = endpoint._finish_main(
        final_outcome,
        prepared.display_target,
        outcome.display_label or prepared.display_label,
        started=started,
        acceptance_recorded=acceptance_recorded,
        dry_run=prepared.dry_run,
        non_persisting_dry_run=prepared.non_persisting_dry_run,
    )
    return ExecutionResult(exit_code=exit_code, outcome=final_outcome)

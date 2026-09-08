"""Preparation and invocation stages for the shared coordinator."""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING

from booley.flows.endpoint_events import (
    _endpoint_start_event,
    _write_display_event,
)
from booley.flows.endpoint_reporting import _StdoutWitness
from booley.runtime.endpoint_execution import (
    EXIT_ERROR,
    EndpointOutcome,
)

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


def _apply_pre_state_gate(endpoint: EndpointState) -> EndpointOutcome | None:
    """Render an early gate rejection without reading or writing ticket state."""
    result = endpoint._pre_state_gate()
    if result is None:
        return None
    if result.report_text:
        print(result.report_text, file=sys.stderr, flush=True)
    return result


def prepare_execution(
    endpoint: EndpointState,
) -> PreparedExecution | EndpointOutcome:
    """Adapt CLI arguments into one prepared execution request."""
    if (early_outcome := endpoint._apply_pre_state_gate()) is not None:
        return early_outcome
    endpoint.read_state()
    endpoint._default_target_args()
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
        )
    )
    return PreparedExecution(
        display_target=display_target,
        display_label=display_label,
        dry_run=dry_run,
        non_persisting_dry_run=non_persisting_dry_run,
    )


def invoke_endpoint(
    endpoint: EndpointState,
    prepared: PreparedExecution,
    *,
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
            result = endpoint._adapt_outcome(endpoint._run())
        except Exception:
            logger.exception("Endpoint %s failed with exception", endpoint.name)
            result = EndpointOutcome(exit_code=EXIT_ERROR)
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
) -> int:
    """Publish completion, persisting only after acceptance succeeds."""
    return endpoint._finish_main(
        endpoint._adapt_outcome(outcome),
        prepared.display_target,
        prepared.display_label,
        started=started,
        acceptance_recorded=acceptance_recorded,
        dry_run=prepared.dry_run,
        non_persisting_dry_run=prepared.non_persisting_dry_run,
    )

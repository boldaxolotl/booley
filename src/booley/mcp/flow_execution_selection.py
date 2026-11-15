"""Select how one endpoint invocation records its evidence."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from booley.flows.execution_persistence import AcceptanceRecorder


def _unbound_acceptance_recorder() -> AcceptanceRecorder:
    # Construct on demand so importing MCP tools does not initialize the
    # Interactive ledger composition.
    from booley.mcp.unbound_recorder import UnboundAcceptanceRecorder

    return UnboundAcceptanceRecorder()


@dataclass(frozen=True, slots=True)
class FlowExecutionSelection:
    """Execution path for one endpoint invocation.

    ``acceptance_recorder`` builds the recorder a custom MCP tool or Specialist
    persists its Criteria through.
    """

    acceptance_recorder: Callable[[], AcceptanceRecorder]


_STANDALONE_SELECTION = FlowExecutionSelection(_unbound_acceptance_recorder)


def select_flow_execution() -> FlowExecutionSelection:
    """Every call records through the standalone (unbound) recorder."""
    return _STANDALONE_SELECTION

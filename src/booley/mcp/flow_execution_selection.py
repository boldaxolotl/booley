"""Select Goal-bound execution or Interactive evidence recording."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from booley.flows.execution_persistence import AcceptanceRecorder

if TYPE_CHECKING:
    from booley.goals.binding import GoalRunBinding


def _unbound_acceptance_recorder() -> AcceptanceRecorder:
    # Construct on demand alongside the Goal recorder.
    # Goal-bound calls do not need to initialize the Interactive ledger composition.
    from booley.mcp.unbound_recorder import UnboundAcceptanceRecorder

    return UnboundAcceptanceRecorder()


@dataclass(frozen=True, slots=True)
class FlowExecutionSelection:
    """Execution path for one endpoint invocation.

    ``flow_runner_module`` is the module launched for a Flow, or ``None`` to
    launch the endpoint's own module. ``acceptance_recorder`` builds the
    recorder a custom MCP tool or Specialist persists its Criteria through.
    """

    flow_runner_module: str | None
    acceptance_recorder: Callable[[], AcceptanceRecorder]


_STANDALONE_SELECTION = FlowExecutionSelection(None, _unbound_acceptance_recorder)


def select_flow_execution(binding: GoalRunBinding | None = None) -> FlowExecutionSelection:
    """A resolved binding selects the Goal adapter; other calls are unbound."""
    if binding is not None:
        return FlowExecutionSelection(
            "booley.mcp.goal_flow_runner", _goal_acceptance_recorder(binding)
        )
    return _STANDALONE_SELECTION


def _goal_acceptance_recorder(binding: GoalRunBinding) -> Callable[[], AcceptanceRecorder]:
    def build() -> AcceptanceRecorder:
        # Imported on construction so importing MCP tools never loads Goal Mode.
        from booley.goals.flow_execution import GoalFlowExecution
        from booley.mcp.goal_freshness import GOAL_FRESHNESS_RESOLVERS

        return GoalFlowExecution(binding, resolvers=GOAL_FRESHNESS_RESOLVERS)

    return build

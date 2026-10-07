"""Where the MCP server and ``McpTool`` choose the Flow runner and the recorder.

Before this module the server chose the Ticket Flow runner from
``BOOLEY_TICKET_FILE`` and ``McpTool`` built its Ticket recorder on its own.
:func:`select_flow_execution` now makes both of those choices, so a later
execution path is added here rather than at each of those two call sites.
Other modules still read ``BOOLEY_TICKET_FILE`` for their own purposes.

Today the choices equal the code they replaced: Flows launch through the
Ticket Board runner only when a Ticket file is configured, and custom MCP
tools and Specialists always record through ``TicketAcceptanceRecorder``,
which itself does nothing outside a Ticket.

Handed a :class:`~booley.goals.binding.GoalRunBinding`, the selection instead
records through :class:`~booley.goals.flow_execution.GoalFlowExecution`, the
same adapter for custom tools, Specialists, and built-in Flows. The MCP
server passes the binding resolved at admission; built-in and Project Flows
launch through ``mcp.goal_flow_runner``.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from booley.flows.execution_persistence import AcceptanceRecorder

if TYPE_CHECKING:
    from booley.goals.binding import GoalRunBinding

TICKET_FLOW_RUNNER = "booley.ticket_board.flow_runner"


def _ticket_acceptance_recorder() -> AcceptanceRecorder:
    # Imported on construction so importing MCP tools never loads Ticket Board.
    from booley.ticket_board.flow_execution import TicketAcceptanceRecorder

    return TicketAcceptanceRecorder()


@dataclass(frozen=True, slots=True)
class FlowExecutionSelection:
    """Execution path for one endpoint invocation.

    ``flow_runner_module`` is the module launched for a Flow, or ``None`` to
    launch the endpoint's own module. ``acceptance_recorder`` builds the
    recorder a custom MCP tool or Specialist persists its Criteria through.
    """

    flow_runner_module: str | None
    acceptance_recorder: Callable[[], AcceptanceRecorder]


_TICKET_SELECTION = FlowExecutionSelection(TICKET_FLOW_RUNNER, _ticket_acceptance_recorder)
_STANDALONE_SELECTION = FlowExecutionSelection(None, _ticket_acceptance_recorder)


def configured_ticket_file() -> Path | None:
    """This process's ``BOOLEY_TICKET_FILE``, or ``None`` when unset or empty."""
    value = os.environ.get("BOOLEY_TICKET_FILE")
    return Path(value) if value else None


def select_flow_execution(
    ticket_file: Path | None, binding: GoalRunBinding | None = None
) -> FlowExecutionSelection:
    """Select the execution path for a call whose configured Ticket file is *ticket_file*.

    A Goal run *binding* selects the Goal adapter, whatever *ticket_file* says.
    """
    if binding is not None:
        return FlowExecutionSelection(
            "booley.mcp.goal_flow_runner", _goal_acceptance_recorder(binding)
        )
    return _TICKET_SELECTION if ticket_file is not None else _STANDALONE_SELECTION


def _goal_acceptance_recorder(binding: GoalRunBinding) -> Callable[[], AcceptanceRecorder]:
    def build() -> AcceptanceRecorder:
        # Imported on construction so importing MCP tools never loads Goal Mode.
        from booley.goals.flow_execution import GoalFlowExecution
        from booley.mcp.goal_freshness import GOAL_FRESHNESS_RESOLVERS

        return GoalFlowExecution(binding, resolvers=GOAL_FRESHNESS_RESOLVERS)

    return build

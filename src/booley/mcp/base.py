"""MCP tool base class shared by Booley Flows and Specialists.

Exit code contract:
  0 — success (criterion met)
  1 — failure (criterion not met, but endpoint ran correctly)
  2 — error (endpoint itself failed, infrastructure problem)

Every MCP tool:
  - Parses CLI args via argparse
  - Loads/saves DevelopmentState
  - Writes a structured JSON report to the stage log directory
  - Classifies post-run git diffs as RTL or TB
  - Invalidates dependent criteria when code-modifying endpoints change files
"""

from __future__ import annotations

from booley.flows.endpoint_context import EndpointContext
from booley.runtime.endpoint_execution import (
    EXIT_ERROR,
    EXIT_FAILURE,
    EXIT_SUCCESS,
    EndpointOutcome,
)

from .call_context import binding_from_environment
from .flow_execution_selection import select_flow_execution

__all__ = ["EXIT_ERROR", "EXIT_FAILURE", "EXIT_SUCCESS", "McpTool", "McpToolResult"]

# Source-compatible result name for Project-local endpoint extensions.
McpToolResult = EndpointOutcome


class McpTool(EndpointContext):
    """Legacy MCP endpoint extension; execution services are transport independent."""

    def __init__(self) -> None:
        super().__init__()
        selection = select_flow_execution(binding=binding_from_environment())
        self._acceptance_recorder = selection.acceptance_recorder()

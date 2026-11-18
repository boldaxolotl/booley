"""MCP tool base class shared by Booley Flows and Specialists.

Exit code contract:
  0 — success (criterion met)
  1 — failure (criterion not met, but endpoint ran correctly)
  2 — error (endpoint itself failed, infrastructure problem)

Every MCP tool:
  - Parses CLI args via argparse
  - Writes a structured JSON report to the stage log directory
"""

from __future__ import annotations

from booley.flows.endpoint_context import EndpointContext
from booley.runtime.endpoint_execution import (
    EXIT_ERROR,
    EXIT_FAILURE,
    EXIT_SUCCESS,
    EndpointOutcome,
)

__all__ = ["EXIT_ERROR", "EXIT_FAILURE", "EXIT_SUCCESS", "McpTool", "McpToolResult"]

# Source-compatible result name for Project-local endpoint extensions.
McpToolResult = EndpointOutcome


class McpTool(EndpointContext):
    """Legacy MCP endpoint extension; execution services are transport independent."""

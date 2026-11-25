"""Bootstrap mode — outer harness vs. nested MCP-server context.

The harness runs Booley endpoints in two contexts:

* TOP_LEVEL: the outer harness Python process invokes a Flow directly
  (CLI: ``python -m booley.flows.sim``). Owns outer-harness durable-record
  hygiene: ghost Job record cleanup, handle pruning and Session registry writes.
* NESTED: the endpoint runs underneath an MCP server (either the
  developer's MCP server or a specialist sub-agent's nested MCP
  server). Outer-process bookkeeping is unsafe here — a parent's live Jobs
  would be misread as orphaned, and PID liveness across host/container
  PID namespaces is unreliable.

The harness signals NESTED by injecting ``BOOLEY_MCP_NESTED=1`` into
the MCP server's environment (see ``harness.mcp_config``). Anything
spawned underneath the MCP server inherits the flag.

Use ``should_run_outer_bookkeeping()`` at the bookkeeping sites instead of scattering inline env-var checks.
"""

from __future__ import annotations

import enum
import os

_NESTED_ENV = "BOOLEY_MCP_NESTED"


class BootstrapMode(enum.Enum):
    """Where this Booley Python process is running."""

    TOP_LEVEL = "top_level"
    NESTED = "nested"

    @classmethod
    def detect(cls) -> BootstrapMode:
        """Probe the environment for the active mode."""
        if os.environ.get(_NESTED_ENV) == "1":
            return cls.NESTED
        return cls.TOP_LEVEL

    @property
    def is_top_level(self) -> bool:
        return self is BootstrapMode.TOP_LEVEL


def should_run_outer_bookkeeping() -> bool:
    """True iff this process owns outer-harness durable-record hygiene.

    Admission itself lives in the shared slot store (ADR 0028) and never
    consults bootstrap mode. This remains the chokepoint for:

    * ``mcp.handle_pruning.prune_at_start`` and
      ``mcp.job_lifecycle.reconcile_orphaned`` (ghost record cleanup)
    * Session registry writes and maintenance (``mcp.server._McpLifetime``)

    It must short-circuit in NESTED mode — see the module docstring.
    """
    return BootstrapMode.detect().is_top_level

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

import logging
from dataclasses import dataclass

from booley.flows.endpoint_cli import _report_dir_arg  # noqa: F401 — compatibility export
from booley.flows.endpoint_context import EndpointContext
from booley.flows.endpoint_diff import _classify_files  # noqa: F401 — compatibility export
from booley.flows.endpoint_events import (  # noqa: F401 — compatibility exports
    _emit_criteria_update,
    _endpoint_end_event,
    _endpoint_progress_event,
    _endpoint_start_event,
    _write_display_event,
)
from booley.flows.endpoint_reporting import _StdoutWitness  # noqa: F401 — compatibility export
from booley.runtime.endpoint_execution import (  # noqa: F401 — compatibility exports
    EXIT_ERROR,
    EXIT_FAILURE,
    EXIT_SUCCESS,
    EndpointOutcome,
)

from .diff_classify import (
    _RTL_DIRS,  # noqa: F401 — re-exported so tests can patch booley.dev_support.base._RTL_DIRS
    _TB_DIRS,  # noqa: F401 — re-exported so tests can patch booley.dev_support.base._TB_DIRS
    read_source_dirs_from_toml,  # noqa: F401 — public API re-export; base itself never calls it
)
from .events import (
    _specialist_thinking_event,  # noqa: F401 — re-exported for specialist.py
)
from .run_lock import (
    _as_pid,  # noqa: F401 — re-exported for booley.dev_support.base importers/tests
    _scan_endpoint_events,  # noqa: F401 — re-exported for booley.dev_support.base importers/tests
)

logger = logging.getLogger(__name__)


@dataclass
class McpToolResult(EndpointOutcome):
    """Source-compatible result name for Project-local endpoint extensions."""


def _as_mcp_tool_result(outcome: EndpointOutcome) -> McpToolResult:
    """Promote a neutral outcome before invoking compatibility hooks."""
    if isinstance(outcome, McpToolResult):
        return outcome
    return McpToolResult(
        exit_code=outcome.exit_code,
        criterion_key=outcome.criterion_key,
        criterion_met=outcome.criterion_met,
        detail=outcome.detail,
        report_text=outcome.report_text,
        input_tokens=outcome.input_tokens,
        output_tokens=outcome.output_tokens,
        cached_tokens=outcome.cached_tokens,
        cache_create_tokens=outcome.cache_create_tokens,
        cost_usd=outcome.cost_usd,
        lines_added=outcome.lines_added,
        lines_removed=outcome.lines_removed,
        display_lines=outcome.display_lines,
        summary=outcome.summary,
    )


class McpTool(EndpointContext):
    """Legacy MCP endpoint extension; execution services are transport independent."""

    def _adapt_outcome(self, outcome: EndpointOutcome) -> McpToolResult:
        return _as_mcp_tool_result(outcome)


# Historical imports used by Project-local endpoints.

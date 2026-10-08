"""The developer switch that exposes Goal Mode before it replaces Ticket Mode (ADR 0067 D13).

Until the release that removes Ticket Mode, exactly one surface is reachable:
the Goal MCP tools, ``booley goal``, and Goalset seeding at ``init`` exist only
when the server or CLI environment sets ``BOOLEY_GOAL_MODE_PREVIEW=1``. The
switch is undocumented on purpose; it disappears with the surface swap.
"""

from __future__ import annotations

from booley.config.goals import GOAL_MODE_PREVIEW_ENV, goal_mode_preview_enabled

__all__ = ["GOAL_MODE_PREVIEW_ENV", "goal_mode_preview_enabled"]

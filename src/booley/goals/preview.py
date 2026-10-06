"""The developer switch that exposes Goal Mode before it replaces Ticket Mode (ADR 0067 D13).

Until the release that removes Ticket Mode, exactly one surface is reachable:
the Goal MCP tools, ``booley goal``, and Goalset seeding at ``init`` exist only
when the server or CLI environment sets ``BOOLEY_GOAL_MODE_PREVIEW=1``. The
switch is undocumented on purpose; it disappears with the surface swap.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

GOAL_MODE_PREVIEW_ENV = "BOOLEY_GOAL_MODE_PREVIEW"


def goal_mode_preview_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """Whether the Goal Mode surface is registered: the switch is exactly ``1``.

    Any other value, including ``true`` or an empty string, leaves it off, so a
    stray variable never exposes a second surface.
    """
    source = os.environ if environ is None else environ
    return source.get(GOAL_MODE_PREVIEW_ENV, "").strip() == "1"

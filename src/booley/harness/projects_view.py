"""Terminal view of the ``booley projects forget`` leftovers.

The table lives in the harness, not in :mod:`booley.projects`, because the shared
table renderer and the colour palette are harness modules and the source
dependency contract (rule D33) forbids ``booley.projects`` from importing the
harness. The harness injects :func:`render_leftovers` into
``booley.projects.cli.run`` as its ``leftovers_view``.
"""

from __future__ import annotations

from collections.abc import Sequence

from booley.projects.image_keepers import Leftover

from . import colors
from .terminal_table import render_table


def render_leftovers(leftovers: Sequence[Leftover]) -> str:
    """Render Docker resources a forgotten Project left behind as an aligned table.

    No column is capped: image IDs and tags stay whole so they can be copied.
    """
    rows = [
        [item.kind, colors.accent(item.name), item.size or "", item.reason, ", ".join(item.tags)]
        for item in leftovers
    ]
    return "\n".join(render_table(["Kind", "Name", "Size", "Reason", "Tags"], rows))

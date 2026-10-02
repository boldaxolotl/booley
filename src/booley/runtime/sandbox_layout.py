"""Container-side paths fixed by Session Issuance.

Leaf module with no Booley imports, so policy code (for example the
Simulation Flow's compiler cache) can name Sandbox locations without
importing the devcontainer renderer.
"""

from __future__ import annotations

# ``.booley_project`` mounts here; BOOLEY_PROJECT_DIR (containerEnv) points at
# it so in-container tooling — including the Ticket-Mode Runner and its
# developer agent (ADR 0028) — resolves project config from one place.
PROJECT_DIR_TARGET = "/booley-project"

WORK_DIR = "/work"

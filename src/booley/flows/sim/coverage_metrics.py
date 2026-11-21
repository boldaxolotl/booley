"""The metrics a Coverage Record scores, kept import-free so the CLI parser can list them."""

from __future__ import annotations

SCORED_METRICS = ("line", "branch", "expression", "toggle", "cover_property")

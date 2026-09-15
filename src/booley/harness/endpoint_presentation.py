"""Shared human-facing endpoint names across Harness renderers."""

from __future__ import annotations

_DISPLAY_NAMES = {"bwave": "B-Wave"}


def endpoint_display_name(endpoint_name: str) -> str:
    """Return the canonical human-facing name for an endpoint identifier."""
    return _DISPLAY_NAMES.get(endpoint_name, endpoint_name)

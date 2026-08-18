"""EDA-tool-independent synthesis PPA profile vocabulary.

Profiles express user intent, not backend commands.  Each synthesis backend
translates these names into its own mapping and physical-design controls.
"""

from __future__ import annotations

from typing import Any

from booley.core.boundary import BoundaryError

PPA_PROFILE_CHOICES = ("compact", "balanced", "max_frequency")
DEFAULT_PPA_PROFILE = "balanced"


def validate_ppa_profile(value: Any, *, field: str = "ppa_profile") -> str:
    """Return a supported profile name or raise at the config boundary."""
    if not isinstance(value, str) or value not in PPA_PROFILE_CHOICES:
        choices = ", ".join(PPA_PROFILE_CHOICES)
        raise BoundaryError(f"{field} must be one of {choices}; got {value!r}")
    return value

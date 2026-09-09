"""Validated concurrency settings from ``booley.toml [jobs]``."""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_CLASS_HEAVY = "heavy"
_CLASS_LIGHT = "light"
_CLASS_TICKET = "ticket"


@dataclass(frozen=True, slots=True)
class SlotCaps:
    """Per-class concurrency caps and the global queue bound."""

    max_heavy: int = 1
    max_light: int = 3
    max_tickets: int = 2
    queue_max: int = 8

    def cap_for(self, job_class: str) -> int:
        """Return the validated cap for a Runtime job class."""
        caps = {
            _CLASS_HEAVY: self.max_heavy,
            _CLASS_LIGHT: self.max_light,
            _CLASS_TICKET: self.max_tickets,
        }
        if job_class not in caps:
            raise ValueError(f"Unknown job class: {job_class!r}")
        cap = caps[job_class]
        if cap < 1:
            logger.warning("Cap for %s is %r; clamping to 1", job_class, cap)
            return 1
        return cap


def parse_caps(data: dict) -> SlotCaps:
    """Parse ``[jobs]`` caps, warning and defaulting invalid values."""
    section = data.get("jobs", {})
    defaults = SlotCaps()
    if not isinstance(section, dict):
        logger.warning("[jobs] is not a table; using defaults")
        return defaults
    values = {
        "max_heavy": defaults.max_heavy,
        "max_light": defaults.max_light,
        "max_tickets": defaults.max_tickets,
        "queue_max": defaults.queue_max,
    }
    non_cap_keys = {"heavy_memory"}
    for key, default in tuple(values.items()):
        if key not in section:
            continue
        value = section[key]
        floor = 0 if key == "queue_max" else 1
        if isinstance(value, int) and not isinstance(value, bool) and value >= floor:
            values[key] = value
        else:
            logger.warning(
                "[jobs] %s = %r is invalid (int >= %d); using %d",
                key,
                value,
                floor,
                default,
            )
    for key in section:
        if key not in values and key not in non_cap_keys:
            logger.warning("[jobs] has unknown key %r (ignored)", key)
    return SlotCaps(**values)

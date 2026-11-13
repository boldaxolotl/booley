"""Validated concurrency settings from ``booley.toml [jobs]``."""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from booley.config.eda import SUPPORTED_EDA_KINDS

logger = logging.getLogger(__name__)

_CLASS_HEAVY = "heavy"
_CLASS_LIGHT = "light"

#: The commercial EDA tools Booley provisions: the ones that take a licence seat.
#: Open-source tools have no Seat Cap.
LICENSED_EDA_TOOLS = SUPPORTED_EDA_KINDS


class SeatCapError(ValueError):
    """``[jobs.seats]`` is malformed, or a licensed tool a Job uses has no Seat Cap."""


def seat_cap_fix(tool: str) -> str:
    """The ``booley.toml`` entry that gives *tool* a Seat Cap (the Flow and Doctor both say it)."""
    return f"[jobs.seats] {tool} = <seats you own>"


@dataclass(frozen=True, slots=True)
class SlotCaps:
    """Per-class concurrency caps and the global queue bound."""

    max_heavy: int = 1
    max_light: int = 3
    queue_max: int = 8

    def cap_for(self, job_class: str) -> int:
        """Return the validated cap for a Runtime job class."""
        caps = {
            _CLASS_HEAVY: self.max_heavy,
            _CLASS_LIGHT: self.max_light,
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
        "queue_max": defaults.queue_max,
    }
    non_cap_keys = {"heavy_memory", "max_tickets", "memory_budget", "seats"}
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


def parse_seat_caps(data: Mapping[str, Any]) -> dict[str, int]:
    """Parse ``[jobs.seats]``: how many licence seats the user owns, per licensed tool.

    Absent reads as no Seat Caps. Raises :class:`SeatCapError` naming the key
    for a table that is not a table, a key that is not a licensed tool, or a
    count that is not a whole number of at least one. A non-table ``[jobs]`` has
    no seats here: :func:`parse_caps` already warns about it.
    """
    section = data.get("jobs")
    if not isinstance(section, Mapping) or "seats" not in section:
        return {}
    table = section["seats"]
    if not isinstance(table, Mapping):
        raise SeatCapError(
            f"[jobs].seats must be a table such as {seat_cap_fix('vivado')}, got {table!r}"
        )
    caps: dict[str, int] = {}
    for tool, count in table.items():
        if tool not in LICENSED_EDA_TOOLS:
            raise SeatCapError(
                f"[jobs.seats].{tool}: not a licensed EDA tool; "
                f"Seat Caps apply to: {', '.join(sorted(LICENSED_EDA_TOOLS))}"
            )
        if not isinstance(count, int) or isinstance(count, bool) or count < 1:
            raise SeatCapError(
                f"[jobs.seats].{tool} = {count!r} must be a positive whole number of seats"
            )
        caps[tool] = count
    return caps


def required_seats(tools: Iterable[str], data: Mapping[str, Any]) -> tuple[tuple[str, int], ...]:
    """The ``(tool, Seat Cap)`` pairs a Job that runs *tools* must hold a seat of.

    A Job that uses no licensed tool needs nothing and never reads the table, so
    a malformed ``[jobs.seats]`` refuses only Jobs that would use it (the schema
    audit reports the table itself). Raises :class:`SeatCapError` for the first
    tool without a Seat Cap: only the user knows how many licence seats they own.
    """
    used = tuple(tools)
    if not used:
        return ()
    caps = parse_seat_caps(data)
    missing = next((tool for tool in used if tool not in caps), None)
    if missing is not None:
        raise SeatCapError(
            f"{missing} has no Seat Cap; add {seat_cap_fix(missing)} to booley.toml "
            "(only you know how many licence seats you own)"
        )
    return tuple((tool, caps[tool]) for tool in used)

"""Validated concurrency settings from ``booley.toml [jobs]``."""

from __future__ import annotations

import logging
import re
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from booley.config.eda import SUPPORTED_EDA_KINDS
from booley.core.booley_dir import resolve_common_booley_dir
from booley.core.config_paths import resolve_toml

logger = logging.getLogger(__name__)

_CLASS_LIGHT = "light"

#: ``[jobs]`` keys that memory admission replaced; ``booley.toml`` may no longer set them.
RETIRED_JOBS_KEYS = ("max_heavy", "heavy_memory")

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

    max_light: int = 3
    queue_max: int = 8

    def cap_for(self, job_class: str) -> int:
        """Return the validated cap for a Runtime job class."""
        if job_class != _CLASS_LIGHT:
            raise ValueError(f"Unknown job class: {job_class!r}")
        cap = self.max_light
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
        "max_light": defaults.max_light,
        "queue_max": defaults.queue_max,
    }
    non_cap_keys = {*RETIRED_JOBS_KEYS, "max_tickets", "memory_budget", "seats"}
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


class RetiredJobsKeyError(ValueError):
    """``booley.toml`` still sets a ``[jobs]`` key that memory admission replaced."""


@dataclass(frozen=True, slots=True)
class RetiredKey:
    """A retired ``[jobs]`` key and the 1-based line it sits on."""

    key: str
    line: int


_TABLE_HEADER = re.compile(r"^\s*\[\[?\s*([^\[\]]*?)\s*\]\]?\s*(?:#.*)?$")
_JOBS_INLINE = re.compile(r"^\s*[\"']?jobs[\"']?\s*=")


def _normalized_name(raw: str) -> str:
    """A TOML table or key name without quotes or spaces around its dots."""
    return re.sub(r"\s*\.\s*", ".", raw.replace('"', "").replace("'", "")).strip()


def _opens_multiline_string(line: str) -> str | None:
    """The delimiter of a multi-line string this line opens and leaves open, if any."""
    _, _, value = line.partition("=")
    for delimiter in ('"""', "'''"):
        if delimiter in value and value.count(delimiter) % 2 == 1:
            return delimiter
    return None


def _retired_key_lines(text: str) -> dict[str, int]:
    """Line of each retired key's assignment, plus ``""`` for the ``jobs = {...}`` line.

    A table-aware scan: only ``[jobs]`` keys and dotted ``jobs.<key>`` keys count,
    and the text of a multi-line string never does.
    """
    lines: dict[str, int] = {}
    table = ""
    delimiter: str | None = None
    for number, line in enumerate(text.splitlines(), start=1):
        if delimiter is not None:
            if delimiter in line:
                delimiter = None
            continue
        header = _TABLE_HEADER.match(line)
        if header is not None and "=" not in line:
            table = _normalized_name(header.group(1))
            continue
        name = _normalized_name(line.partition("=")[0])
        if _JOBS_INLINE.match(line):
            lines.setdefault("", number)
        for key in RETIRED_JOBS_KEYS:
            if name in (key, f"jobs.{key}") and (table == "jobs") == (name == key):
                lines.setdefault(key, number)
        delimiter = _opens_multiline_string(line)
    return lines


def retired_jobs_keys(path: Path) -> tuple[RetiredKey, ...]:
    """The retired ``[jobs]`` keys the ``booley.toml`` at *path* sets, with their lines.

    A missing or unparseable file reports none: its own error surfaces where the
    file is loaded. An inline ``jobs = {...}`` table reports the line of ``jobs``.
    """
    try:
        text = path.read_text(encoding="utf-8")
        data = tomllib.loads(text)
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return ()
    section = data.get("jobs")
    if not isinstance(section, dict):
        return ()
    lines = _retired_key_lines(text)
    return tuple(
        RetiredKey(key, lines.get(key, lines.get("", 1)))
        for key in RETIRED_JOBS_KEYS
        if key in section
    )


def retired_jobs_keys_fix(found: RetiredKey) -> str:
    """The one-line repair for a retired key (the message, Doctor and ``booley-heal`` say it)."""
    return f"delete [jobs].{found.key} (line {found.line}); memory admission replaced it"


def retired_jobs_keys_message(path: Path, found: Iterable[RetiredKey]) -> str:
    """The refusal text naming each retired key and its line, the edit, and ``booley-heal``."""
    named = ", ".join(f"{item.key} (line {item.line})" for item in found)
    return (
        f"booley.toml sets retired [jobs] keys: {named}. "
        f"Fix: delete these lines from {path}; memory admission replaced them "
        "(or run booley-heal to remove them)."
    )


def refuse_retired_jobs_keys(project_root: Path | None) -> None:
    """Raise :class:`RetiredJobsKeyError` while the live Project configuration sets a retired key.

    Reads only the Main Booley Directory's ``booley.toml``, the file admission
    reads: a linked worktree's or a measured commit's copy is never read, so
    exported checkouts stay measurable. A Project that cannot be resolved has
    nothing to refuse.
    """
    try:
        path = resolve_toml(resolve_common_booley_dir(project_root))
    except (FileNotFoundError, ValueError, RuntimeError):
        return
    found = retired_jobs_keys(path)
    if found:
        raise RetiredJobsKeyError(retired_jobs_keys_message(path, found))

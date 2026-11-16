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
from booley.config.retired import TABLE_HEADER, normalized_name, opens_multiline_string

logger = logging.getLogger(__name__)

_MEMORY_ADMISSION = "memory admission replaced it"

#: Retired ``[jobs]`` keys and why; ``booley.toml`` may no longer set them.
RETIRED_JOBS_KEYS: dict[str, str] = {
    "max_heavy": _MEMORY_ADMISSION,
    "heavy_memory": _MEMORY_ADMISSION,
    "max_light": "Specialists and the Mutation Flow are admitted by memory",
    "queue_max": "the Job queue has no limit: a Job waits until it runs or is cancelled",
    "memory_budget": (
        "the Memory Budget is computed from [sandbox].memory: set [sandbox].memory instead"
    ),
}

#: The commercial EDA tools Booley provisions: the ones that take a licence seat.
#: Open-source tools have no Seat Cap.
LICENSED_EDA_TOOLS = SUPPORTED_EDA_KINDS


class SeatCapError(ValueError):
    """``[jobs.seats]`` is malformed, or a licensed tool a Job uses has no Seat Cap."""


def seat_cap_fix(tool: str) -> str:
    """The ``booley.toml`` entry that gives *tool* a Seat Cap (the Flow and Doctor both say it)."""
    return f"[jobs.seats] {tool} = <seats you own>"


def parse_seat_caps(data: Mapping[str, Any]) -> dict[str, int]:
    """Parse ``[jobs.seats]``: how many licence seats the user owns, per licensed tool.

    Absent reads as no Seat Caps. Raises :class:`SeatCapError` naming the key
    for a table that is not a table, a key that is not a licensed tool, or a
    count that is not a whole number of at least one. A non-table ``[jobs]`` has
    no seats here.
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


@dataclass(frozen=True, slots=True)
class RetiredKey:
    """A retired ``[jobs]`` key and the 1-based line it sits on."""

    key: str
    line: int


_JOBS_INLINE = re.compile(r"^\s*[\"']?jobs[\"']?\s*=")


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
        header = TABLE_HEADER.match(line)
        if header is not None:
            table = normalized_name(header.group(1))
            continue
        name = normalized_name(line.partition("=")[0])
        if _JOBS_INLINE.match(line):
            lines.setdefault("", number)
        for key in RETIRED_JOBS_KEYS:
            if (name == key and table == "jobs") or (name == f"jobs.{key}" and table == ""):
                lines.setdefault(key, number)
        delimiter = opens_multiline_string(line)
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
    return f"delete [jobs].{found.key} (line {found.line}); {RETIRED_JOBS_KEYS[found.key]}"


def retired_jobs_keys_message(path: Path, found: Iterable[RetiredKey]) -> str:
    """The refusal text naming each retired key and its line, the edit, and ``booley-heal``."""
    named = ", ".join(
        f"{item.key} (line {item.line}: {RETIRED_JOBS_KEYS[item.key]})" for item in found
    )
    return (
        f"booley.toml sets retired [jobs] keys: {named}. "
        f"Fix: delete these lines from {path} (or run booley-heal to remove them)."
    )

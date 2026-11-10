"""Validated ``[worktree]`` configuration: the idle age ``booley worktree gc`` uses."""

import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from booley.core.boundary import require_dict, require_str_value

DEFAULT_GC_OLDER_THAN = "7d"

_AGE = re.compile(r"(?:([0-9]+)d)?(?:([0-9]+)h)?(?:([0-9]+)m)?(?:([0-9]+)s)?")
_AGE_UNIT_SECONDS = (86400, 3600, 60, 1)
# A longer text is no age; refusing it early keeps a huge digit run out of ``int()``.
_AGE_MAX_CHARS = 32


@dataclass(frozen=True)
class GcAge:
    """An idle age: its length in seconds and the text the user wrote, for messages."""

    seconds: int
    text: str

    @classmethod
    def of(cls, text: str) -> "GcAge":
        """Parse *text* with :func:`parse_age`."""
        return cls(parse_age(text), text)


def parse_age(text: str) -> int:
    """Return the seconds in an age such as ``7d``, ``36h`` or ``1d12h``.

    Descending ``<N>d``, ``<N>h``, ``<N>m``, ``<N>s`` components with a positive
    total; a bare number is refused because it names no unit.
    """
    match = _AGE.fullmatch(text) if len(text) <= _AGE_MAX_CHARS else None
    seconds = 0
    if match is not None:
        seconds = sum(
            int(count) * unit
            for count, unit in zip(match.groups(), _AGE_UNIT_SECONDS, strict=True)
            if count is not None
        )
    if seconds <= 0:
        raise ValueError(
            f"invalid age {text[:_AGE_MAX_CHARS]!r}: use positive d, h, m, s components "
            'in that order, such as "7d", "36h" or "1d12h"'
        )
    return seconds


def parse_gc_older_than(data: Mapping[str, Any]) -> GcAge:
    """Validate ``[worktree].gc_older_than`` without filesystem access."""
    section = require_dict(data.get("worktree", {}), field="[worktree]")
    text = require_str_value(
        section.get("gc_older_than", DEFAULT_GC_OLDER_THAN), field="[worktree].gc_older_than"
    )
    try:
        return GcAge.of(text)
    except ValueError as exc:
        raise ValueError(f"[worktree].gc_older_than: {exc}") from exc


def gc_older_than(booley_dir: Path) -> GcAge:
    """Read ``[worktree].gc_older_than`` from *booley_dir*'s ``booley.toml`` (default 7 days)."""
    path = booley_dir / "booley.toml"
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        return parse_gc_older_than(data)
    except ValueError as exc:
        raise ValueError(f"booley.toml {exc}") from exc

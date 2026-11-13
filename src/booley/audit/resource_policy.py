"""Memory Budget parsing and formatting for environment audits."""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path

from booley.core.boundary import as_dict, as_str

GIB_BYTES = 1024**3
MEMORY_HEADROOM_BYTES = 2 * GIB_BYTES

CGROUP_MEMORY_LIMIT_PATHS = (
    Path("/sys/fs/cgroup/memory.max"),
    Path("/sys/fs/cgroup/memory/memory.limit_in_bytes"),
)
_CGROUP_UNLIMITED_FLOOR = 1 << 60
_MEMORY_SUFFIXES = {"b": 1, "k": 1024, "m": 1024**2, "g": GIB_BYTES}


def format_memory(byte_count: int | float) -> str:
    """Format a byte count in GiB the way Booley configuration spells it."""
    gib = byte_count / GIB_BYTES
    if gib == int(gib):
        return f"{int(gib)}g"
    return f"{gib:.1f}g"


def parse_memory_limit(text: str) -> int | None:
    """Parse a Docker-style memory string such as ``6g`` or ``512m``."""
    match = re.fullmatch(r"(\d+)\s*([bkmg]?)", text.strip().lower())
    if not match:
        return None
    return int(match.group(1)) * _MEMORY_SUFFIXES[match.group(2) or "b"]


def cgroup_memory_limit_bytes() -> int | None:
    """Return the effective cgroup limit, or ``None`` when it is unlimited."""
    for path in CGROUP_MEMORY_LIMIT_PATHS:
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if text == "max":
            return None
        try:
            value = int(text)
        except ValueError:
            continue
        return None if value >= _CGROUP_UNLIMITED_FLOOR else value
    return None


def configured_sandbox_memory(config: Mapping[str, object]) -> str:
    """Return the configured sandbox limit, or an empty string when absent."""
    section = as_dict(config.get("sandbox"), default={}) or {}
    memory = section.get("memory", "")
    memory_table = as_dict(memory)
    if memory_table is not None:
        memory = memory_table.get("default", "")
    return (as_str(memory, "") or "").strip()


def configured_memory_budget(config: Mapping[str, object]) -> int | None:
    """Return the ``[jobs].memory_budget`` override in bytes, or ``None`` when unset.

    Only a positive size string such as ``"24g"`` is accepted. The schema audit
    and the runtime budget share this parser so Doctor and admission reject the
    same values.
    """
    section = as_dict(config.get("jobs"))
    if section is None or "memory_budget" not in section:
        return None
    value = section["memory_budget"]
    parsed = parse_memory_limit(value) if isinstance(value, str) else None
    if parsed is None or parsed <= 0:
        raise ValueError(f'[jobs].memory_budget = {value!r} must be a positive size such as "24g"')
    return parsed

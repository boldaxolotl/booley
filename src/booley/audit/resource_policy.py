"""Memory Budget parsing and formatting for environment audits."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from booley.core.boundary import as_dict, as_str

GIB_BYTES = 1024**3
# The default swap beyond memory, and the headroom a Sandbox without swap holds
# back: a quarter of memory.
SWAP_SHARE_DIVISOR = 4

CGROUP_MEMORY_LIMIT_PATHS = (
    Path("/sys/fs/cgroup/memory.max"),
    Path("/sys/fs/cgroup/memory/memory.limit_in_bytes"),
)
CGROUP_SWAP_LIMIT_PATHS = (
    Path("/sys/fs/cgroup/memory.swap.max"),
    Path("/sys/fs/cgroup/memory/memory.memsw.limit_in_bytes"),
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


def cgroup_swap_limit_bytes(memory_limit_bytes: int) -> int | None:
    """Return the swap the cgroup allows beyond its memory limit.

    cgroup v2 states the swap itself; cgroup v1 states memory plus swap, so the
    memory limit comes off. An unlimited value returns the unbounded floor (the
    caller caps it at the host's swap). ``None`` means neither file is readable,
    as on a Docker host without swap accounting.
    """
    v2_path, v1_path = CGROUP_SWAP_LIMIT_PATHS
    for path in (v2_path, v1_path):
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if text == "max":
            return _CGROUP_UNLIMITED_FLOOR
        try:
            value = int(text)
        except ValueError:
            continue
        if value >= _CGROUP_UNLIMITED_FLOOR:
            return _CGROUP_UNLIMITED_FLOOR
        return max(0, value - memory_limit_bytes) if path == v1_path else value
    return None


def configured_sandbox_memory(config: Mapping[str, object]) -> str:
    """Return the configured sandbox limit, or an empty string when absent."""
    section = as_dict(config.get("sandbox"), default={}) or {}
    memory = section.get("memory", "")
    memory_table = as_dict(memory)
    if memory_table is not None:
        memory = memory_table.get("default", "")
    return (as_str(memory, "") or "").strip()


def configured_sandbox_swap(config: Mapping[str, object]) -> str:
    """Return the configured ``[sandbox].swap``, or an empty string when absent."""
    section = as_dict(config.get("sandbox"), default={}) or {}
    return (as_str(section.get("swap", ""), "") or "").strip()


@dataclass(frozen=True)
class HostMemory:
    """The memory and swap of the Docker host, in bytes."""

    memory_bytes: int
    swap_bytes: int


def _meminfo_bytes(text: str, key: str) -> int:
    match = re.search(rf"^{key}:\s*(\d+)\s*kB\s*$", text, re.MULTILINE)
    if match is None:
        raise ValueError(f"no {key} line in the meminfo text")
    return int(match.group(1)) * 1024


def parse_meminfo(text: str) -> HostMemory:
    """Parse ``/proc/meminfo`` text into :class:`HostMemory`.

    Raises:
        ValueError: ``MemTotal`` or ``SwapTotal`` is missing.
    """
    return HostMemory(_meminfo_bytes(text, "MemTotal"), _meminfo_bytes(text, "SwapTotal"))


def _format_exact_size(byte_count: int) -> str:
    """Render a byte count in the largest unit that holds it exactly."""
    for suffix, factor in (("g", GIB_BYTES), ("m", 1024**2), ("k", 1024)):
        if byte_count % factor == 0:
            return f"{byte_count // factor}{suffix}"
    return str(byte_count)


def sandbox_swap_bytes(memory: str, swap: str, host_swap_bytes: int) -> int | None:
    """Return the swap a Sandbox may use beyond *memory*, or ``None`` with no memory limit.

    A parseable *swap* is used as written (``"0"`` means no swap). An empty or
    unparseable *swap* defaults to a quarter of *memory*, capped at the swap the
    Docker host has, so a typo never escapes the cap.
    """
    memory_bytes = parse_memory_limit(memory)
    if memory_bytes is None:
        return None
    swap_limit = parse_memory_limit(swap)
    if swap_limit is not None:
        return swap_limit
    return min(memory_bytes // SWAP_SHARE_DIVISOR, host_swap_bytes)


def memory_swap_limit(memory: str, swap: str, host_swap_bytes: int) -> str:
    """Return the Docker ``--memory-swap`` value (memory plus swap), or ``""`` with no memory."""
    swap_bytes = sandbox_swap_bytes(memory, swap, host_swap_bytes)
    memory_bytes = parse_memory_limit(memory)
    if swap_bytes is None or memory_bytes is None:
        return ""
    return _format_exact_size(memory_bytes + swap_bytes)

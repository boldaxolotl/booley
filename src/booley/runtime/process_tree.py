"""Small, side-effect-free helpers for inspecting Unix process trees."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path


def _parse_ppid(stat: str) -> int | None:
    """Return the parent PID from one ``/proc/<pid>/stat`` line."""
    try:
        return int(stat.rsplit(")", 1)[1].split()[1])
    except (ValueError, IndexError):
        return None


def _ppid_of(pid: int) -> int | None:
    """Return *pid*'s parent PID, or ``None`` when it cannot be read."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except (OSError, ValueError):
        return None
    return _parse_ppid(stat)


def has_ancestor(
    pid: int,
    ancestor_pid: int,
    *,
    read_ppid: Callable[[int], int | None] = _ppid_of,
    max_hops: int = 1024,
) -> bool:
    """Return whether *ancestor_pid* is proven in *pid*'s bounded ancestry."""
    if pid <= 0 or ancestor_pid <= 0 or max_hops <= 0:
        return False
    if pid == ancestor_pid:
        return True
    current = pid
    seen = {current}
    for _hop in range(max_hops):
        if current <= 1:
            return False
        parent = read_ppid(current)
        if parent is None or parent <= 1 or parent in seen:
            return False
        if parent == ancestor_pid:
            return True
        seen.add(parent)
        current = parent
    return False


def descendant_pids(root: int) -> list[int]:
    """Return *root*'s descendants deepest-first from the Unix ``/proc`` tree."""
    children: dict[int, list[int]] = {}
    try:
        entries = [int(path.name) for path in Path("/proc").iterdir() if path.name.isdigit()]
    except (OSError, ValueError):
        return []
    for pid in entries:
        ppid = _ppid_of(pid)
        if ppid is not None:
            children.setdefault(ppid, []).append(pid)

    ordered: list[int] = []
    frontier = [root]
    while frontier:
        pid = frontier.pop()
        for child in children.get(pid, []):
            ordered.append(child)
            frontier.append(child)
    ordered.reverse()
    return ordered

"""Small, side-effect-free helpers for inspecting Unix process trees."""

from __future__ import annotations

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

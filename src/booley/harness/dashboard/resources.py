"""Observational cgroup-v2 resource sampling with explicit unavailable values."""

from __future__ import annotations

import contextlib
import os
import shutil
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Resources:
    """CPU percentage is relative to total Sandbox CPU capacity."""

    cpu_percent: float | None = None
    memory: int | None = None
    memory_limit: int | None = None
    disk_free: int | None = None


class ResourceSampler:
    """Delta CPU sample; no sleeps, subprocesses or mutations."""

    def __init__(self, cgroup: Path = Path("/sys/fs/cgroup")) -> None:
        self.cgroup = cgroup
        self._previous: tuple[float, int] | None = None

    def sample(self, root: Path, *, now: float) -> Resources:
        """Read the current Sandbox cgroup; failures leave individual measurements absent."""
        cpu = memory = limit = disk = None
        with contextlib.suppress(OSError):
            disk = shutil.disk_usage(root).free
        try:
            memory = int((self.cgroup / "memory.current").read_text())
            raw = (self.cgroup / "memory.max").read_text().strip()
            limit = None if raw == "max" else int(raw)
        except (OSError, ValueError):
            pass
        try:
            capacity = _cpu_capacity(self.cgroup)
            usage = dict(
                line.split() for line in (self.cgroup / "cpu.stat").read_text().splitlines()
            )
            consumed = int(usage["usage_usec"])
            if self._previous is not None and now > self._previous[0] and capacity > 0:
                cpu = max(
                    0,
                    (consumed - self._previous[1])
                    / 1e6
                    / (now - self._previous[0])
                    / capacity
                    * 100,
                )
            self._previous = now, consumed
        except (OSError, ValueError, KeyError, ZeroDivisionError):
            pass
        return Resources(cpu, memory, limit, disk)


def _cpu_capacity(cgroup: Path) -> float:
    quota, period = (cgroup / "cpu.max").read_text().split()
    capacity = float(os.cpu_count() or 1) if quota == "max" else int(quota) / int(period)
    try:
        cpus = (cgroup / "cpuset.cpus.effective").read_text().strip().split(",")
        count = 0
        for value in cpus:
            first, _, last = value.partition("-")
            count += int(last or first) - int(first) + 1
        return min(capacity, count) if count > 0 else capacity
    except (OSError, ValueError):
        return capacity

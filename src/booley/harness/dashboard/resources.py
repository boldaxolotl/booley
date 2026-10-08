"""Observational cgroup-v2 resource sampling with explicit unavailable values."""

from __future__ import annotations

import contextlib
import os
import shutil
from dataclasses import dataclass
from itertools import islice
from pathlib import Path

from booley.runtime.pid import ProcessIdentity, capture_process_identity


@dataclass(frozen=True)
class Resources:
    """CPU percentage is relative to total Sandbox CPU capacity."""

    cpu_percent: float | None = None
    memory: int | None = None
    memory_limit: int | None = None
    disk_free: int | None = None
    cpu_capacity: float | None = None


class ResourceSampler:
    """Delta CPU sample; no sleeps, subprocesses or mutations."""

    def __init__(self, cgroup: Path = Path("/sys/fs/cgroup")) -> None:
        self.cgroup = cgroup
        self._previous: tuple[float, int] | None = None
        self.capacity: float | None = None

    def sample(self, root: Path, *, now: float) -> Resources:
        """Read the current Sandbox cgroup; failures leave individual measurements absent."""
        cpu = memory = limit = disk = None
        self.capacity = None
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
            self.capacity = capacity
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
        return Resources(cpu, memory, limit, disk, self.capacity)


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


@dataclass(frozen=True)
class ProcessResources:
    """Revalidated tree measurements; CPU percent uses one core as 100%."""

    cpu_percent: float | None = None
    memory: int | None = None
    peak_memory: int | None = None


def _process_tree(identity: ProcessIdentity, proc_root: Path) -> frozenset[ProcessIdentity]:
    pending, found = [identity.pid], {}
    for _ in range(4096):
        if not pending:
            return frozenset(found.values())
        pid = pending.pop()
        if pid in found:
            continue
        current = capture_process_identity(pid, proc_root=proc_root)
        if current is None or (pid == identity.pid and current != identity):
            raise ValueError("process tree identity changed")
        found[pid] = current
        tasks = list(islice((proc_root / str(pid) / "task").iterdir(), 4097))
        if not tasks or len(tasks) > 4096:
            raise ValueError("process tree tasks unavailable or over bound")
        for task in tasks:
            children = (task / "children").read_text().split()
            if len(children) > 4096:
                raise ValueError("process children over bound")
            pending.extend(int(child) for child in children)
            if len(pending) > 4096:
                raise ValueError("process tree over bound")
    raise ValueError("process tree over bound")


def _process_sample(identity: ProcessIdentity, proc_root: Path) -> tuple[int, int]:
    directory = proc_root / str(identity.pid)
    fields = (directory / "stat").read_text().rsplit(")", 1)[1].split()
    ticks = int(fields[11]) + int(fields[12])
    status = dict(
        line.split(":", 1)
        for line in (directory / "status").read_text().splitlines()
        if ":" in line
    )
    return ticks, int(status["VmRSS"].split()[0]) * 1024


class ProcessSampler:
    """Bounded, revalidated Job process-tree deltas; incomplete trees are unavailable."""

    def __init__(self, proc_root: Path = Path("/proc")) -> None:
        self.proc_root = proc_root
        self._previous: dict[
            ProcessIdentity, tuple[float, frozenset[ProcessIdentity], int, int]
        ] = {}

    def sample(
        self, identity: ProcessIdentity, *, now: float, capacity: float | None = None
    ) -> ProcessResources:
        """Include descendants; legacy capacity argument does not change per-core units."""
        del capacity
        try:
            tree = _process_tree(identity, self.proc_root)
            samples = [_process_sample(member, self.proc_root) for member in tree]
            ticks, memory = sum(s[0] for s in samples), sum(s[1] for s in samples)
            if _process_tree(identity, self.proc_root) != tree:
                return ProcessResources()
            previous = self._previous.get(identity)
            cpu, peak = None, memory
            if previous:
                peak = max(memory, previous[3])
                if now > previous[0] and previous[1] == tree:
                    cpu = max(
                        0,
                        (ticks - previous[2])
                        / os.sysconf("SC_CLK_TCK")
                        / (now - previous[0])
                        * 100,
                    )
            if len(self._previous) >= 4096:
                self._previous.clear()
            self._previous[identity] = now, tree, ticks, peak
            return ProcessResources(cpu, memory, peak)
        except (OSError, ValueError, KeyError, IndexError):
            return ProcessResources()

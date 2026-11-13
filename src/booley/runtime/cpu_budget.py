"""CPUs available to this process: its affinity, bounded by the cgroup CPU quota."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

from booley.core.boundary import as_int

_CPU_MAX = Path("/sys/fs/cgroup/cpu.max")
_CPU_V1_LAYOUTS = (
    (
        Path("/sys/fs/cgroup/cpu/cpu.cfs_quota_us"),
        Path("/sys/fs/cgroup/cpu/cpu.cfs_period_us"),
    ),
    (
        Path("/sys/fs/cgroup/cpu,cpuacct/cpu.cfs_quota_us"),
        Path("/sys/fs/cgroup/cpu,cpuacct/cpu.cfs_period_us"),
    ),
)


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None


def _affinity_cpu_count() -> int:
    try:
        return len(os.sched_getaffinity(0))
    except AttributeError:
        return os.cpu_count() or 1
    except OSError:
        return os.cpu_count() or 1


def _cgroup_cpu_count(read_text: Callable[[Path], str | None]) -> int | None:
    cpu_max = read_text(_CPU_MAX)
    if cpu_max:
        fields = cpu_max.split()
        if len(fields) == 2 and fields[0] != "max":
            return _quota_jobs(fields[0], fields[1])
        if fields and fields[0] == "max":
            return None
    for quota_path, period_path in _CPU_V1_LAYOUTS:
        quota = read_text(quota_path)
        period = read_text(period_path)
        if quota is None or period is None:
            continue
        quota_value = as_int(quota)
        if quota_value is None:
            continue
        if quota_value < 0:
            return None
        jobs = _quota_jobs(quota, period)
        if jobs is not None:
            return jobs
    return None


def _quota_jobs(quota: str, period: str) -> int | None:
    quota_value = as_int(quota)
    period_value = as_int(period)
    if quota_value is None or period_value is None:
        return None
    if quota_value < 0 or period_value <= 0:
        return None
    return max(1, quota_value // period_value)


def available_cpu_count(
    affinity_cpu_count: Callable[[], int] | None = None,
    read_text: Callable[[Path], str | None] | None = None,
) -> int:
    """The CPUs this process may use: its affinity, bounded by the cgroup CPU quota."""
    affinity = max(1, (affinity_cpu_count or _affinity_cpu_count)())
    quota = _cgroup_cpu_count(read_text or _read_text)
    return min(affinity, quota) if quota is not None else affinity

"""Resource-bounded GNU Make policy for Verilator simulation-model builds."""

from __future__ import annotations

import os
import shlex
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from booley.audit.resource_policy import GIB_BYTES, heavy_memory_reservation
from booley.config.jobs import load_job_budget_config, parse_caps
from booley.core.boundary import as_dict, as_int
from booley.targets.domain import TargetInspection

LaneKind = Literal["heavy", "unreserved"]
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
_MEMORY_LIMITS = (
    Path("/sys/fs/cgroup/memory.max"),
    Path("/sys/fs/cgroup/memory/memory.limit_in_bytes"),
)
_CGROUP_UNLIMITED_FLOOR = 1 << 60


@dataclass(frozen=True, slots=True)
class VerilatorBuildBudget:
    """Effective resources assigned to one Verilator model compilation."""

    effective_cpu_count: int
    memory_bytes: int
    heavy_lane_count: int
    make_jobs: int


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


def _cgroup_memory_limit(read_text: Callable[[Path], str | None]) -> int | None:
    for path in _MEMORY_LIMITS:
        value = read_text(path)
        if value is None:
            continue
        if value == "max":
            return None
        parsed = as_int(value)
        if parsed is None:
            continue
        return None if parsed >= _CGROUP_UNLIMITED_FLOOR else parsed
    return None


def load_verilator_build_budget(
    lane_kind: LaneKind = "heavy",
    *,
    affinity_cpu_count: Callable[[], int] | None = None,
    read_text: Callable[[Path], str | None] | None = None,
    config_loader: Callable[[], dict] | None = None,
) -> VerilatorBuildBudget:
    """Compute the deterministic Make budget for one admitted compilation lane."""
    affinity_cpu_count = affinity_cpu_count or _affinity_cpu_count
    read_text = read_text or _read_text
    config_loader = config_loader or load_job_budget_config
    config = config_loader()
    caps = parse_caps(config)
    affinity = max(1, affinity_cpu_count())
    quota = _cgroup_cpu_count(read_text)
    effective_cpu = min(affinity, quota) if quota is not None else affinity
    jobs_section = as_dict(config.get("jobs"), default={}) or {}
    reservation = heavy_memory_reservation(
        jobs_section.get("heavy_memory"), calibration=None, selected_targets=()
    )
    memory_bytes = reservation.bytes
    memory_limit = _cgroup_memory_limit(read_text)
    if memory_limit is not None:
        memory_bytes = min(memory_bytes, memory_limit // caps.max_heavy)
    if lane_kind == "unreserved":
        jobs = 1
    else:
        cpu_jobs = max(1, effective_cpu // caps.max_heavy)
        memory_jobs = max(1, memory_bytes // GIB_BYTES)
        jobs = min(cpu_jobs, memory_jobs)
    return VerilatorBuildBudget(
        effective_cpu_count=effective_cpu,
        memory_bytes=memory_bytes,
        heavy_lane_count=caps.max_heavy,
        make_jobs=max(1, jobs),
    )


def _make_options(inspection: TargetInspection) -> tuple[object, ...]:
    modern = inspection.flow_options.get("make_options")
    legacy = as_dict(inspection.tool_options, default={}) or {}
    values: list[object] = []
    for authored in (modern, legacy.get("make_options")):
        if isinstance(authored, Sequence) and not isinstance(authored, (str, bytes)):
            values.extend(authored)
    return tuple(values)


def _has_jobs_option(options: Sequence[object]) -> bool:
    for option in options:
        try:
            tokens = shlex.split(str(option))
        except ValueError:
            tokens = [str(option)]
        for token in tokens:
            if token in {"-j", "--jobs"} or token.startswith("--jobs="):
                return True
            if token.startswith("-") and not token.startswith("--"):
                cluster = token[1:]
                for index, character in enumerate(cluster):
                    if character != "j":
                        continue
                    suffix = cluster[index + 1 :]
                    if not suffix or suffix.isdigit():
                        return True
    return False


def verilator_backend_arguments(
    inspection: TargetInspection,
    *,
    lane_kind: LaneKind = "heavy",
) -> tuple[str, ...]:
    """Return only Booley-owned backend options for one Verilator setup."""
    if inspection.eda_tool != "verilator" or _has_jobs_option(_make_options(inspection)):
        return ()
    budget = load_verilator_build_budget(lane_kind)
    return (f"-j{budget.make_jobs}", "VM_PARALLEL_BUILDS=1")

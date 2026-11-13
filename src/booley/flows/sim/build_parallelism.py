"""Resource-bounded GNU Make policy for Verilator simulation-model builds."""

from __future__ import annotations

import logging
import re
import shlex
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from booley.audit.resource_policy import GIB_BYTES, heavy_memory_reservation
from booley.config.jobs import parse_caps
from booley.core.boundary import as_dict, as_int
from booley.flows.memory_admission import job_thread_count, memory_admission_enabled
from booley.runtime.booley_dir import resolve_common_booley_dir
from booley.runtime.cpu_budget import _affinity_cpu_count, _read_text, available_cpu_count
from booley.runtime.shared_infra import load_job_budget_config
from booley.targets.domain import TargetInspection

logger = logging.getLogger(__name__)

LaneKind = Literal["heavy", "unreserved"]
_MEMORY_LIMITS = (
    Path("/sys/fs/cgroup/memory.max"),
    Path("/sys/fs/cgroup/memory/memory.limit_in_bytes"),
)
_CGROUP_UNLIMITED_FLOOR = 1 << 60
_SHORT_OPTION_CLUSTER_WITH_JOBS = re.compile(r"-[bBdehikLnprRstvwqS]*j\d*")


@dataclass(frozen=True, slots=True)
class VerilatorBuildBudget:
    """Effective resources assigned to one Verilator model compilation."""

    effective_cpu_count: int
    memory_bytes: int
    heavy_lane_count: int
    make_jobs: int


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


def _load_common_job_budget(project_root: Path | None) -> dict:
    """The Job budget settings of the Main Booley Directory, as admission reads them.

    Never the checkout's own ``booley.toml``: a checkout run or a linked worktree
    shares one machine budget, and its commit must not resize it. Bare
    invocations without a resolvable Project get the defaults.
    """
    try:
        common = resolve_common_booley_dir(project_root)
    except FileNotFoundError:
        return {}
    except (ValueError, RuntimeError) as exc:
        # Admission tolerates the same unusable selection; the build keeps defaults.
        logger.warning("Failed to resolve the Main Booley Directory for the build budget: %s", exc)
        return {}
    return load_job_budget_config(booley_dir=common)


def load_verilator_build_budget(
    lane_kind: LaneKind = "heavy",
    *,
    project_root: Path | None = None,
    affinity_cpu_count: Callable[[], int] | None = None,
    read_text: Callable[[Path], str | None] | None = None,
    config_loader: Callable[[], dict] | None = None,
) -> VerilatorBuildBudget:
    """Compute the deterministic Make budget for one admitted compilation lane."""
    if lane_kind == "unreserved":
        return VerilatorBuildBudget(
            effective_cpu_count=1,
            memory_bytes=0,
            heavy_lane_count=1,
            make_jobs=1,
        )
    affinity_cpu_count = affinity_cpu_count or _affinity_cpu_count
    read_text = read_text or _read_text
    config = (
        config_loader() if config_loader is not None else _load_common_job_budget(project_root)
    )
    caps = parse_caps(config)
    effective_cpu = available_cpu_count(affinity_cpu_count, read_text)
    jobs_section = as_dict(config.get("jobs"), default={}) or {}
    reservation = heavy_memory_reservation(
        jobs_section.get("heavy_memory"), calibration=None, selected_targets=()
    )
    memory_bytes = reservation.bytes
    memory_limit = _cgroup_memory_limit(read_text)
    if memory_limit is not None:
        memory_bytes = min(memory_bytes, memory_limit // caps.max_heavy)
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
            if _SHORT_OPTION_CLUSTER_WITH_JOBS.fullmatch(token):
                return True
    return False


def verilator_backend_arguments(
    inspection: TargetInspection,
    *,
    lane_kind: LaneKind = "heavy",
    project_root: Path | None = None,
) -> tuple[str, ...]:
    """Return only Booley-owned backend options for one Verilator setup."""
    if inspection.eda_tool != "verilator" or _has_jobs_option(_make_options(inspection)):
        return ()
    if lane_kind == "heavy" and memory_admission_enabled():
        # Under memory admission the Job, not a heavy lane, shares the cores: its
        # count was fixed at admission. No count means no admission root, so no
        # other Job is tracked: use every available core.
        threads = job_thread_count()
        make_jobs = (
            available_cpu_count(_affinity_cpu_count, _read_text) if threads is None else threads
        )
    else:
        make_jobs = load_verilator_build_budget(lane_kind, project_root=project_root).make_jobs
    return (f"-j{make_jobs}", "VM_PARALLEL_BUILDS=1")

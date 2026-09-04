"""Simulation wrapper telemetry shared by execution and compatibility code."""

from __future__ import annotations

import re

from booley.flows.base import SubprocessResult

_BUILD_MILLISECONDS_RE = re.compile(r"^BOOLEY_BUILD_MILLISECONDS: (\d+)$", re.MULTILINE)
_BUILD_SECONDS_RE = re.compile(r"^BOOLEY_BUILD_SECONDS: (\d+)$", re.MULTILINE)
_RUN_STAGE_RE = re.compile(
    r"^BOOLEY_RUN_STAGE token=[0-9a-f]+ rc=-?\d+ duration_ms=(\d+)$",
    re.MULTILINE,
)
_SIM_CPU_RE = re.compile(
    r"^BOOLEY_SIM_CPU_SECONDS: user=(\d+(?:\.\d+)?) system=(\d+(?:\.\d+)?)$",
    re.MULTILINE,
)


def parse_build_seconds(output: str) -> float:
    """Extract build wall time, preferring milliseconds over the legacy marker."""
    milliseconds = _BUILD_MILLISECONDS_RE.search(output)
    if milliseconds:
        return int(milliseconds.group(1)) / 1000
    seconds = _BUILD_SECONDS_RE.search(output)
    return float(seconds.group(1)) if seconds else 0.0


def parse_run_seconds(output: str) -> float | None:
    """Extract the wrapper's high-resolution run-half duration, when present."""
    matches = _RUN_STAGE_RE.findall(output)
    return int(matches[-1]) / 1000 if matches else None


def parse_sim_cpu_seconds(output: str) -> tuple[float, float] | None:
    """Extract simulator-child user and system CPU seconds, when supported."""
    matches = _SIM_CPU_RE.findall(output)
    if not matches:
        return None
    user_s, system_s = matches[-1]
    return float(user_s), float(system_s)


def process_resources(
    output: str,
    process: SubprocessResult,
) -> dict[str, float | int | None]:
    """Normalize process-tree and simulator-child resource observations."""
    resources: dict[str, float | int | None] = {
        "command_peak_rss_mb": process.peak_rss_mb,
        "command_oom_kill_delta": process.oom_kill_delta,
    }
    cpu = parse_sim_cpu_seconds(output)
    if cpu is not None:
        resources["simulation_user_cpu_s"] = round(cpu[0], 6)
        resources["simulation_system_cpu_s"] = round(cpu[1], 6)
    return resources


__all__ = [
    "parse_build_seconds",
    "parse_run_seconds",
    "parse_sim_cpu_seconds",
    "process_resources",
]

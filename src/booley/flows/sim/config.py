"""Public project-configuration interface shared by simulation consumers."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from booley.targets.flow_names import config_section

DEFAULT_MAX_RUNDIR_BYTES = 5 * 1024**3
DEFAULT_SIM_TIMEOUT_MS = 600_000


def _sim_config(work_dir: Path | str | None) -> Mapping[str, Any]:
    """Return the configured Simulation Flow section, or an empty mapping."""
    try:
        from booley.runtime.shared_infra import _load_rtl_config

        config = _load_rtl_config(work_dir)
    except ImportError:
        return {}
    if not config:
        return {}
    return config_section(config.get("flows", {}), "sim")


def resolve_run_cwd(work_dir: Path | str | None = None) -> str:
    """Return the configured simulation cwd relative to the Project root.

    ``.`` is explicit when unset so every run-half uses the Project root rather
    than falling back to its simulator build directory.
    """
    value = _sim_config(work_dir).get("run_cwd")
    return str(value) if value else "."


def resolve_trace_args(work_dir: Path | str | None = None) -> list[str]:
    """Return project-owned arguments that enable its trace harness."""
    return [str(argument) for argument in (_sim_config(work_dir).get("trace_args") or [])]


def resolve_trace_files(work_dir: Path | str | None = None) -> list[str]:
    """Return declared trace artifact paths or globs in search order."""
    return [str(path) for path in (_sim_config(work_dir).get("trace_files") or [])]


def resolve_pre_run_commands(work_dir: Path | str | None = None) -> list[str]:
    """Return Project-owned shell lines run before each Simulation work unit."""
    return [str(command) for command in (_sim_config(work_dir).get("pre_run_commands") or [])]


def resolve_max_rundir_bytes(work_dir: Path | str | None = None) -> int:
    """Return the maximum growth allowed for one simulator run directory."""
    value = _sim_config(work_dir).get("max_rundir_bytes", DEFAULT_MAX_RUNDIR_BYTES)
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return DEFAULT_MAX_RUNDIR_BYTES


def resolve_sim_timeout_ms(work_dir: Path | str | None = None) -> int:
    """Return the Project-owned default simulator timeout in milliseconds."""
    value = _sim_config(work_dir).get("timeout_ms", DEFAULT_SIM_TIMEOUT_MS)
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return DEFAULT_SIM_TIMEOUT_MS


def resolve_sim_time_grace_s(work_dir: Path | str | None = None) -> float:
    """Return the frozen-simulator-clock watchdog grace period."""
    from booley.flows.sim.run_guard import DEFAULT_SIM_TIME_GRACE_S

    value = _sim_config(work_dir).get("sim_time_grace_s", DEFAULT_SIM_TIME_GRACE_S)
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return DEFAULT_SIM_TIME_GRACE_S


def resolve_cycle_sentinels(work_dir: Path | str | None = None) -> list[str]:
    """Return configured Cycle Count record prefixes."""
    return [str(value) for value in (_sim_config(work_dir).get("cycle_sentinels") or []) if value]

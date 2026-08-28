"""Public project-configuration interface shared by simulation consumers."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from booley.targets.flow_names import config_section


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

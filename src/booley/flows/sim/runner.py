"""Public selection and verdict configuration for simulation adapters."""

from __future__ import annotations

from pathlib import Path

from booley.targets.flow_names import config_section

SIM_RUN_HALVES: dict[str, str] = {
    "icarus": "booley.flows.sim.backends.icarus",
    "verilator": "booley.flows.sim.backends.verilator",
}


def resolve_sim_sentinels(work_dir: Path | None = None) -> tuple[list[str], list[str]]:
    """Return project-defined passing and failing simulator-output markers."""
    try:
        from booley.runtime.shared_infra import _load_rtl_config

        cfg = _load_rtl_config(work_dir)
        if cfg:
            sim = config_section(cfg.get("flows", {}), "sim")
            passes = [str(s) for s in (sim.get("pass_sentinels") or [])]
            fails = [str(s) for s in (sim.get("fail_sentinels") or [])]
            return passes, fails
    except ImportError:
        pass
    return [], []

"""Resolve whether a built-in Flow is enabled.

Execution location is invariant: all Flow subprocesses run in the Session
Runtime.
"""

from __future__ import annotations

from pathlib import Path

from booley.targets.flow_names import config_section


def flow_enabled(flow_name: str, work_dir: Path | None) -> bool:
    """Read ``[flows.<name>].enabled``."""
    cfg: dict = {}
    try:
        from booley.runtime.shared_infra import _load_rtl_config

        cfg = _load_rtl_config(work_dir) or {}
    except Exception:  # noqa: BLE001 — bare invocations keep working with defaults
        cfg = {}
    flows = cfg.get("flows", {}) if isinstance(cfg, dict) else {}
    if not isinstance(flows, dict):
        flows = {}
    section = config_section(flows, flow_name)
    return section.get("enabled", True) is not False

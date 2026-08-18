"""Resolve whether a built-in Flow is enabled.

Execution location is no longer configurable: all Flow subprocesses run in
the Session Runtime.  The legacy backend value is retained only long enough to
produce an actionable hard-migration error.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from booley.flow_names import config_section


@dataclass(frozen=True)
class ExecutionSelection:
    """One Flow's enablement and any retired backend spelling."""

    enabled: bool = True
    legacy_backend: str | None = None


def resolve_execution(flow_name: str, work_dir: Path | None) -> ExecutionSelection:
    """Read ``[flows.<name>].enabled`` and a surviving backend key."""
    cfg: dict = {}
    try:
        from booley.shared_infra import _load_rtl_config

        cfg = _load_rtl_config(work_dir) or {}
    except Exception:  # noqa: BLE001 — bare invocations keep working with defaults
        cfg = {}
    flows = cfg.get("flows", {}) if isinstance(cfg, dict) else {}
    if not isinstance(flows, dict):
        flows = {}
    section = config_section(flows, flow_name)
    raw_backend = section.get("backend")
    return ExecutionSelection(
        enabled=section.get("enabled", True) is not False,
        legacy_backend=None if raw_backend is None else str(raw_backend).strip(),
    )


def execution_error(flow_name: str, selection: ExecutionSelection) -> str | None:
    """Return the hard-migration error for a retired backend key, if any."""
    if selection.legacy_backend is None:
        return None
    raw = selection.legacy_backend
    if raw == "none":
        return (
            f'[flows.{flow_name}].backend = "none" is retired. Write instead:\n'
            f"  [flows.{flow_name}]\n  enabled = false"
        )
    return (
        f"[flows.{flow_name}].backend = {raw!r} is retired: all Flows run inside "
        "the Session Runtime. Delete the backend line."
    )

"""Resolve whether a built-in Flow is enabled.

Execution location is invariant: all Flow subprocesses run in the Session
Runtime.
"""

from __future__ import annotations

from pathlib import Path

from booley.config.flow_enablement import (
    FlowConfigError,
    flow_enabled_from_config,
)
from booley.config.flow_enablement import (
    flow_enabled as _flow_enabled,
)


def flow_enabled(flow_name: str, work_dir: Path | None) -> bool:
    """Read ``[flows.<name>].enabled``."""
    return _flow_enabled(flow_name, work_dir)


__all__ = ["FlowConfigError", "flow_enabled", "flow_enabled_from_config"]

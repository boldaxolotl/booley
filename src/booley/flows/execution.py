"""Resolve whether a built-in Flow is enabled.

Execution location is invariant: all Flow subprocesses run in the Session
Runtime.
"""

from __future__ import annotations

from booley.config.flow_enablement import (
    FlowConfigError,
    flow_enabled,
    flow_enabled_from_config,
)

__all__ = ["FlowConfigError", "flow_enabled", "flow_enabled_from_config"]

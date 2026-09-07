"""Shared invocation contract for Booley's built-in Flows.

Custom Flows continue to inherit :class:`booley.flows.base.BooleyFlow` and own
their argument surface.  This module is deliberately limited to the shipped
Flows so a framework upgrade cannot collide with a Custom Flow's flags.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from booley.core.boundary import BoundaryError, require_int
from booley.targets.flow_names import canonical, config_section


def positive_milliseconds(value: str) -> int:
    """Parse one strictly-positive integer millisecond value for argparse."""
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def resolve_timeout_ms(
    flow_name: str,
    work_dir: Path | None,
    requested: Any,
    default_ms: int,
) -> int:
    """Resolve a built-in Flow timeout using call > config > default precedence.

    CLI values have already crossed the argparse boundary, but callers at the
    MCP budgeting seam pass JSON values directly.  Validate both paths here so
    the child Flow and its outer watchdog cannot disagree.
    """
    field = "timeout_ms"
    if requested is not None:
        timeout_ms = require_int(requested, field=field)
    elif work_dir is None:
        timeout_ms = default_ms
    else:
        from booley.runtime.shared_infra import _load_rtl_config

        config = _load_rtl_config(work_dir) or {}
        flows = config.get("flows", {})
        section = config_section(flows, canonical(flow_name)) if isinstance(flows, dict) else {}
        if field not in section:
            timeout_ms = default_ms
        else:
            timeout_ms = require_int(
                section[field],
                field=f"[flows.{canonical(flow_name)}].{field}",
            )
    if timeout_ms <= 0:
        source = field if requested is not None else f"[flows.{canonical(flow_name)}].{field}"
        raise BoundaryError(f"{source} must be a positive integer, got {timeout_ms!r}")
    return timeout_ms


def requested_timeout_ms(arguments: dict[str, Any]) -> Any:
    """Return the canonical timeout call value, accepting one legacy spelling."""
    canonical_value = arguments.get("timeout_ms")
    legacy_value = arguments.get("timeout")
    if canonical_value is not None and legacy_value is not None:
        raise BoundaryError("timeout_ms conflicts with deprecated timeout")
    return canonical_value if canonical_value is not None else legacy_value


@dataclass(frozen=True)
class BudgetPlan:
    """Side-effect-free pre-spawn accounting for one Flow invocation."""

    timeout_ms: int
    work_units: int
    execution_s: int
    setup_grace_s: int
    finalize_grace_s: int
    minimum_s: int

    @property
    def outer_timeout_s(self) -> int:
        """Return the aggregate subprocess watchdog in whole seconds."""
        planned = self.execution_s + self.setup_grace_s + self.finalize_grace_s
        return max(self.minimum_s, planned)

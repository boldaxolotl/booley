"""Shared invocation contract for Booley's built-in Flows.

Custom Flows continue to inherit :class:`booley.flows.base.BooleyFlow` and own
their argument surface.  This module is deliberately limited to the shipped
Flows so a framework upgrade cannot collide with a Custom Flow's flags.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final

from booley.core.boundary import BoundaryError, require_int
from booley.targets.flow_names import canonical, config_section

_BUILTIN_TIMEOUT_DEFAULTS_MS: Final[Mapping[str, int]] = MappingProxyType(
    {
        "sim": 600_000,
        "lint": 120_000,
        "synth": 1_800_000,
        "fpga": 7_200_000,
    }
)


def default_timeout_ms(flow_name: str) -> int:
    """Return the canonical per-work-unit timeout for a shipped Flow."""
    name = canonical(flow_name)
    try:
        return _BUILTIN_TIMEOUT_DEFAULTS_MS[name]
    except KeyError as exc:
        raise BoundaryError(f"{flow_name!r} is not a built-in Flow") from exc


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
) -> int:
    """Resolve a built-in Flow timeout using call > config > default precedence.

    CLI values have already crossed the argparse boundary, but callers at the
    MCP budgeting seam pass JSON values directly.  Validate both paths here so
    the child Flow and its outer watchdog cannot disagree.  An omitted
    ``work_dir`` means the current workspace, matching the child parser.
    """
    field = "timeout_ms"
    if requested is not None:
        timeout_ms = require_int(requested, field=field)
    else:
        from booley.runtime.shared_infra import _load_rtl_config

        config = _load_rtl_config(work_dir or Path.cwd()) or {}
        flows = config.get("flows", {})
        section = config_section(flows, canonical(flow_name)) if isinstance(flows, dict) else {}
        if field not in section:
            timeout_ms = default_timeout_ms(flow_name)
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
    """Return the canonical MCP timeout, rejecting the CLI-only legacy alias."""
    if "timeout" in arguments:
        raise BoundaryError("timeout is a CLI-only deprecated alias; use timeout_ms")
    return arguments.get("timeout_ms")


@dataclass(frozen=True)
class BudgetPlan:
    """Side-effect-free pre-spawn accounting for one Flow invocation."""

    timeout_ms: int
    work_units: int
    setup_grace_per_unit_s: int
    finalize_grace_s: int
    execution_floor_s: int = 0
    outer_floor_s: int = 0

    @property
    def execution_s(self) -> int:
        """Return the aggregate active execution budget."""
        planned = max(1, self.timeout_ms // 1000) * self.work_units
        return max(self.execution_floor_s, planned)

    @property
    def setup_grace_s(self) -> int:
        """Return setup headroom scaled across all work units."""
        return self.setup_grace_per_unit_s * self.work_units

    @property
    def outer_timeout_s(self) -> int:
        """Return the aggregate subprocess watchdog in whole seconds."""
        planned = self.execution_s + self.setup_grace_s + self.finalize_grace_s
        return max(self.outer_floor_s, planned)

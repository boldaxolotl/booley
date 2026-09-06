"""Compatibility entry points for the canonical Target catalog.

New consumers should construct one :class:`TargetCatalog` at their boundary and
pass handles downstream. These functions preserve the historical call surface
while migration completes.
"""

from __future__ import annotations

from pathlib import Path

from booley.targets.catalog import TargetCatalog
from booley.targets.domain import (
    _HANDLE_FACTORY_KEY,
    TARGET_AWARE_FLOWS,
    TARGET_IDENTITY_PARAM,
    TARGET_SELECTOR_PARAM,
    TargetHandle,
    TargetInput,
    TargetInspection,
    TargetRef,
    criterion_matches_target,
    flow_can_drive,
    partition_target_inputs,
)


def select_target(
    project_root: Path | str,
    token: str,
    *,
    for_flow: str | None = None,
) -> TargetHandle:
    """Select one Target through a fresh catalog compatibility wrapper."""
    return TargetCatalog.build(project_root).select(token, for_flow=for_flow)


def select_targets(
    project_root: Path | str,
    target_arg: str | None,
    *,
    for_flow: str | None = None,
) -> tuple[TargetHandle, ...]:
    """Select an endpoint's comma-separated Targets through one catalog."""
    return TargetCatalog.build(project_root).select_many(target_arg, for_flow=for_flow)


def inspect_target(project_root: Path | str, handle: TargetHandle) -> TargetInspection:
    """Inspect a selected handle through its content-identified snapshot."""
    return TargetCatalog.build(project_root).inspect(handle)


def inspect_target_selector(
    project_root: Path | str,
    token: str,
    *,
    for_flow: str | None = None,
) -> TargetInspection:
    """Select and inspect one token through a single catalog."""
    catalog = TargetCatalog.build(project_root)
    return catalog.inspect(catalog.select(token, for_flow=for_flow))


__all__ = [
    "TARGET_AWARE_FLOWS",
    "TARGET_IDENTITY_PARAM",
    "TARGET_SELECTOR_PARAM",
    "_HANDLE_FACTORY_KEY",
    "TargetCatalog",
    "TargetHandle",
    "TargetInput",
    "TargetInspection",
    "TargetRef",
    "criterion_matches_target",
    "flow_can_drive",
    "inspect_target",
    "inspect_target_selector",
    "partition_target_inputs",
    "select_target",
    "select_targets",
]

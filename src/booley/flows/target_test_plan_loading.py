"""Build a Target's Test Run plan from the ``tests.toml`` of one checkout (ADR 0072).

The plan itself (:mod:`booley.flows.target_test_plan`) is pure domain policy;
this module is the one place that reads the file for it.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from pathlib import Path
from typing import Any, cast

from booley.config.project_config import load_test_configuration, lookup_target_section
from booley.flows.simulator_family import normalize_eda_tool
from booley.flows.target_test_plan import TargetTestPlan, build_target_test_plan
from booley.targets.catalog import TargetCatalog

__all__ = [
    "checkout_target_identity",
    "launches_cocotb",
    "load_checkout_target_test_plan",
    "load_target_test_plan",
    "plusarg_parameter_names",
    "simulation_adapter_kind",
]


def launches_cocotb(flow_options: Mapping[str, Any]) -> bool:
    """Whether a Target with these flow options is launched through Cocotb."""
    module = flow_options.get("cocotb_module")
    return isinstance(module, str) and bool(module)


def simulation_adapter_kind(eda_tool: str | None, flow_options: Mapping[str, Any]) -> str:
    """The integration that launches a Target: ``cocotb`` or its simulator family."""
    if launches_cocotb(flow_options):
        return "cocotb"
    try:
        return normalize_eda_tool(eda_tool)
    except ValueError:
        return str(eda_tool or "")


def plusarg_parameter_names(parameters: Mapping[str, Any]) -> tuple[str, ...]:
    """Names of the plusarg parameters the resolved Target declares."""
    return tuple(
        str(name)
        for name, spec in parameters.items()
        if isinstance(spec, Mapping)
        and cast("Mapping[str, Any]", spec).get("paramtype") == "plusarg"
    )


def load_target_test_plan(
    work_dir: Path | str,
    target: str,
    *,
    identity: str,
    adapter: str,
    target_plusargs: Collection[str] = (),
) -> TargetTestPlan:
    """Read ``tests.toml`` of the checkout at *work_dir* and build *target*'s plan.

    *identity* (``vlnv#name``) picks the Target's section; *target* is its display name.
    """
    sections = load_test_configuration(work_dir)
    section = lookup_target_section(sections, identity)
    return build_target_test_plan(
        target, section, adapter=adapter, target_plusargs=target_plusargs
    )


def checkout_target_identity(work_dir: Path | str, target: str) -> str:
    """The identity (``vlnv#name``) of the Target written *target* in the checkout at *work_dir*.

    Every way of writing one Target gives the same identity. Raises
    ``FuseSocError`` (``UnknownTargetError`` included) when the Target cannot
    be selected.
    """
    return TargetCatalog.build(work_dir).select(target, for_flow="sim").identity


def load_checkout_target_test_plan(work_dir: Path | str, target: str) -> TargetTestPlan:
    """*target*'s Test Run plan in the checkout at *work_dir*, from its ``.core`` and ``tests.toml``.

    For callers outside the Simulation Flow that need the plan the Flow would
    build (Goal Mode freezes a Goal's Test Runs with it). Raises
    ``FuseSocError`` (``UnknownTargetError`` included) when the Target cannot
    be selected or inspected, ``ValueError`` for a ``tests.toml`` the Target
    cannot apply, and ``OSError`` when it cannot be read.
    """
    catalog = TargetCatalog.build(work_dir)
    handle = catalog.select(target, for_flow="sim")
    inspection = catalog.inspect(handle)
    return load_target_test_plan(
        handle.project_root,
        handle.selector,
        identity=handle.identity,
        adapter=simulation_adapter_kind(inspection.eda_tool, inspection.flow_options),
        target_plusargs=plusarg_parameter_names(inspection.parameters),
    )

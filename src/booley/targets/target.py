"""Canonical selection and pre-setup inspection of FuseSoC Targets.

This module owns the boundary between durable Target identity, the selector
accepted by Flow entry points, and condition-selected declarations. Execution
authority begins later, when :func:`booley.fusesoc.fusesoc_registry.resolve_target`
produces EDAM after FuseSoC setup.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fusesoc.coremanager import CoreManager, DependencyError
from fusesoc.librarymanager import Library, LibraryManager
from fusesoc.vlnv import Vlnv

from booley.fusesoc import fusesoc_registry
from booley.fusesoc.fusesoc_registry import TargetRef
from booley.targets import target_naming

TARGET_AWARE_FLOWS: tuple[str, ...] = ("synth", "fpga", "lint", "sim")

_SIM_EDA_TOOLS = frozenset({"verilator", "icarus", "iverilog"})
_LINT_EDA_TOOLS = frozenset({"verilator", "verible"})


def flow_can_drive(flow: str, ref: TargetRef | TargetHandle) -> bool:
    """Return whether a Booley Flow can drive a declared Target.

    FPGA intent comes from the Target axis when present because that Flow
    rebuilds the resolved inputs into a Vivado EDAM. The declared EDA tool
    remains a FuseSoC resolution input, not the FPGA execution backend.
    """
    from booley.targets.flow_names import canonical

    flow = canonical(flow)
    if flow not in TARGET_AWARE_FLOWS:
        raise ValueError(
            f"{flow!r} is not a target-aware Booley Flow; "
            f"choose one of: {', '.join(TARGET_AWARE_FLOWS)}"
        )
    if target_naming.fpga_intent(ref.name, ref.eda_tool):
        return flow == "fpga"
    if flow == "sim":
        return ref.eda_tool in _SIM_EDA_TOOLS and (ref.flow == "sim" or ref.flow is None)
    if flow == "lint":
        return ref.flow == "lint" or (ref.flow is None and ref.eda_tool in _LINT_EDA_TOOLS)
    if flow == "synth":
        return ref.eda_tool == "yosys"
    return False


@dataclass(frozen=True)
class TargetHandle:
    """Stable Target identity plus the selector accepted by Booley Flows."""

    identity: str
    selector: str
    name: str
    vlnv: str
    core_file: Path
    flow: str | None
    eda_tool: str | None
    drivable_by: tuple[str, ...]


@dataclass(frozen=True)
class TargetInput:
    """One condition-selected Target input in Project path coordinates."""

    path: str
    core: str
    file_type: str
    tags: tuple[str, ...]
    is_include: bool
    attributes: Mapping[str, Any]


@dataclass(frozen=True)
class TargetInspection:
    """Condition-selected declarations available before execution setup."""

    handle: TargetHandle
    toplevel: str
    flow: str | None
    eda_tool: str | None
    flow_options: Mapping[str, Any]
    parameters: Mapping[str, Any]
    inputs: tuple[TargetInput, ...]

    @property
    def rtl_files(self) -> tuple[str, ...]:
        """Selected non-testbench, non-header source paths."""
        return tuple(
            item.path for item in self.inputs if "tb" not in item.tags and not item.is_include
        )

    @property
    def tb_files(self) -> tuple[str, ...]:
        """Selected testbench source paths."""
        return tuple(item.path for item in self.inputs if "tb" in item.tags)


@dataclass(frozen=True)
class _InspectionConfig:
    """Small in-memory config slice consumed by FuseSoC's CoreManager."""

    cache_root: str = ""
    library_root: str = ""
    resolve_env_vars_early: bool = False
    allow_additional_properties: bool = False


def _selection_bucket(project_root: Path, ref: TargetRef) -> list[TargetRef]:
    bucket = fusesoc_registry.target_declarations(project_root)[ref.name]
    if ref.doctor_selftest:
        return bucket
    return [candidate for candidate in bucket if not candidate.doctor_selftest]


def _handle_from_ref(
    project_root: Path,
    token: str,
    ref: TargetRef,
    *,
    for_flow: str | None,
) -> TargetHandle:
    """Construct the durable handle for one already-authorized Target."""
    if for_flow is not None and not flow_can_drive(for_flow, ref):
        raise fusesoc_registry.IncompatibleTargetError(
            f"Target {token!r} cannot be driven by the {for_flow!r} Flow "
            f"(declared flow={ref.flow!r}, EDA tool={ref.eda_tool!r}). "
            f"Choose a compatible Target with `booley targets --for-flow {for_flow}`."
        )
    bucket = _selection_bucket(project_root, ref)
    return TargetHandle(
        identity=f"{ref.vlnv}#{ref.name}",
        selector=fusesoc_registry.minimal_selector(ref, bucket),
        name=ref.name,
        vlnv=ref.vlnv,
        core_file=ref.core_file,
        flow=ref.flow,
        eda_tool=ref.eda_tool,
        drivable_by=tuple(flow for flow in TARGET_AWARE_FLOWS if flow_can_drive(flow, ref)),
    )


def select_target(
    project_root: Path | str,
    token: str,
    *,
    for_flow: str | None = None,
) -> TargetHandle:
    """Select one user-visible or Doctor-private Target as a durable handle."""
    root = Path(project_root)
    ref = fusesoc_registry.resolve_selected_ref(root, token)
    return _handle_from_ref(root, token, ref, for_flow=for_flow)


def select_targets(
    project_root: Path | str,
    target_arg: str | None,
    *,
    for_flow: str | None = None,
) -> tuple[TargetHandle, ...]:
    """Select an endpoint's comma-separated Targets as canonical handles."""
    tokens = fusesoc_registry.parse_target_tokens(target_arg)
    return tuple(select_target(project_root, token, for_flow=for_flow) for token in tokens)


def _inspection_flags(handle: TargetHandle) -> dict[str, Any]:
    flags: dict[str, Any] = {"is_toplevel": True, "target": handle.name}
    if handle.flow:
        flags["flow"] = handle.flow
    if handle.eda_tool:
        flags["tool"] = handle.eda_tool
        flags[f"tool_{handle.eda_tool}"] = True
    return flags


def _inspection_cores(root: Path, handle: TargetHandle, flags: Mapping[str, Any]) -> list[Any]:
    plan = fusesoc_registry.prepare_core_library_plan(root)
    selected_core = plan.operational_core(handle.core_file)
    manager = CoreManager(_InspectionConfig(), library_manager=LibraryManager(""))
    for index, (library_root, ignored_dirs) in enumerate(
        zip(plan.roots, plan.ignored_dirs, strict=True)
    ):
        manager.add_library(
            Library(f"project-{index}", str(library_root)),
            ignored_dirs=set(ignored_dirs),
        )
    top = manager.get_core(Vlnv(handle.vlnv))
    actual_core = Path(top.core_file).resolve()
    if actual_core != selected_core.resolve():
        raise fusesoc_registry.FuseSocError(
            f"FuseSoC selected {actual_core} for {handle.vlnv}; "
            f"Booley authored {handle.core_file} and expected operational view {selected_core}"
        )
    return manager.get_depends(top.name, dict(flags))


def _inspect_inputs(
    root: Path, cores: list[Any], flags: Mapping[str, Any]
) -> tuple[TargetInput, ...]:
    inputs: list[TargetInput] = []
    top = cores[-1]
    for core in cores:
        core_flags = dict(flags)
        core_flags["is_toplevel"] = core.name == top.name
        for item in core.get_files(core_flags):
            inputs.append(
                TargetInput(
                    path=fusesoc_registry.core_relative_to_project(
                        Path(core.core_file), root, str(item["name"])
                    ),
                    core=str(core.name),
                    file_type=str(item.get("file_type", "user")),
                    tags=tuple(item.get("tags") or ()),
                    is_include=bool(item.get("is_include_file")),
                    attributes={key: value for key, value in item.items() if key != "name"},
                )
            )
    return tuple(inputs)


def inspect_target(project_root: Path | str, token: str) -> TargetInspection:
    """Inspect selected declarations using FuseSoC's own CAPI2 evaluator."""
    root = Path(project_root)
    handle = select_target(root, token)
    try:
        flags = _inspection_flags(handle)
        cores = _inspection_cores(root, handle, flags)
        core = cores[-1]
        return TargetInspection(
            handle=handle,
            toplevel=str(core.get_toplevel(flags)),
            flow=core.get_flow(flags),
            eda_tool=handle.eda_tool,
            flow_options=dict(core.get_flow_options(flags)),
            parameters=dict(core.get_parameters(flags)),
            inputs=_inspect_inputs(root, cores, flags),
        )
    except (DependencyError, OSError, SyntaxError, RuntimeError, ValueError) as exc:
        raise fusesoc_registry.FuseSocError(
            f"could not inspect Target {handle.identity!r}: {exc}"
        ) from exc

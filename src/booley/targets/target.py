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


@dataclass(frozen=True, init=False)
class TargetHandle:
    """Stable Target identity plus the selector accepted by Booley Flows."""

    def __new__(cls) -> TargetHandle:
        raise TypeError("TargetHandle values are created by select_target(s)")

    identity: str
    selector: str
    name: str
    vlnv: str
    core_file: Path
    flow: str | None
    eda_tool: str | None
    drivable_by: tuple[str, ...]
    project_root: Path
    doctor_private: bool


@dataclass(frozen=True)
class TargetInput:
    """One condition-selected Target input in Project path coordinates."""

    path: str
    core: str
    file_type: str
    tags: tuple[str, ...]
    is_include: bool
    attributes: Mapping[str, object]


@dataclass(frozen=True)
class TargetInspection:
    """Condition-selected declarations available before execution setup."""

    handle: TargetHandle
    toplevel: str
    flow: str | None
    eda_tool: str | None
    flow_options: Mapping[str, object]
    parameters: Mapping[str, object]
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
    values: dict[str, object] = {
        "identity": f"{ref.vlnv}#{ref.name}",
        "selector": fusesoc_registry.minimal_selector(ref, bucket),
        "name": ref.name,
        "vlnv": ref.vlnv,
        "core_file": ref.core_file.resolve(),
        "flow": ref.flow,
        "eda_tool": ref.eda_tool,
        "drivable_by": tuple(flow for flow in TARGET_AWARE_FLOWS if flow_can_drive(flow, ref)),
        "project_root": project_root.resolve(),
        "doctor_private": ref.doctor_selftest,
    }
    handle = object.__new__(TargetHandle)
    for name, value in values.items():
        object.__setattr__(handle, name, value)
    return handle


def select_target(
    project_root: Path | str,
    token: str,
    *,
    for_flow: str | None = None,
) -> TargetHandle:
    """Select one user-visible or Doctor-private Target as a durable handle."""
    root = Path(project_root).resolve()
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


def inspect_target(project_root: Path | str, handle: TargetHandle) -> TargetInspection:
    """Inspect one authorized handle using FuseSoC's own CAPI2 evaluator."""
    root = Path(project_root).resolve()
    if root != handle.project_root:
        raise fusesoc_registry.FuseSocError(
            f"Target {handle.identity!r} was selected for Project "
            f"{handle.project_root}, not {root}"
        )
    from booley.fusesoc.target_inspection import inspect_handle

    return inspect_handle(root, handle)


def inspect_target_selector(
    project_root: Path | str,
    token: str,
    *,
    for_flow: str | None = None,
) -> TargetInspection:
    """Compatibility adapter that selects one external token and inspects it."""
    root = Path(project_root).resolve()
    return inspect_target(root, select_target(root, token, for_flow=for_flow))

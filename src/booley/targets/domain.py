"""Dependency-neutral Target values, policy, and errors."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import InitVar, dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import cast

from booley.targets import target_naming

TARGET_AWARE_FLOWS: tuple[str, ...] = ("synth", "fpga", "lint", "sim")
TARGET_IDENTITY_PARAM = "target"
TARGET_SELECTOR_PARAM = "_target_selector"

_SIM_EDA_TOOLS = frozenset({"verilator", "icarus", "iverilog"})
_LINT_EDA_TOOLS = frozenset({"verilator", "verible"})


class FuseSocError(Exception):
    """Base for Target discovery, inspection, and FuseSoC resolution failures."""


class CoreCollisionError(FuseSocError):
    """The same logical VLNV is authored in both supported core roots."""


class TargetResolutionError(FuseSocError):
    """FuseSoC setup failed, or its resolved EDAM could not be read."""


class MissingSourceError(TargetResolutionError):
    """A Target references source paths that do not exist on disk."""


class UnknownTargetError(FuseSocError):
    """A selection token does not identify a visible Target."""


class AmbiguousTargetError(FuseSocError):
    """A selection token identifies more than one Target."""


class IncompatibleTargetError(FuseSocError):
    """A Target exists but the requested Booley Flow cannot drive it."""


class ForeignTargetHandleError(FuseSocError):
    """A Target handle belongs to another Project or catalog snapshot."""


class StaleTargetCatalogError(FuseSocError):
    """Target declaration or projection inputs changed after catalog creation."""


@dataclass(frozen=True)
class TargetRef:
    """One Target declaration resolved to its authored core."""

    name: str
    vlnv: str
    core_file: Path
    eda_tool: str | None = None
    flow: str | None = None
    cocotb_module: str | None = None
    doctor_flows: tuple[str, ...] = ()
    doctor_selftest: bool = False


@dataclass(frozen=True)
class CoreSources:
    """A Target's selected inputs partitioned into RTL and testbench paths."""

    rtl_source_files: tuple[str, ...]
    tb_files: tuple[str, ...]


_HANDLE_FACTORY_KEY = object()


@dataclass(frozen=True)
class TargetHandle:
    """Stable Target identity plus its callable selector and snapshot provenance."""

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
    cocotb_module: str | None = None
    doctor_flows: tuple[str, ...] = ()
    declared_toplevel: str = ""
    snapshot_id: str = field(default="", repr=False, compare=False)
    _factory_key: InitVar[object] = None

    def __post_init__(self, _factory_key: object) -> None:
        if _factory_key is not _HANDLE_FACTORY_KEY:
            raise TypeError("TargetHandle values are created by TargetCatalog.select()")


@dataclass(frozen=True)
class TargetInput:
    """One condition-selected Target input in Project path coordinates."""

    path: str
    core: str
    file_type: str
    tags: tuple[str, ...]
    is_include: bool
    attributes: Mapping[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(self, "attributes", immutable_mapping(self.attributes))


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

    def __post_init__(self) -> None:
        object.__setattr__(self, "flow_options", immutable_mapping(self.flow_options))
        object.__setattr__(self, "parameters", immutable_mapping(self.parameters))

    @property
    def sources(self) -> CoreSources:
        """Return the selected inputs partitioned for source-aware callers."""
        return partition_target_inputs(self.inputs)

    @property
    def rtl_files(self) -> tuple[str, ...]:
        """Selected non-testbench, non-header source paths."""
        return self.sources.rtl_source_files

    @property
    def tb_files(self) -> tuple[str, ...]:
        """Selected testbench source paths."""
        return self.sources.tb_files


def partition_target_inputs(inputs: Iterable[TargetInput]) -> CoreSources:
    """Apply Booley's single RTL/testbench partition policy to Target inputs."""
    selected = tuple(inputs)
    return CoreSources(
        rtl_source_files=tuple(
            item.path for item in selected if "tb" not in item.tags and not item.is_include
        ),
        tb_files=tuple(item.path for item in selected if "tb" in item.tags),
    )


def immutable_mapping(value: Mapping[str, object]) -> Mapping[str, object]:
    """Return a recursively immutable copy of an untrusted mapping value."""
    return _immutable_mapping(cast(Mapping[object, object], value))


def _immutable_mapping(value: Mapping[object, object]) -> Mapping[str, object]:
    return MappingProxyType({str(key): _immutable_value(item) for key, item in value.items()})


def _immutable_value(value: object) -> object:
    if isinstance(value, Mapping):
        return _immutable_mapping(cast(Mapping[object, object], value))
    if isinstance(value, (list, tuple)):
        return tuple(_immutable_value(item) for item in cast(Iterable[object], value))
    if isinstance(value, (set, frozenset)):
        return frozenset(_immutable_value(item) for item in cast(Iterable[object], value))
    return value


def criterion_matches_target(
    params: Mapping[str, object],
    *,
    identity: str,
    selector: str,
) -> bool:
    """Match criterion metadata to one canonical Target identity/selector pair."""
    expected_identity = params.get(TARGET_IDENTITY_PARAM)
    if TARGET_SELECTOR_PARAM in params:
        return expected_identity == identity and params.get(TARGET_SELECTOR_PARAM) == selector
    return expected_identity in {identity, selector}


def flow_can_drive(flow: str, target: TargetRef | TargetHandle) -> bool:
    """Return whether a Booley Flow can drive a declared Target."""
    from booley.targets.flow_names import canonical

    flow = canonical(flow)
    if flow not in TARGET_AWARE_FLOWS:
        raise ValueError(
            f"{flow!r} is not a target-aware Booley Flow; "
            f"choose one of: {', '.join(TARGET_AWARE_FLOWS)}"
        )
    if target_naming.fpga_intent(target.name, target.eda_tool):
        return flow == "fpga"
    if flow == "sim":
        return target.eda_tool in _SIM_EDA_TOOLS and (target.flow == "sim" or target.flow is None)
    if flow == "lint":
        return target.flow == "lint" or (
            target.flow is None and target.eda_tool in _LINT_EDA_TOOLS
        )
    if flow == "synth":
        return target.eda_tool == "yosys"
    return False


__all__ = [
    "TARGET_AWARE_FLOWS",
    "TARGET_IDENTITY_PARAM",
    "TARGET_SELECTOR_PARAM",
    "AmbiguousTargetError",
    "CoreCollisionError",
    "CoreSources",
    "ForeignTargetHandleError",
    "FuseSocError",
    "IncompatibleTargetError",
    "MissingSourceError",
    "StaleTargetCatalogError",
    "TargetHandle",
    "TargetInput",
    "TargetInspection",
    "TargetRef",
    "TargetResolutionError",
    "UnknownTargetError",
    "criterion_matches_target",
    "flow_can_drive",
    "immutable_mapping",
    "partition_target_inputs",
]

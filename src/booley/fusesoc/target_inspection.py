"""Untyped FuseSoC Python-API adapter for pre-setup Target inspection."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fusesoc.coremanager import CoreManager, DependencyError
from fusesoc.librarymanager import Library, LibraryManager
from fusesoc.vlnv import Vlnv

from booley.fusesoc import fusesoc_registry

if TYPE_CHECKING:
    from booley.targets.target import TargetHandle, TargetInput, TargetInspection


@dataclass(frozen=True)
class _InspectionConfig:
    cache_root: str = ""
    library_root: str = ""
    resolve_env_vars_early: bool = False
    allow_additional_properties: bool = False


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
    from booley.targets.target import TargetInput

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


def inspect_handle(root: Path, handle: TargetHandle) -> TargetInspection:
    """Inspect a selected handle while containing FuseSoC's untyped API."""
    from booley.targets.target import TargetInspection

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

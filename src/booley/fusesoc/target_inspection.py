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


def _target_flags(name: str, flow: str | None, eda_tool: str | None) -> dict[str, Any]:
    """Build fresh FuseSoC condition flags for one Target."""
    flags: dict[str, Any] = {"is_toplevel": True, "target": name}
    if flow:
        flags["flow"] = flow
    if eda_tool:
        flags["tool"] = eda_tool
        flags[f"tool_{eda_tool}"] = True
    return flags


def _inspection_flags(handle: TargetHandle) -> dict[str, Any]:
    return _target_flags(handle.name, handle.flow, handle.eda_tool)


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


class _TargetInspectionSession:
    """Reuse one prepared FuseSoC library view across Target inspections."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        try:
            self.plan = fusesoc_registry.prepare_core_library_plan(self.root)
            self.manager = CoreManager(_InspectionConfig(), library_manager=LibraryManager(""))
            for index, (library_root, ignored_dirs) in enumerate(
                zip(self.plan.roots, self.plan.ignored_dirs, strict=True)
            ):
                self.manager.add_library(
                    Library(f"project-{index}", str(library_root)),
                    ignored_dirs=set(ignored_dirs),
                )
        except (DependencyError, OSError, SyntaxError, RuntimeError, ValueError) as exc:
            raise fusesoc_registry.FuseSocError(
                f"could not prepare Target inspection: {exc}"
            ) from exc

    def _cores(
        self,
        *,
        identity: str,
        vlnv: str,
        core_file: Path,
        flags: Mapping[str, Any],
    ) -> list[Any]:
        try:
            selected_core = self.plan.operational_core(core_file)
            top = self.manager.get_core(Vlnv(vlnv))
            actual_core = Path(top.core_file).resolve()
            if actual_core != selected_core.resolve():
                raise fusesoc_registry.FuseSocError(
                    f"FuseSoC selected {actual_core} for {vlnv}; "
                    f"Booley authored {core_file} and expected operational view {selected_core}"
                )
            return self.manager.get_depends(top.name, dict(flags))
        except fusesoc_registry.FuseSocError:
            raise
        except (DependencyError, OSError, SyntaxError, RuntimeError, ValueError) as exc:
            raise fusesoc_registry.FuseSocError(
                f"could not inspect Target {identity!r}: {exc}"
            ) from exc

    def inspect_handle(self, handle: TargetHandle) -> TargetInspection:
        """Inspect one canonical handle with fresh Target-specific flags."""
        from booley.targets.target import TargetInspection

        flags = _inspection_flags(handle)
        cores = self._cores(
            identity=handle.identity,
            vlnv=handle.vlnv,
            core_file=handle.core_file,
            flags=flags,
        )
        core = cores[-1]
        try:
            return TargetInspection(
                handle=handle,
                toplevel=str(core.get_toplevel(flags)),
                flow=core.get_flow(flags),
                eda_tool=handle.eda_tool,
                flow_options=dict(core.get_flow_options(flags)),
                parameters=dict(core.get_parameters(flags)),
                inputs=_inspect_inputs(self.root, cores, flags),
            )
        except (OSError, SyntaxError, RuntimeError, ValueError) as exc:
            raise fusesoc_registry.FuseSocError(
                f"could not inspect Target {handle.identity!r}: {exc}"
            ) from exc

    def source_partition(
        self,
        ref: fusesoc_registry.TargetRef,
    ) -> fusesoc_registry.CoreSources:
        """Resolve one Target's RTL/testbench partition through the shared view."""
        identity = f"{ref.vlnv}#{ref.name}"
        flags = _target_flags(ref.name, ref.flow, ref.eda_tool)
        cores = self._cores(
            identity=identity,
            vlnv=ref.vlnv,
            core_file=ref.core_file,
            flags=flags,
        )
        try:
            inputs = _inspect_inputs(self.root, cores, flags)
        except (OSError, SyntaxError, RuntimeError, ValueError) as exc:
            raise fusesoc_registry.FuseSocError(
                f"could not inspect Target {identity!r}: {exc}"
            ) from exc
        return fusesoc_registry.CoreSources(
            rtl_source_files=tuple(
                item.path for item in inputs if "tb" not in item.tags and not item.is_include
            ),
            tb_files=tuple(item.path for item in inputs if "tb" in item.tags),
        )


def inspect_handle(root: Path, handle: TargetHandle) -> TargetInspection:
    """Inspect a selected handle while containing FuseSoC's untyped API."""
    try:
        return _TargetInspectionSession(root).inspect_handle(handle)
    except (DependencyError, OSError, SyntaxError, RuntimeError, ValueError) as exc:
        raise fusesoc_registry.FuseSocError(
            f"could not inspect Target {handle.identity!r}: {exc}"
        ) from exc

"""Untyped FuseSoC Python-API adapter for pre-setup Target inspection."""

from __future__ import annotations

import json
import re
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


_TARGET_CONDITION_RE = re.compile(r"\btarget_(?P<name>[A-Za-z0-9_.-]+)\b")


def _conditioned_target_names(value: Any) -> set[str]:
    """Return Target names referenced by CAPI2 conditional expressions."""
    if isinstance(value, str):
        if "?" not in value:
            return set()
        return {match.group("name") for match in _TARGET_CONDITION_RE.finditer(value)}
    if isinstance(value, Mapping):
        names: set[str] = set()
        for key, item in value.items():
            names.update(_conditioned_target_names(key))
            names.update(_conditioned_target_names(item))
        return names
    if isinstance(value, (list, tuple)):
        names = set()
        for item in value:
            names.update(_conditioned_target_names(item))
        return names
    return set()


class TargetSourceInspector:
    """Inspect and cache Target sources through one Project library view."""

    def __init__(self, project_root: Path | str) -> None:
        self.root = Path(project_root).resolve()
        self._prepared_state: tuple[fusesoc_registry.CoreLibraryPlan, CoreManager] | None = None
        self._startup_error: fusesoc_registry.FuseSocError | None = None
        self._documents: dict[Path, dict[str, Any]] = {}
        self._conditioned_targets: frozenset[str] = frozenset()
        self._sources: dict[tuple[object, ...], fusesoc_registry.CoreSources] = {}

    def _prepare(self) -> tuple[fusesoc_registry.CoreLibraryPlan, CoreManager]:
        """Prepare and retain the shared FuseSoC state on first use."""
        if self._startup_error is not None:
            raise self._startup_error
        if self._prepared_state is not None:
            return self._prepared_state
        try:
            plan = fusesoc_registry.prepare_core_library_plan(self.root)
            manager = CoreManager(_InspectionConfig(), library_manager=LibraryManager(""))
            for index, (library_root, ignored_dirs) in enumerate(
                zip(plan.roots, plan.ignored_dirs, strict=True)
            ):
                manager.add_library(
                    Library(f"project-{index}", str(library_root)),
                    ignored_dirs=set(ignored_dirs),
                )
            documents = {
                core_file.resolve(): fusesoc_registry.read_core(core_file)
                for core_file in fusesoc_registry.discover_cores(self.root)
            }
            conditioned_targets: set[str] = set()
            for document in documents.values():
                conditioned_targets.update(_conditioned_target_names(document))
            self._documents = documents
            self._conditioned_targets = frozenset(conditioned_targets)
            self._prepared_state = (plan, manager)
            return self._prepared_state
        except fusesoc_registry.FuseSocError as exc:
            self._startup_error = exc
            raise
        except (DependencyError, OSError, SyntaxError, RuntimeError, ValueError) as exc:
            error = fusesoc_registry.FuseSocError(f"could not prepare Target inspection: {exc}")
            self._startup_error = error
            raise error from exc

    def _source_key(self, ref: fusesoc_registry.TargetRef) -> tuple[object, ...]:
        """Identify source-equivalent Targets without merging conditional variants."""
        self._prepare()
        core_file = ref.core_file.resolve()
        document = self._documents.get(core_file)
        if document is None:
            document = fusesoc_registry.read_core(core_file)
            self._documents[core_file] = document
        targets = document.get("targets")
        declaration = targets.get(ref.name) if isinstance(targets, Mapping) else None
        declaration_key = json.dumps(
            declaration,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        conditioned_name = ref.name if ref.name in self._conditioned_targets else None
        return (
            core_file,
            ref.vlnv,
            ref.flow,
            ref.eda_tool,
            conditioned_name,
            declaration_key,
        )

    def _cores(
        self,
        *,
        identity: str,
        vlnv: str,
        core_file: Path,
        flags: Mapping[str, Any],
    ) -> list[Any]:
        plan, manager = self._prepare()
        try:
            selected_core = plan.operational_core(core_file)
            top = manager.get_core(Vlnv(vlnv))
            actual_core = Path(top.core_file).resolve()
            if actual_core != selected_core.resolve():
                raise fusesoc_registry.FuseSocError(
                    f"FuseSoC selected {actual_core} for {vlnv}; "
                    f"Booley authored {core_file} and expected operational view {selected_core}"
                )
            return manager.get_depends(top.name, dict(flags))
        except fusesoc_registry.FuseSocError:
            raise
        except (DependencyError, OSError, SyntaxError, RuntimeError, ValueError) as exc:
            raise fusesoc_registry.FuseSocError(
                f"could not inspect Target {identity!r}: {exc}"
            ) from exc

    def _inspect_handle(self, handle: TargetHandle) -> TargetInspection:
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

    def inspect(
        self,
        ref: fusesoc_registry.TargetRef,
    ) -> fusesoc_registry.CoreSources:
        """Return one Target partition, reusing source-equivalent results."""
        source_key = self._source_key(ref)
        cached = self._sources.get(source_key)
        if cached is not None:
            return cached
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
        from booley.targets.target import partition_target_inputs

        sources = partition_target_inputs(inputs)
        self._sources[source_key] = sources
        return sources


def inspect_handle(root: Path, handle: TargetHandle) -> TargetInspection:
    """Inspect a selected handle while containing FuseSoC's untyped API."""
    try:
        return TargetSourceInspector(root)._inspect_handle(handle)
    except (DependencyError, OSError, SyntaxError, RuntimeError, ValueError) as exc:
        raise fusesoc_registry.FuseSocError(
            f"could not inspect Target {handle.identity!r}: {exc}"
        ) from exc

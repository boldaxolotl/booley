"""Presentation of the Project's runnable Target catalog (``booley targets``).

A read-only presentation layer over :class:`booley.targets.catalog.TargetCatalog`.
Shared by the CLI verb and MCP tool so both present the same catalog facts as
JSON payloads; the terminal views of those payloads live in
:mod:`booley.harness.targets_view`.

Vocabulary (docs/GLOSSARY.md): a **Target** is a named FuseSoC ``.core`` build
target, identified by ``(VLNV, name)`` (ADR 0030). "Doctor" means the Target
selects a smoke Flow in ``flow_options.booley.doctor``; "drivable" means the
Booley Flow *could* run it (Flow/EDA-tool compatibility), selected or not. Health auditing
(legacy upstream FuseSoC ``tools:`` authoring, missing ``tags:[tb]``, …) stays in doctor —
this module only describes, it never judges.
"""

from __future__ import annotations

import fnmatch
import subprocess
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import NotRequired, TypedDict

from booley.core.build_paths import work_root_for
from booley.fusesoc import fusesoc_registry
from booley.fusesoc.core_projection import lexical_stealth_path
from booley.targets.catalog import TargetCatalog
from booley.targets.domain import TARGET_AWARE_FLOWS, TargetHandle

# Glob metacharacters: a `booley targets` positional containing any of these is
# a filter pattern; anything else is a selection token for the detail view.
_GLOB_CHARS = frozenset("*?[")


def is_glob(token: str) -> bool:
    """True when a ``booley targets`` positional is a filter pattern."""
    return any(c in _GLOB_CHARS for c in token)


@dataclass(frozen=True)
class CoreGroup:
    """All Targets one ``.core`` declares — the listing's grouping unit."""

    vlnv: str
    core_file: Path
    entries: tuple[TargetHandle, ...]


@dataclass(frozen=True)
class TargetSurface:
    """The whole surface: per-core groups plus non-fatal observations."""

    groups: tuple[CoreGroup, ...]
    warnings: tuple[str, ...]
    """Non-fatal observations produced while building the surface."""

    def entries(self) -> Iterator[TargetHandle]:
        for group in self.groups:
            yield from group.entries


class TargetRow(TypedDict):
    """JSON row for one listed Target."""

    name: str
    selector: str
    flow: str | None
    eda_tool: str | None
    cocotb_module: str | None
    toplevel: str | None
    doctor_flows: list[str]
    drivable_by: list[str]


class CoreRow(TypedDict):
    """JSON row for one core and its public Targets."""

    vlnv: str
    core_file: str
    targets: list[TargetRow]


class SurfacePayload(TypedDict):
    """JSON payload for a Target listing."""

    cores: list[CoreRow]
    warnings: list[str]


class ResolvedDetail(TypedDict):
    """Resolved half of one Target detail payload."""

    toplevel: str
    eda_tool: str | None
    cocotb_module: str | None
    parameters: Mapping[str, object]
    rtl_hdl_sources: int
    rtl_include_dirs: int
    tb_files: int
    sdc_files: list[str]
    xdc_files: list[str]
    build_root: str


class TargetDetailPayload(TypedDict):
    """JSON payload for one selected Target."""

    name: str
    selector: str
    vlnv: str
    core_file: str
    flow: str | None
    eda_tool: str | None
    cocotb_module: str | None
    toplevel: str | None
    doctor_flows: list[str]
    drivable_by: list[str]
    warnings: list[str]
    resolved: NotRequired[ResolvedDetail]
    resolved_error: NotRequired[str]
    resolution_command: NotRequired[str]


def collect_surface(project_root: Path | str) -> TargetSurface:
    """Build the public Target surface for *project_root* — YAML reads only."""
    return _surface_from_catalog(TargetCatalog.build(project_root))


def _surface_from_catalog(catalog: TargetCatalog) -> TargetSurface:
    grouped: dict[tuple[str, Path], list[TargetHandle]] = {}
    for handle in catalog.list():
        grouped.setdefault((handle.vlnv, handle.core_file), []).append(handle)

    groups = tuple(
        CoreGroup(
            vlnv=vlnv,
            core_file=core_file,
            entries=tuple(sorted(entries, key=lambda entry: entry.name)),
        )
        for (vlnv, core_file), entries in sorted(grouped.items(), key=lambda kv: kv[0])
    )
    return TargetSurface(groups=groups, warnings=())


def filter_surface(
    surface: TargetSurface,
    *,
    for_flow: str | None = None,
    glob: str | None = None,
) -> TargetSurface:
    """Narrow *surface* to Targets *for_flow* could drive and/or a name glob.

    The glob matches (case-sensitively) the bare name, the minimal selector,
    or the fully-qualified ``vendor:library:name#target`` form, so
    ``'soc*'``, ``'*#lint'`` and ``'*soc*#*'`` all do what they look like.
    Raises :class:`ValueError` when *for_flow* is not a target-aware Booley Flow.
    """

    if for_flow is not None:
        from booley.targets.flow_names import canonical

        for_flow = canonical(for_flow)

    def keep(entry: TargetHandle) -> bool:
        if for_flow is not None and for_flow not in entry.drivable_by:
            return False
        if glob is not None:
            candidates = (
                entry.name,
                entry.selector,
                f"{_vlnv_identity(entry.vlnv)}#{entry.name}",
            )
            if not any(fnmatch.fnmatchcase(c, glob) for c in candidates):
                return False
        return True

    if for_flow is not None and for_flow not in TARGET_AWARE_FLOWS:
        raise ValueError(
            f"{for_flow!r} is not a target-aware Booley Flow; "
            f"choose one of: {', '.join(TARGET_AWARE_FLOWS)}"
        )
    groups: list[CoreGroup] = []
    for group in surface.groups:
        kept = tuple(e for e in group.entries if keep(e))
        if kept:
            groups.append(replace(group, entries=kept))
    return TargetSurface(groups=tuple(groups), warnings=surface.warnings)


def _vlnv_identity(vlnv: str) -> str:
    """``vendor:library:name`` with any trailing ``:version`` dropped."""
    parts = vlnv.split(":")
    return ":".join(parts[:3]) if len(parts) >= 3 else vlnv


def _rel_to(path: Path, root: Path) -> str:
    """Project-relative POSIX display path (absolute when outside *root*)."""
    try:
        return lexical_stealth_path(root, path).relative_to(root).as_posix()
    except ValueError:
        return str(path)


# ---------------------------------------------------------------------------
# JSON payloads — the MCP tool's output and `booley targets --json`
# ---------------------------------------------------------------------------


def surface_payload(surface: TargetSurface, project_root: Path | str) -> SurfacePayload:
    """JSON-ready view of a (possibly filtered) surface."""
    root = Path(project_root)
    return {
        "cores": [
            {
                "vlnv": group.vlnv,
                "core_file": _rel_to(group.core_file, root),
                "targets": [
                    {
                        "name": e.name,
                        "selector": e.selector,
                        "flow": e.flow,
                        "eda_tool": e.eda_tool,
                        "cocotb_module": e.cocotb_module,
                        "toplevel": e.declared_toplevel or None,
                        "doctor_flows": list(e.doctor_flows),
                        "drivable_by": list(e.drivable_by),
                    }
                    for e in group.entries
                ],
            }
            for group in surface.groups
        ],
        "warnings": list(surface.warnings),
    }


def detail_payload(
    project_root: Path | str,
    token: str,
    *,
    resolve: bool = True,
    fusesoc_cmd: Sequence[str] = fusesoc_registry.DEFAULT_FUSESOC_CMD,
    env: Mapping[str, str] | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> TargetDetailPayload:
    """Everything ``booley targets <name>`` shows for one Target.

    The cheap half always fills in (enumeration + Doctor metadata); when enabled,
    the resolved half runs ``fusesoc run --setup`` and lands under ``"resolved"``.
    A resolution failure lands under ``"resolved_error"`` instead of raising. Unknown and
    ambiguous *token*\\ s DO raise (:class:`booley.targets.domain.UnknownTargetError`
    / :class:`booley.targets.domain.AmbiguousTargetError`) — their messages already
    name the candidates. The remaining keyword arguments mirror
    :func:`fusesoc_registry.resolve_target`.
    """
    root = Path(project_root)
    catalog = TargetCatalog.build(root)
    handle = catalog.select(token)
    surface = _surface_from_catalog(catalog)
    entry = next(e for e in surface.entries() if e.selector == handle.selector)
    selected = entry

    payload: TargetDetailPayload = {
        "name": selected.name,
        "selector": entry.selector,
        "vlnv": selected.vlnv,
        "core_file": _rel_to(selected.core_file, root),
        "flow": selected.flow,
        "eda_tool": selected.eda_tool,
        "cocotb_module": selected.cocotb_module,
        "toplevel": entry.declared_toplevel or None,
        "doctor_flows": list(entry.doctor_flows),
        "drivable_by": list(entry.drivable_by),
        "warnings": list(surface.warnings),
    }
    if not resolve:
        return payload

    build_root = work_root_for(root, "targets", selected.name)
    try:
        resolved = fusesoc_registry.resolve_target_handle(
            handle,
            build_root=build_root,
            fusesoc_cmd=fusesoc_cmd,
            env=env,
            runner=runner,
        )
    except fusesoc_registry.FuseSocError as exc:
        message = str(exc).strip()
        payload["resolved_error"] = message.splitlines()[0] if message else type(exc).__name__
    else:
        payload["resolved"] = {
            "toplevel": resolved.toplevel,
            "eda_tool": resolved.configured_eda_tool,
            "cocotb_module": resolved.cocotb_module,
            "parameters": dict(resolved.parameters),
            "rtl_hdl_sources": len(resolved.rtl_hdl_source_files),
            "rtl_include_dirs": len(resolved.rtl_include_dirs),
            "tb_files": len(resolved.tb_files),
            "sdc_files": [f.name for f in resolved.sdc_files],
            "xdc_files": [f.name for f in resolved.xdc_files],
            "build_root": _rel_to(resolved.build_root, root),
        }
    return payload

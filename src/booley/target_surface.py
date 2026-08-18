"""target_surface.py — the project's runnable-Target surface (``booley targets``).

A read-only presentation layer over :mod:`booley.fusesoc_registry`: the cheap
YAML-only enumeration (never ``fusesoc run``) joined with the ``[flows.*].default_target``
wiring from booley.toml. Shared by the ``booley targets`` CLI verb and the
``booley_targets`` MCP tool so both render the same facts.

Vocabulary (docs/CONTEXT.md): a **Target** is a named FuseSoC ``.core`` build
target, identified by ``(VLNV, name)`` (ADR 0030). "Wired" means a Booley Flow's
``[flows.<flow>].default_target`` selection names it; "drivable" means the Booley Flow
*could* run it (Flow/EDA-tool compatibility), wired or not. Health auditing
(legacy upstream FuseSoC ``tools:`` authoring, missing ``tags:[tb]``, …) stays in doctor —
this module only describes, it never judges.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from booley import fusesoc_registry
from booley.fusesoc_registry import TargetRef

# The five build Booley Flows represented by the Target compatibility surface.
# These are the values ``booley targets --for`` accepts.
TARGET_AWARE_FLOWS: tuple[str, ...] = (
    "synth",
    "elab",
    "fpga",
    "lint",
    "sim",
)

# Declared-EDA-tool families used only as the fallback classification for
# legacy upstream FuseSoC `tools:`-style Targets, which enumerate with flow=None (doctor flags
# the authoring; `--for` should still not hide them).
_SIM_EDA_TOOLS = frozenset({"verilator", "icarus", "iverilog"})
_LINT_EDA_TOOLS = frozenset({"verilator", "verible"})

# Glob metacharacters: a `booley targets` positional containing any of these is
# a filter pattern; anything else is a selection token for the detail view.
_GLOB_CHARS = frozenset("*?[")


def is_glob(token: str) -> bool:
    """True when a ``booley targets`` positional is a filter pattern."""
    return any(c in _GLOB_CHARS for c in token)


def flow_can_drive(flow: str, ref: TargetRef) -> bool:
    """Could *flow* drive *ref*? Compatibility, not ``[flows.*].default_target`` wiring.

    simulate and elaborate want a sim-flow Target (elaborate builds the sim
    executable without running it); lint wants a lint-flow Target; the two
    synthesis Flows key off the declared EDA tool, because CAPI2 has no
    "synth" flow — a yosys and a vivado Target are both ``generic``.

    Raises :class:`ValueError` for anything outside
    :data:`TARGET_AWARE_FLOWS` (Specialists are not target-aware).
    """
    from booley.flow_names import canonical

    flow = canonical(flow)
    if flow not in TARGET_AWARE_FLOWS:
        raise ValueError(
            f"{flow!r} is not a target-aware Booley Flow; "
            f"choose one of: {', '.join(TARGET_AWARE_FLOWS)}"
        )
    if flow in ("sim", "elab"):
        # A ``flow: sim`` declaration alone is not an execution entitlement.
        # Commercial simulator Targets may remain in a vendor core, but Booley
        # deliberately does not present them as drivable until an equivalently
        # proven Session-Runtime policy exists.  Apply the same allow-list to
        # modern and legacy ``tools:`` shapes so ``booley targets --for sim``
        # cannot advertise Xcelium/VCS merely because their CAPI2 flow says sim.
        return ref.eda_tool in _SIM_EDA_TOOLS and (ref.flow == "sim" or ref.flow is None)
    if flow == "lint":
        return ref.flow == "lint" or (ref.flow is None and ref.eda_tool in _LINT_EDA_TOOLS)
    if flow == "synth":
        return ref.eda_tool == "yosys"
    return ref.eda_tool == "vivado"  # fpga


@dataclass(frozen=True)
class TargetEntry:
    """One Target row: the enumerated ref plus everything the listing shows."""

    ref: TargetRef
    selector: str
    """Shortest ``--target`` token that uniquely selects this Target
    (:func:`fusesoc_registry.minimal_selector`) — copy-pasteable as-is."""

    toplevel: str
    """Statically-declared ``toplevel`` from the ``.core`` (``""`` when absent
    or parameter-derived — the resolved detail view has the authority)."""

    wired_to: tuple[str, ...]
    """Booley Flows whose ``[flows.<flow>].default_target`` selection names this Target."""

    drivable_by: tuple[str, ...]
    """Booley Flows that could drive this Target (:func:`flow_can_drive`)."""


@dataclass(frozen=True)
class CoreGroup:
    """All Targets one ``.core`` declares — the listing's grouping unit."""

    vlnv: str
    core_file: Path
    entries: tuple[TargetEntry, ...]


@dataclass(frozen=True)
class TargetSurface:
    """The whole surface: per-core groups plus wiring warnings."""

    groups: tuple[CoreGroup, ...]
    warnings: tuple[str, ...]
    """Config tokens that do not resolve — a ``[flows.*].default_target`` naming a
    missing or ambiguous Target is broken wiring worth surfacing here (doctor
    FAILs it too, with more context)."""

    def entries(self) -> Iterator[TargetEntry]:
        for group in self.groups:
            yield from group.entries


def _wired_map(project_root: Path) -> tuple[dict[TargetRef, list[str]], list[str]]:
    """Resolve every ``[flows.*].default_target`` token to (ref → wiring, warnings).

    Tokens route through :func:`fusesoc_registry.resolve_ref` — the same rules
    the Flows apply — so a resolved ref compares equal to the enumerated one.
    """
    from booley.flows.flow_config import resolve_flow_default_target

    wired: dict[TargetRef, list[str]] = {}
    warnings: list[str] = []
    for flow in TARGET_AWARE_FLOWS:
        for raw_token in resolve_flow_default_target(flow, project_root).split(","):
            token = raw_token.strip()
            if not token:
                continue
            try:
                ref = fusesoc_registry.resolve_ref(project_root, token)
            except fusesoc_registry.FuseSocError as exc:
                warnings.append(f"[flows.{flow}].default_target {token!r} does not resolve: {exc}")
                continue
            wired.setdefault(ref, []).append(flow)
    return wired, warnings


def collect_surface(project_root: Path | str) -> TargetSurface:
    """Build the full Target surface for *project_root* — YAML reads only."""
    root = Path(project_root)
    declarations = fusesoc_registry.target_declarations(root)
    wired, warnings = _wired_map(root)

    core_docs: dict[Path, Mapping[str, Any]] = {}

    def declared_toplevel(ref: TargetRef) -> str:
        doc = core_docs.get(ref.core_file)
        if doc is None:
            doc = fusesoc_registry.read_core(ref.core_file)
            core_docs[ref.core_file] = doc
        return fusesoc_registry.core_target_toplevel(doc, ref.name)

    grouped: dict[tuple[str, Path], list[TargetEntry]] = {}
    for bucket in declarations.values():
        for ref in bucket:
            entry = TargetEntry(
                ref=ref,
                selector=fusesoc_registry.minimal_selector(ref, bucket),
                toplevel=declared_toplevel(ref),
                wired_to=tuple(wired.get(ref, ())),
                drivable_by=tuple(b for b in TARGET_AWARE_FLOWS if flow_can_drive(b, ref)),
            )
            grouped.setdefault((ref.vlnv, ref.core_file), []).append(entry)

    groups = tuple(
        CoreGroup(
            vlnv=vlnv,
            core_file=core_file,
            entries=tuple(sorted(entries, key=lambda e: e.ref.name)),
        )
        for (vlnv, core_file), entries in sorted(grouped.items(), key=lambda kv: kv[0])
    )
    return TargetSurface(groups=groups, warnings=tuple(warnings))


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
        from booley.flow_names import canonical

        for_flow = canonical(for_flow)

    def keep(entry: TargetEntry) -> bool:
        if for_flow is not None and not flow_can_drive(for_flow, entry.ref):
            return False
        if glob is not None:
            candidates = (
                entry.ref.name,
                entry.selector,
                f"{_vlnv_identity(entry.ref.vlnv)}#{entry.ref.name}",
            )
            if not any(fnmatch.fnmatchcase(c, glob) for c in candidates):
                return False
        return True

    if for_flow is not None and for_flow not in TARGET_AWARE_FLOWS:
        raise ValueError(
            f"{for_flow!r} is not a target-aware Booley Flow; "
            f"choose one of: {', '.join(TARGET_AWARE_FLOWS)}"
        )
    groups = []
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
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


# ---------------------------------------------------------------------------
# JSON payloads — the MCP tool's output and `booley targets --json`
# ---------------------------------------------------------------------------


def surface_payload(surface: TargetSurface, project_root: Path | str) -> dict[str, Any]:
    """JSON-ready view of a (possibly filtered) surface."""
    root = Path(project_root)
    return {
        "cores": [
            {
                "vlnv": group.vlnv,
                "core_file": _rel_to(group.core_file, root),
                "targets": [
                    {
                        "name": e.ref.name,
                        "selector": e.selector,
                        "flow": e.ref.flow,
                        "eda_tool": e.ref.eda_tool,
                        "cocotb_module": e.ref.cocotb_module,
                        "toplevel": e.toplevel or None,
                        "wired_to": list(e.wired_to),
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
    **resolve_kwargs: Any,
) -> dict[str, Any]:
    """Everything ``booley targets <name>`` shows for one Target.

    The cheap half always fills in (enumeration + wiring); the resolved half
    runs ``fusesoc run --setup`` and lands under ``"resolved"``, or —
    resolution being genuinely unavailable outside the Session Runtime — its
    failure lands under ``"resolved_error"`` instead of raising. Unknown and
    ambiguous *token*\\ s DO raise (:class:`fusesoc_registry.UnknownTargetError`
    / :class:`fusesoc_registry.AmbiguousTargetError`) — their messages already
    name the candidates. *resolve_kwargs* forward to
    :func:`fusesoc_registry.resolve_target` (e.g. ``runner`` for tests).
    """
    root = Path(project_root)
    ref = fusesoc_registry.resolve_ref(root, token)
    surface = collect_surface(root)
    entry = next(e for e in surface.entries() if e.ref == ref)

    payload: dict[str, Any] = {
        "name": ref.name,
        "selector": entry.selector,
        "vlnv": ref.vlnv,
        "core_file": _rel_to(ref.core_file, root),
        "flow": ref.flow,
        "eda_tool": ref.eda_tool,
        "cocotb_module": ref.cocotb_module,
        "toplevel": entry.toplevel or None,
        "wired_to": list(entry.wired_to),
        "drivable_by": list(entry.drivable_by),
        "warnings": list(surface.warnings),
    }
    if not resolve:
        return payload

    from booley.flows import edam as edam_layer

    build_root = edam_layer.work_root_for(root, "targets", ref.name)
    try:
        resolved = fusesoc_registry.resolve_target(
            token, project_root=root, build_root=build_root, **resolve_kwargs
        )
    except fusesoc_registry.FuseSocError as exc:
        payload["resolved_error"] = str(exc).strip().splitlines()[0]
    else:
        payload["resolved"] = {
            "toplevel": resolved.toplevel,
            "eda_tool": resolved.eda_tool,
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


# ---------------------------------------------------------------------------
# Terminal rendering — `booley targets` without --json
# ---------------------------------------------------------------------------

_WIRED_MARK = "←"  # ← reads "this Booley Flow's [flows.*].default_target points here"

# A declared toplevel is normally one module name; upstream cores occasionally
# declare CAPI2 conditional expressions ("tool_verilator? (wrapper)" …) that
# would stretch the column across the whole terminal. Cap the display — the
# detail view / --json carry the full string.
_TOP_DISPLAY_CHARS = 28


def _top_display(toplevel: str) -> str:
    top = toplevel or "-"
    if len(top) > _TOP_DISPLAY_CHARS:
        top = top[: _TOP_DISPLAY_CHARS - 1].rstrip() + "…"
    return f"top={top}"


def render_listing(surface: TargetSurface, project_root: Path | str) -> str:
    """The grouped-by-core terminal listing."""
    root = Path(project_root)
    lines: list[str] = []
    entries = list(surface.entries())
    if entries:
        name_w = max(len(e.selector) for e in entries)
        flow_w = max(len(e.ref.flow or "-") for e in entries)
        eda_tool_w = max(len(e.ref.eda_tool or "-") for e in entries)
        top_w = max(len(_top_display(e.toplevel)) for e in entries)
        for group in surface.groups:
            lines.append(f"{group.vlnv}  ({_rel_to(group.core_file, root)})")
            for e in group.entries:
                top = _top_display(e.toplevel)
                row = (
                    f"  {e.selector:<{name_w}}  {e.ref.flow or '-':<{flow_w}}  "
                    f"{e.ref.eda_tool or '-':<{eda_tool_w}}  {top:<{top_w}}"
                )
                if e.ref.cocotb_module:
                    row += f"  cocotb={e.ref.cocotb_module}"
                if e.wired_to:
                    row += f"  {_WIRED_MARK} {', '.join(e.wired_to)}"
                lines.append(row.rstrip())
            lines.append("")
        lines.append(
            f"{_WIRED_MARK} = wired via [flows.<flow>].default_target · "
            "`booley targets <name>` resolves parameters/files/SDC/XDC"
        )
    else:
        lines.append("(no Targets match)")
    for warning in surface.warnings:
        lines.append(f"WARNING: {warning}")
    return "\n".join(lines)


def render_detail(payload: dict[str, Any]) -> str:
    """The single-Target detail view."""

    def row(label: str, value: str) -> str:
        return f"  {label:<13} {value}"

    lines = [f"Target {payload['name']}  (core {payload['vlnv']}, {payload['core_file']})"]
    lines.append(row("selector", payload["selector"]))
    lines.append(row("flow", payload["flow"] or "-"))
    lines.append(row("EDA tool", payload["eda_tool"] or "-"))
    if payload["cocotb_module"]:
        lines.append(row("cocotb", payload["cocotb_module"]))
    lines.append(row("toplevel", (payload["toplevel"] or "-") + "  (declared in .core)"))
    lines.append(
        row("wired to", ", ".join(payload["wired_to"]) or "(no [flows.*].default_target names it)")
    )
    lines.append(row("drivable by", ", ".join(payload["drivable_by"]) or "-"))

    resolved = payload.get("resolved")
    if resolved is not None:
        lines.append("")
        lines.append("Resolved via `fusesoc run --setup`:")
        lines.append(row("toplevel", resolved["toplevel"] or "-"))
        lines.append(row("EDA tool", resolved["eda_tool"] or "-"))
        params = resolved["parameters"]
        lines.append(row("parameters", _render_parameters(params)))
        lines.append(
            row(
                "rtl",
                f"{resolved['rtl_hdl_sources']} (System)Verilog sources, "
                f"{resolved['rtl_include_dirs']} include dirs",
            )
        )
        lines.append(row("tb files", str(resolved["tb_files"])))
        lines.append(row("SDC", ", ".join(resolved["sdc_files"]) or "(none)"))
        lines.append(row("XDC", ", ".join(resolved["xdc_files"]) or "(none)"))
        lines.append(row("build dir", resolved["build_root"]))
    elif payload.get("resolved_error"):
        lines.append("")
        lines.append(f"Resolved view unavailable: {payload['resolved_error']}")
        lines.append(
            "  (resolution needs `fusesoc` — run inside the Session Runtime, "
            f"e.g. `booley session enter -- booley targets {payload['selector']}`)"
        )

    for warning in payload.get("warnings", []):
        lines.append(f"WARNING: {warning}")
    return "\n".join(lines)


def _render_parameters(params: Mapping[str, Any]) -> str:
    """One-line EDAM ``parameters`` summary: ``NAME (datatype) = default``."""
    if not params:
        return "(none)"
    parts = []
    for name, spec in params.items():
        if isinstance(spec, Mapping):
            datatype = spec.get("datatype")
            default = spec.get("default")
            piece = name + (f" ({datatype})" if datatype else "")
            if default is not None:
                piece += f" = {default}"
            parts.append(piece)
        else:
            parts.append(f"{name} = {spec}")
    return ", ".join(parts)

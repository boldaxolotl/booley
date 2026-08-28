"""fusesoc_trace_overlay.py — how to build a FuseSoC ``--trace`` overlay core (ADR 0022).

Extracted from :mod:`booley.fusesoc.fusesoc_registry` (principle 8 / SRP). That module's
job is design-description *discovery and resolution* — list Target names from data
you trust, resolve through the ``fusesoc`` program. This module carries a distinct,
orthogonal responsibility: **synthesising a generated, agent-immutable-safe
``--trace`` build overlay** for a sim Target. Its one reason to change is the
mechanics of how a trace overlay ``.core`` is constructed (Verilator recipe resolution,
per-simulator dump-root rooting, the overlay VLNV/marker convention, the on-disk
``.core`` emission) — none of which touches how base cores are enumerated or
resolved.

The two sides communicate through a handful of resolution primitives
(:func:`~booley.fusesoc.fusesoc_registry.enumerate_targets`, ``read_core``,
``available_targets``, ``core_target_flow``/``core_target_eda_tool``, the
``FuseSocError``/``UnknownTargetError`` types and the ``TRACE_OVERLAY_MARKER``
constant), which stay in ``fusesoc_registry`` and are imported **lazily** here to
avoid a module-load cycle with the backward-compat re-export ``fusesoc_registry``
keeps for its own consumers.

Consumers of the symbols moved here (via the ``fusesoc_registry`` re-export today):
``booley.doctor``, ``booley.simulate``, ``booley.coverage_analyst`` and their tests.
"""

from __future__ import annotations

import copy
import logging
from collections.abc import Mapping, MutableMapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from booley.fusesoc.core_projection import (
    native_cores_ignored,
    projected_core_path,
    projection_enabled,
)
from booley.sim.trace_recipe import (
    TraceMode,
    TraceRecipeError,
    require_cocotb_trace_mode,
    resolve_verilator_trace_mode,
)

logger = logging.getLogger(__name__)

# The fixed trace depth baked into every overlay. ``--trace-scope`` left the Booley Flow
# surface (2026-06-23 decision), so traces are full-hierarchy; the depth caps the
# dump at a generous level rather than the unbounded default.
DEFAULT_TRACE_DEPTH = 99

# VLNV ``name``-component suffix the overlay carries so it never collides with the
# base core (verified: FuseSoC scopes Targets per-VLNV, so the overlay can reuse
# the base Target *name* under this distinct VLNV and resolve unambiguously).
_TRACE_OVERLAY_VLNV_SUFFIX = "-booleytrace"

# The Booley sim-harness dump module (``sim/booley_vcd_dump.sv``): an
# uninstantiated convention module whose ``$dumpvars(0)`` fires on ``+trace``.
# Verilator auto-roots every uninstantiated module so it runs for free, but
# Edalize's Icarus flow elaborates only ``-s <toplevel>``, which prunes it — so
# the Icarus overlay roots it explicitly.
_VCD_DUMP_MODULE = "booley_vcd_dump"

# Overlay-only fileset name that carries an overlay-*supplied* dump module. Kept
# distinct from any authored fileset so appending it to the Target's fileset list
# never clobbers a design fileset of the same name.
_INJECTED_DUMP_FILESET = "booley_trace_dump"


def _inject_dump_module(
    overlay_doc: MutableMapping[str, Any],
    target: str,
    base_core_file: Path,
    marker: str,
    project_root: Path | str,
) -> Path | None:
    """Supply the dump module from Booley's ``refs/`` so the design repo needn't.

    Normally the overlay copies the source beside its ephemeral ``.core`` so
    FuseSoC stages it into the build before :meth:`TraceOverlay.cleanup` removes
    the copy. The design's tracked files remain untouched (ADR 0017/0018).

    Native-core isolation converts fileset paths to absolute paths and therefore
    does not stage the copy. That branch references the persistent packaged source
    directly so cleanup cannot delete a later Makefile dependency. Returns the
    ephemeral copy to clean, or ``None`` for the packaged-source branch.
    """
    from booley.runtime.paths import refs_dir

    src = refs_dir() / f"{_VCD_DUMP_MODULE}.sv"
    try:
        content = src.read_text(encoding="utf-8")
    except OSError as exc:
        from booley.fusesoc.fusesoc_registry import FuseSocError

        raise FuseSocError(f"could not read packaged trace dump module {src}: {exc}") from exc

    injected: Path | None = None
    declared_path = str(src.resolve())
    if not native_cores_ignored(project_root):
        injected = base_core_file.with_name(f"{_VCD_DUMP_MODULE}{marker}.sv")
        try:
            injected.write_text(content, encoding="utf-8")
        except OSError as exc:
            from booley.fusesoc.fusesoc_registry import FuseSocError

            raise FuseSocError(f"could not supply trace dump module {injected}: {exc}") from exc
        declared_path = injected.name
    filesets = overlay_doc.setdefault("filesets", {})
    filesets[_INJECTED_DUMP_FILESET] = {
        "files": [{declared_path: {"file_type": "systemVerilogSource"}}],
        "tags": ["tb"],
    }
    overlay_doc["targets"][target].setdefault("filesets", []).append(_INJECTED_DUMP_FILESET)
    return injected


def trace_overlay_vlnv(base_vlnv: str) -> str:
    """Derive the overlay VLNV for *base_vlnv* (pure — no I/O).

    Appends :data:`_TRACE_OVERLAY_VLNV_SUFFIX` to the VLNV's ``name`` component
    (``vendor:library:name:version`` — version is the last field, name the one
    before it), e.g. ``::design:0`` → ``::design-booleytrace:0``. A bare
    single-field VLNV is suffixed whole.
    """
    parts = base_vlnv.split(":")
    name_idx = len(parts) - 2 if len(parts) >= 2 else 0
    parts[name_idx] = f"{parts[name_idx]}{_TRACE_OVERLAY_VLNV_SUFFIX}"
    return ":".join(parts)


def _with_trace_options(
    verilator_options: Sequence[Any],
    trace_depth: int,
) -> list[str]:
    """Return *verilator_options* with a single canonical ``--trace``/``--trace-depth``.

    Used only when the Target has no authored trace format, so the overlay's
    fixed VCD pair can be appended without rewriting project-owned options.
    """
    out: list[str] = []
    skip_value = False
    for raw_opt in verilator_options:
        opt = str(raw_opt)
        if skip_value:  # consume the value that followed a dropped --trace-depth
            skip_value = False
            continue
        if opt == "--trace-depth":
            skip_value = True
            continue
        out.append(opt)
    out += ["--trace", "--trace-depth", str(trace_depth)]
    return out


def validate_cocotb_trace_mode(target: str, mode: TraceMode) -> None:
    """Raise a registry-shaped error when Cocotb cannot honor *mode*."""
    from booley.fusesoc.fusesoc_registry import FuseSocError

    try:
        require_cocotb_trace_mode(target, mode)
    except TraceRecipeError as exc:
        raise FuseSocError(str(exc)) from exc


def _with_icarus_dump_root(iverilog_options: Sequence[Any]) -> list[str]:
    """Return *iverilog_options* with a single ``-s<dump-module>`` extra root.

    Edalize's Icarus flow elaborates only ``-s <toplevel>`` (overriding iverilog's
    auto-rooting), which prunes the uninstantiated :data:`_VCD_DUMP_MODULE`. The
    overlay appends an explicit extra root so its ``+trace`` ``$dumpvars`` fires —
    the edalize analog of the legacy runner's second ``-s _vcd_dump``. Idempotent:
    a base Target that already names the root does not double up.
    """
    flag = f"-s{_VCD_DUMP_MODULE}"
    out = [str(o) for o in iverilog_options if str(o) != flag]
    out.append(flag)
    return out


def _target_dump_module_entry(
    core_doc: Mapping[str, Any],
    target: str,
) -> str | None:
    """The ``booley_vcd_dump.sv`` fileset path *target* carries, or ``None``.

    Reads the ``.core`` doc directly (pre-resolve): a file entry is either a bare
    path string or a single-key ``{path: {attrs}}`` mapping. Only filesets the
    Target actually pulls in are considered. The returned path is as authored —
    relative to the declaring ``.core``'s directory.
    """
    from booley.fusesoc.fusesoc_registry import target_fileset_names

    targets = core_doc.get("targets", {})
    target_def = targets.get(target, {}) if isinstance(targets, Mapping) else {}
    used = target_fileset_names(target_def)
    filesets = core_doc.get("filesets", {}) or {}
    want = f"{_VCD_DUMP_MODULE}.sv"
    for fs_name in used:
        fs = filesets.get(fs_name, {}) if isinstance(filesets, Mapping) else {}
        for entry in fs.get("files", []) or []:
            path = entry if isinstance(entry, str) else next(iter(entry), "")
            if Path(str(path)).name == want:
                return str(path)
    return None


def _target_includes_dump_module(core_doc: Mapping[str, Any], target: str) -> bool:
    """True when *target*'s filesets carry the ``booley_vcd_dump.sv`` module."""
    return _target_dump_module_entry(core_doc, target) is not None


def target_includes_dump_module(project_root: Path | str, target: str) -> bool:
    """True when *target* carries the ``booley_vcd_dump.sv`` trace module.

    Public, subprocess-free wrapper for setup-time checks (``booley doctor``):
    without the module, ``simulate --trace`` has nothing to root and produces no
    waveform — a failure that otherwise only surfaces on the first trace run.
    """
    from booley.fusesoc.fusesoc_registry import FuseSocError, read_core, resolve_ref

    try:
        ref = resolve_ref(project_root, target)
    except FuseSocError:  # unknown / ambiguous → nothing to root
        return False
    return _target_includes_dump_module(read_core(ref.core_file), ref.name)


@dataclass(frozen=True)
class TraceOverlay:
    """Handle to a written trace-overlay ``.core`` and the VLNV it declares.

    The caller resolves ``(target, vlnv)`` then calls :meth:`cleanup` (the overlay
    is needed only until ``fusesoc run --setup`` copies its filesets into the build
    root; the resolved build dir is self-contained thereafter).
    """

    core_file: Path
    """Absolute path to the written overlay ``.core`` (carries the marker)."""

    vlnv: str
    """The overlay's derived VLNV — what :func:`resolve_target` is handed."""

    mode: TraceMode = TraceMode.VCD_FIFO
    """The coherent build/run recipe selected for this overlay."""

    extra_files: tuple[Path, ...] = ()
    """Overlay-*supplied* source files (e.g. an injected dump module) to remove
    alongside the ``.core`` — empty when the design's own fileset carried them."""

    def cleanup(self) -> None:
        """Remove the overlay ``.core`` and any supplied files (idempotent)."""
        for path in (self.core_file, *self.extra_files):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                logger.debug("trace overlay cleanup failed for %s: %s", path, exc)


def _inject_runtime_dump_trace(
    overlay_doc: dict,
    doc: dict,
    target: str,
    ref: Any,
    flow_options: dict,
    project_root: Path | str,
) -> Path | None:
    """Wire the runtime ``+trace`` dump module for Icarus.

    Verilator traces at compile time via ``verilator_options`` (handled by the
    caller); Icarus needs an explicitly rooted ``booley_vcd_dump``
    module so its runtime ``$dumpvars`` fires. Returns the injected dump-module
    path when the overlay supplied one (``None`` when the design's own tb
    fileset already carries it).
    """
    from booley.fusesoc.fusesoc_registry import TRACE_OVERLAY_MARKER

    # If the design's own tb fileset carries the module, root that. Otherwise
    # the overlay *supplies* it from
    # Booley's refs/ so the design repo needs no tracked booley_vcd_dump —
    # keeping the trace machinery out of the user's git history (Stealth
    # Mode, ADR 0017/0018).
    injected: Path | None = None
    if _target_dump_module_entry(doc, target) is None:
        injected = _inject_dump_module(
            overlay_doc,
            target,
            ref.core_file,
            TRACE_OVERLAY_MARKER,
            project_root,
        )
    flow_options["iverilog_options"] = _with_icarus_dump_root(
        flow_options.get("iverilog_options") or [],
    )
    return injected


def _write_overlay_core_file(overlay_path: Path, overlay_doc: dict) -> None:
    """Serialize *overlay_doc* to *overlay_path* as a CAPI2 ``.core`` file."""
    from booley.fusesoc.fusesoc_registry import FuseSocError

    # A ``.core`` is YAML with a leading ``CAPI=2:`` marker line; safe_load parses
    # that marker into a harmless ``CAPI=2`` key on read (read_core), so re-emit it
    # explicitly and drop the round-tripped key from the body.
    body = {k: v for k, v in overlay_doc.items() if k != "CAPI=2"}
    try:
        with overlay_path.open("w", encoding="utf-8") as f:
            f.write("CAPI=2:\n")
            yaml.safe_dump(body, f, sort_keys=False)
    except OSError as exc:
        raise FuseSocError(f"could not write trace overlay {overlay_path}: {exc}") from exc


def write_trace_overlay(
    target: str,
    *,
    project_root: Path | str,
    trace_depth: int = DEFAULT_TRACE_DEPTH,
) -> TraceOverlay:
    """Write a co-located ``--trace`` overlay ``.core`` for a supported sim Target.

    Booley never edits a committed Target — it stays agent-immutable. To trace,
    this generates an overlay: a deep copy of the base Target's declaring core
    under a derived VLNV (:func:`trace_overlay_vlnv`). For Verilator, an authored
    VCD or FST format and all of its options are preserved. A Target with no
    authored format receives Booley's canonical ``--trace``/``--trace-depth``
    VCD options (:func:`_with_trace_options`).

    A copy — **not** a CAPI2 ``depend`` — because a dependency resolves through the
    dependency's *default* Target, which omits the testbench filesets a sim Target
    adds (verified: the TB is lost). The overlay is written **beside** the base
    ``.core`` so its (unchanged) relative fileset paths resolve identically; its
    distinct VLNV gives it its own FuseSoC build root, so trace caching is keyed by
    the overlay Target with no Booley-owned ``(target, trace)`` cache. The filename
    carries :data:`TRACE_OVERLAY_MARKER` so :func:`discover_cores` skips it.

    EDA-tool-aware injection (every supported sim simulator traces via an overlay):

    * **Verilator** — preserve an authored trace recipe or inject the canonical
      VCD recipe. The returned :class:`TraceOverlay` identifies whether the
      run-half should read native FST directly or stream VCD through B-Wave.
    For Icarus the ``booley_vcd_dump`` module must be a compiled
    source to root. If the design's own tb fileset carries it that source is
    used; otherwise the overlay **supplies** it from Booley's ``refs/``
    (:func:`_inject_dump_module`) so the design repo needs no tracked
    ``booley_vcd_dump`` — the trace machinery stays out of the user's git history
    (Stealth Mode, ADR 0017/0018). Verilator needs no such module (its C++
    ``--exe`` harness owns the VCD lifecycle).

    * **Icarus** — inject an extra ``-s<dump-module>`` ``iverilog_options`` root so
      the uninstantiated ``booley_vcd_dump`` (pruned by edalize's ``-s
      <toplevel>``) is elaborated and its runtime ``+trace`` ``$dumpvars`` fires.
    Raises :class:`UnknownTargetError` / :class:`AmbiguousTargetError` if *target*
    is not a single selectable Target (ADR 0030);
    :class:`FuseSocError` if it is not a Verilator/Icarus sim Target, or if the
    supplied dump module cannot be written.
    """
    from booley.fusesoc.fusesoc_registry import (
        TRACE_OVERLAY_MARKER,
        FuseSocError,
        core_target_eda_tool,
        core_target_flow,
        read_core,
        resolve_ref,
    )

    # Raises UnknownTargetError / AmbiguousTargetError for a token that is not a
    # single selectable Target (ADR 0030). Rebind to the bare name so every
    # downstream `.core` target-key lookup below is correct even for a vlnv#name.
    ref = resolve_ref(project_root, target)
    target = ref.name
    doc = read_core(ref.core_file)
    flow = core_target_flow(doc, target)
    eda_tool = core_target_eda_tool(doc, target)
    if flow != "sim" or eda_tool not in ("verilator", "icarus"):
        raise FuseSocError(
            f"trace overlay unsupported for Target {target!r} "
            f"(flow={flow!r}, EDA tool={eda_tool!r}); only Verilator/Icarus "
            f"sim Targets trace via an overlay .core."
        )

    overlay_doc = copy.deepcopy(doc)
    overlay_vlnv = trace_overlay_vlnv(ref.vlnv)
    overlay_doc["name"] = overlay_vlnv
    target_def = overlay_doc["targets"][target]
    flow_options = target_def.setdefault("flow_options", {})
    injected: Path | None = None  # set when the overlay supplies the dump module
    if eda_tool == "verilator":
        authored_options = flow_options.get("verilator_options") or []
        try:
            mode = resolve_verilator_trace_mode(authored_options)
        except TraceRecipeError as exc:
            raise FuseSocError(str(exc)) from exc
        if mode is None:
            mode = TraceMode.VCD_FIFO
            flow_options["verilator_options"] = _with_trace_options(
                authored_options,
                trace_depth,
            )
        else:
            flow_options["verilator_options"] = [str(option) for option in authored_options]
    else:  # Icarus — runtime +trace via an explicitly rooted dump module
        mode = TraceMode.VCD_FIFO
        injected = _inject_runtime_dump_trace(
            overlay_doc,
            doc,
            target,
            ref,
            flow_options,
            project_root,
        )

    projected_overlay: Path | None = None
    if projection_enabled(project_root) and injected is not None:
        entry = overlay_doc["filesets"][_INJECTED_DUMP_FILESET]["files"][0]
        attrs = next(iter(entry.values()))
        relative = injected.relative_to(Path(project_root)).as_posix()
        overlay_doc["filesets"][_INJECTED_DUMP_FILESET]["files"] = [{relative: attrs}]

    overlay_path = ref.core_file.with_name(
        f"{ref.core_file.stem}{TRACE_OVERLAY_MARKER}{ref.core_file.suffix}"
    )
    _write_overlay_core_file(overlay_path, overlay_doc)
    if projection_enabled(project_root):
        projected_overlay = projected_core_path(project_root, overlay_path)
    return TraceOverlay(
        core_file=overlay_path,
        vlnv=overlay_vlnv,
        mode=mode,
        extra_files=tuple(path for path in (injected, projected_overlay) if path is not None),
    )

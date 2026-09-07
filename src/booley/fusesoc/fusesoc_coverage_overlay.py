"""Ephemeral FuseSoC build overlays for Verilator-native coverage."""

from __future__ import annotations

import copy
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from booley.flows.sim.trace_recipe import TraceMode, TraceRecipeError, resolve_verilator_trace_mode
from booley.fusesoc.constants import TRACE_OVERLAY_MARKER
from booley.targets.domain import TargetHandle

from .fusesoc_trace_overlay import (
    DEFAULT_TRACE_DEPTH,
    _with_trace_options,
    _write_overlay_core_file,
)

logger = logging.getLogger(__name__)

_COVERAGE_VLNV_SUFFIX = "-booleycoverage"
_TRACE_COVERAGE_VLNV_SUFFIX = "-booleytracecoverage"
_BRIDGE_FILESET = "booley_verilator_coverage_bridge"


def coverage_overlay_vlnv(base_vlnv: str, *, trace: bool) -> str:
    """Derive the coverage build VLNV without changing the authored core."""
    parts = base_vlnv.split(":")
    name_index = len(parts) - 2 if len(parts) >= 2 else 0
    suffix = _TRACE_COVERAGE_VLNV_SUFFIX if trace else _COVERAGE_VLNV_SUFFIX
    parts[name_index] = f"{parts[name_index]}{suffix}"
    return ":".join(parts)


def _with_coverage_options(
    verilator_options: Sequence[object], instrumentation: Sequence[str]
) -> list[str]:
    """Append one canonical copy of every collector-owned coverage switch."""
    owned = {"--coverage", *instrumentation}
    return [str(option) for option in verilator_options if str(option) not in owned] + list(
        instrumentation
    )


def _custom_main_hooks(flow_options: dict) -> tuple[str, ...]:
    booley = flow_options.get("booley")
    coverage = booley.get("coverage") if isinstance(booley, dict) else None
    hooks = coverage.get("custom_main_hooks") if isinstance(coverage, dict) else None
    return tuple(str(hook) for hook in hooks) if isinstance(hooks, list) else ()


def _inject_custom_main_bridge(document: dict, target: str) -> None:
    """Compile Booley's hook implementation into an opted-in custom main."""
    from booley.fusesoc.fusesoc_registry import FuseSocError
    from booley.runtime.paths import refs_dir

    filesets = document.setdefault("filesets", {})
    if _BRIDGE_FILESET in filesets:
        raise FuseSocError(
            f"Target {target!r} already defines reserved fileset {_BRIDGE_FILESET!r}"
        )
    root = refs_dir()
    source = (root / "booley_coverage.cpp").resolve()
    header = (root / "booley_coverage.h").resolve()
    if not source.is_file() or not header.is_file():
        raise FuseSocError("packaged custom-main coverage bridge is missing")
    filesets[_BRIDGE_FILESET] = {
        "files": [
            {str(header): {"file_type": "cppSource", "is_include_file": True}},
            {str(source): {"file_type": "cppSource"}},
        ]
    }
    document["targets"][target].setdefault("filesets", []).append(_BRIDGE_FILESET)


@dataclass(frozen=True)
class CoverageOverlay:
    """One temporary coverage core and the trace recipe its run half needs."""

    core_file: Path
    vlnv: str
    trace_mode: TraceMode

    def cleanup(self) -> None:
        """Remove the generated core without touching the authored Target."""
        try:
            self.core_file.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.debug("coverage overlay cleanup failed for %s: %s", self.core_file, exc)


def write_coverage_overlay(
    handle: TargetHandle,
    *,
    instrumentation: Sequence[str],
    trace: bool,
    trace_depth: int = DEFAULT_TRACE_DEPTH,
) -> CoverageOverlay:
    """Write an isolated Verilator coverage or trace-plus-coverage Target."""
    from booley.fusesoc.fusesoc_registry import (
        FuseSocError,
        read_core,
        require_current_target_handle,
    )

    require_current_target_handle(handle)
    if handle.flow != "sim" or handle.eda_tool != "verilator":
        raise FuseSocError(
            f"coverage overlay unsupported for Target {handle.name!r} "
            f"(flow={handle.flow!r}, EDA tool={handle.eda_tool!r}); only Verilator "
            "sim Targets support native coverage."
        )

    document = copy.deepcopy(read_core(handle.core_file))
    overlay_vlnv = coverage_overlay_vlnv(handle.vlnv, trace=trace)
    document["name"] = overlay_vlnv
    flow_options = document["targets"][handle.name].setdefault("flow_options", {})
    authored_options = flow_options.get("verilator_options") or []
    try:
        mode = resolve_verilator_trace_mode(authored_options)
    except TraceRecipeError as exc:
        raise FuseSocError(str(exc)) from exc
    if mode is None:
        mode = TraceMode.VCD_FIFO
        if trace:
            authored_options = _with_trace_options(authored_options, trace_depth)
    flow_options["verilator_options"] = _with_coverage_options(
        authored_options,
        instrumentation,
    )
    if _custom_main_hooks(flow_options):
        _inject_custom_main_bridge(document, handle.name)

    kind = "trace-coverage" if trace else "coverage"
    overlay_path = handle.core_file.with_name(
        f"{handle.core_file.stem}{TRACE_OVERLAY_MARKER}.{kind}{handle.core_file.suffix}"
    )
    _write_overlay_core_file(overlay_path, document)
    return CoverageOverlay(overlay_path, overlay_vlnv, mode)


__all__ = ["CoverageOverlay", "coverage_overlay_vlnv", "write_coverage_overlay"]

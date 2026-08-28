"""Closed trace recipes shared by Simulation orchestration and run-halves."""

from __future__ import annotations

import re
from collections.abc import Sequence
from enum import StrEnum


class TraceMode(StrEnum):
    """One coherent build/run recipe for a traced simulation."""

    VCD_FIFO = "vcd_fifo"
    NATIVE_FST = "native_fst"


class TraceRecipeError(ValueError):
    """An authored Target cannot resolve to one coherent trace recipe."""


_TRACE_FORMAT_FLAGS = frozenset({"--trace", "--trace-vcd", "--trace-fst", "--trace-saif"})
_TRACE_FORMAT_DEFINE_RE = re.compile(r"(?:^|\s)-DVM_TRACE_FMT_(FST|VCD)(?:=[^\s]+)?(?=\s|$)")


def _trace_format_defines(verilator_options: Sequence[object]) -> set[str]:
    """Return format-selecting harness CFLAGS declared in Verilator options."""
    return {
        match.group(1)
        for option in verilator_options
        for match in _TRACE_FORMAT_DEFINE_RE.finditer(str(option))
    }


def resolve_verilator_trace_mode(verilator_options: Sequence[object]) -> TraceMode | None:
    """Resolve an authored Verilator build contract, or ``None`` when absent.

    Verilator trace flags select generated runtime objects while project CFLAGS
    often select the matching C++ harness class. Both halves must agree before
    the overlay can preserve the recipe or inject its default VCD recipe.
    """
    options = {str(option) for option in verilator_options}
    defines = _trace_format_defines(verilator_options)
    if "--trace-saif" in options:
        raise TraceRecipeError(
            "SAIF tracing is not supported by Booley's waveform pipeline; "
            "use an authored --trace-fst recipe or VCD tracing"
        )
    if "--trace-fst" in options and "--trace-vcd" in options:
        raise TraceRecipeError(
            "trace Target requests both native FST and VCD; remove one explicit "
            "format flag so Booley can resolve a coherent trace recipe"
        )
    if defines == {"FST", "VCD"}:
        raise TraceRecipeError(
            "trace Target defines both FST and VCD CFLAGS; keep one "
            "VM_TRACE_FMT_* harness selection"
        )
    if "FST" in defines and "--trace-fst" not in options:
        if options & {"--trace", "--trace-vcd"}:
            raise TraceRecipeError(
                "FST CFLAG VM_TRACE_FMT_FST conflicts with a VCD trace option; "
                "use --trace-fst or select the VCD harness"
            )
        raise TraceRecipeError(
            "VM_TRACE_FMT_FST requires --trace-fst so generated trace objects "
            "match the authored C++ harness"
        )
    if "VCD" in defines and "--trace-fst" in options:
        raise TraceRecipeError(
            "VCD CFLAG VM_TRACE_FMT_VCD conflicts with the native FST trace option; "
            "use a VCD trace flag or select the FST harness"
        )
    if "--trace-fst" in options:
        return TraceMode.NATIVE_FST
    if options & _TRACE_FORMAT_FLAGS:
        return TraceMode.VCD_FIFO
    return None


def require_cocotb_trace_mode(target: str, mode: TraceMode) -> None:
    """Reject trace recipes the Cocotb run-half cannot produce coherently."""
    if mode is TraceMode.NATIVE_FST:
        raise TraceRecipeError(
            f"Cocotb Target {target!r} requests native FST tracing, but "
            "the Cocotb run-half currently owns a VCD dump; use a VCD "
            "trace recipe for this Target"
        )

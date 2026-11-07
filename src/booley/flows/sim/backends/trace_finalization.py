"""Trace finalization shared by the native adapters: TRACE_OK, TRACE_FALLBACK or an incident."""

from __future__ import annotations

import subprocess
from pathlib import Path

from booley.bwave.waveform_store import StreamingConversion
from booley.flows.sim.adapter_transport import AdapterTraceResult
from booley.flows.sim.backends.shared import CurrentRunTraces, ambiguous_trace_reason


def _missing_trace_reason(current: CurrentRunTraces) -> str:
    """Explain why no current trace artifact could be retained."""
    reason = "trace requested but no fresh .fst store or convertible .vcd was produced"
    if current.patterns:
        return reason + (
            " (no fresh, nonempty file matching [flows.sim].trace_files "
            f"{list(current.patterns)} was produced by the current run)"
        )
    if current.roots:
        return reason + (
            " (a testbench writing its dump under its own name needs "
            "[flows.sim].trace_files to declare it)"
        )
    return reason


def finalize_trace(
    current: CurrentRunTraces,
    sim_proc: subprocess.Popen[str] | None,
    *,
    conversion: StreamingConversion | None = None,
    raw_vcd: Path | None = None,
    vcd_fallback: bool = False,
) -> tuple[str, AdapterTraceResult]:
    """Postprocess *raw_vcd*, select this run's waveform, and report its fate.

    *vcd_fallback* lets a raw VCD that cannot be converted stand as the retained
    waveform (``TRACE_FALLBACK``) unless it was over the conversion cap.
    """
    session = current.session
    if raw_vcd is not None:
        session.postprocess(raw_vcd)
    selection = current.select()
    found = selection.path
    inspection = session.inspect(found) if found is not None else None
    artifact = inspection.artifact if inspection is not None else None
    if artifact is not None:
        output = f"TRACE_OK: {artifact.path}\n{artifact.metadata_line()}"
        print(output)
        evidence = AdapterTraceResult(
            "ok",
            path=str(artifact.path),
            top_scope=artifact.top_scope,
            signal_count=artifact.signal_count,
            total_ticks=artifact.total_ticks,
        )
        return f"\n{output}", evidence
    if selection.ambiguous:
        reason = ambiguous_trace_reason(selection.ambiguous)
    elif found is None or inspection is None:
        reason = _missing_trace_reason(current)
    elif vcd_fallback and found.suffix.lower() == ".vcd" and not selection.oversize:
        print(f"TRACE_FALLBACK: {found}")
        detail = f"native FST conversion unavailable: {inspection.failure_reason}"
        return f"\nTRACE_FALLBACK: {found}", AdapterTraceResult(
            "ok", path=str(found), detail=detail
        )
    else:
        reason = f"trace requested but the retained waveform is not queryable: {inspection.failure_reason}"
    incident = session.write_incident(reason, sim_proc=sim_proc, bwave_proc=conversion)
    print(f"ERROR: {reason}")
    print(f"TRACE_INCIDENT: {incident}")
    return (
        f"\nERROR: {reason}\nTRACE_INCIDENT: {incident}",
        AdapterTraceResult("incident", path=str(incident), detail=reason),
    )

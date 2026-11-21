"""Trace finalization shared by the native adapters: TRACE_OK or an incident."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

from booley.bwave.waveform_store import StreamingConversion
from booley.flows.sim.adapter_transport import (
    AdapterTraceResult,
    AdapterTransportIdentity,
    partial_result_identity,
    publish_native_adapter_result,
)
from booley.flows.sim.backends.shared import (
    CurrentRunTraces,
    RunTermination,
    TraceSelection,
    ambiguous_trace_reason,
)
from booley.flows.sim.trace_session import TraceInspection


def _missing_trace_reason(current: CurrentRunTraces) -> str:
    """Explain why no current trace artifact could be retained."""
    reason = "trace requested but no fresh .fst store or convertible .vcd was produced"
    if current.patterns:
        reason += (
            " (no fresh, nonempty file matching [flows.sim].trace_files "
            f"{list(current.patterns)} was produced by the current run)"
        )
        return "; ".join((reason, *current.stream_failures))
    if current.roots:
        return reason + (
            " (a testbench writing its dump under its own name needs "
            "[flows.sim].trace_files to declare it)"
        )
    return reason


_PENDING_DETAIL = "trace conversion had not finished when the adapter stopped"


def native_pending_publisher(
    identity: AdapterTransportIdentity | None,
    output: str,
    returncode: int,
    *,
    pass_sentinels: list[str] | None,
    fail_sentinels: list[str] | None,
    termination: RunTermination,
) -> Callable[[AdapterTraceResult], None] | None:
    """Build the pending-partial writer of a native adapter, or ``None`` without transport.

    It runs the terminal verdict function on the run's real *termination*, so
    the partial holds the simulation verdict plus a trace incident.
    """
    if identity is None:
        return None
    partial = partial_result_identity(identity)

    def publish(pending: AdapterTraceResult) -> None:
        publish_native_adapter_result(
            partial,
            output,
            returncode,
            pass_sentinels=pass_sentinels,
            fail_sentinels=fail_sentinels,
            trace_required=True,
            trace=pending,
            termination=termination,
        )

    return publish


def _select_and_inspect(
    current: CurrentRunTraces, raw_vcd: Path | None
) -> tuple[TraceSelection, TraceInspection | None]:
    """Convert, select and inspect the waveform; an interrupt still releases held diagnostics."""
    session = current.session
    try:
        current.finish_streams()
        if raw_vcd is not None:
            session.postprocess(raw_vcd)
        selection = current.select()
        found = selection.path
        return selection, session.inspect(found) if found is not None else None
    except BaseException:
        session.report_failed_conversions(declared_trace_used=False)
        raise


def finalize_trace(
    current: CurrentRunTraces,
    sim_proc: subprocess.Popen[str] | None,
    *,
    convert_timeout_s: float,
    publish_pending: Callable[[AdapterTraceResult], None] | None = None,
    conversion: StreamingConversion | None = None,
    raw_vcd: Path | None = None,
) -> tuple[str, AdapterTraceResult]:
    """Postprocess *raw_vcd*, select this run's waveform, and report its fate.

    The simulation verdict is fixed before conversion starts: *publish_pending*
    receives a trace incident first, so an adapter killed or crashing during
    conversion still leaves the verdict (plus that incident) behind. One
    *convert_timeout_s* budget then bounds every conversion of the attempt. A
    raw VCD is never a waveform: failing to convert it is a trace incident.
    """
    session = current.session
    session.start_conversion_budget(convert_timeout_s)
    if publish_pending is not None:
        publish_pending(AdapterTraceResult("incident", detail=_PENDING_DETAIL))
    selection, inspection = _select_and_inspect(current, raw_vcd)
    artifact = inspection.artifact if inspection is not None else None
    session.report_failed_conversions(
        declared_trace_used=artifact is not None and selection.declared
    )
    if artifact is not None:
        output = f"TRACE_OK: {artifact.path}\n{artifact.metadata_line()}"
        print(output)
        return f"\n{output}", AdapterTraceResult(
            "ok",
            path=str(artifact.path),
            top_scope=artifact.top_scope,
            signal_count=artifact.signal_count,
            total_ticks=artifact.total_ticks,
            warnings=artifact.warnings,
        )
    if selection.ambiguous:
        reason = ambiguous_trace_reason(selection.ambiguous)
    elif selection.path is None or inspection is None:
        reason = _missing_trace_reason(current)
    else:
        reason = f"trace requested but the retained waveform is not queryable: {inspection.failure_reason}"
    incident = session.write_incident(reason, sim_proc=sim_proc, bwave_proc=conversion)
    print(f"ERROR: {reason}")
    print(f"TRACE_INCIDENT: {incident}")
    return (
        f"\nERROR: {reason}\nTRACE_INCIDENT: {incident}",
        AdapterTraceResult("incident", path=str(incident), detail=reason),
    )

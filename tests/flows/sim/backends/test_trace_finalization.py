"""Shared trace finalization: what a finished run's waveform is worth to every native adapter."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.flows.sim.backends.shared import CurrentRunTraces
from booley.flows.sim.backends.trace_finalization import finalize_trace
from booley.flows.sim.trace_session import TraceArtifact, TraceInspection

OPENING = "trace requested but no fresh .fst store or convertible .vcd was produced"
KNOB_HINT = " (a testbench writing its dump under its own name needs [flows.sim].trace_files to declare it)"


class _FakeTrace:
    """Trace seam stub: the store exists only once a VCD was converted (or is already owned)."""

    owned_outputs: frozenset[Path] = frozenset()

    def __init__(self, store: Path | None = None, failure: str = "", owned: bool = False):
        self.store, self.failure, self.owned = store, failure, owned
        self.events: list[str] = []
        self.incidents: list[tuple[str, object, object]] = []

    def start_conversion_budget(self, timeout_s: float) -> None:
        self.events.append(f"budget:{timeout_s}")

    def postprocess(self, _path: Path) -> None:
        self.events.append("postprocess")

    def find(self) -> Path | None:
        self.events.append("find")
        return self.store if self.owned or "postprocess" in self.events else None

    def write_incident(self, reason, *, sim_proc=None, bwave_proc=None) -> Path:
        self.incidents.append((reason, sim_proc, bwave_proc))
        return Path("trace_incident.txt")

    def inspect(self, path: Path) -> TraceInspection:
        if self.failure:
            return TraceInspection(None, self.failure)
        return TraceInspection(TraceArtifact(path, path.stat().st_size, "tb", 3, 7))


def _current(trace, patterns: tuple[str, ...] = (), roots: tuple[Path, ...] = ()):
    # An empty ``before`` snapshot makes every declared file count as freshly written.
    return CurrentRunTraces(trace, patterns, roots, {})


def test_ok_store_reports_metadata(tmp_path: Path):
    store = tmp_path / "trace.fst"
    store.write_bytes(b"fst")

    output, result = finalize_trace(
        _current(_FakeTrace(store, owned=True)), None, convert_timeout_s=60
    )

    metadata = TraceArtifact(store, 3, "tb", 3, 7).metadata_line()
    assert output == f"\nTRACE_OK: {store}\n{metadata}"
    assert (result.status, result.path) == ("ok", str(store))
    assert (result.top_scope, result.signal_count, result.total_ticks) == ("tb", 3, 7)


def test_unqueryable_store_is_an_incident(tmp_path: Path):
    store = tmp_path / "trace.fst"
    store.write_bytes(b"fst")
    trace = _FakeTrace(store, failure="FST has no signals", owned=True)
    sim_proc, conversion = SimpleNamespace(returncode=1), SimpleNamespace(name="bwave")

    output, result = finalize_trace(
        _current(trace), sim_proc, convert_timeout_s=60, conversion=conversion
    )

    reason = "trace requested but the retained waveform is not queryable: FST has no signals"
    assert trace.incidents == [(reason, sim_proc, conversion)]
    assert output == f"\nERROR: {reason}\nTRACE_INCIDENT: trace_incident.txt"
    assert (result.status, result.detail) == ("incident", reason)


@pytest.mark.parametrize(
    ("patterns", "roots", "hint"),
    [
        (
            ("missing.vcd",),
            (Path("searched"),),
            " (no fresh, nonempty file matching [flows.sim].trace_files "
            "['missing.vcd'] was produced by the current run)",
        ),
        ((), (Path("searched"),), KNOB_HINT),
        ((), (), ""),
    ],
)
def test_missing_trace_names_what_was_searched_for(
    patterns: tuple[str, ...], roots: tuple[Path, ...], hint: str
):
    _, result = finalize_trace(_current(_FakeTrace(), patterns, roots), None, convert_timeout_s=60)

    assert result.status == "incident"
    assert result.detail == OPENING + hint


def test_raw_vcd_is_always_an_incident(tmp_path: Path):
    """A VCD that cannot be converted never stands as the retained waveform."""
    raw = tmp_path / "dump.vcd"
    raw.write_bytes(b"0" * 8)
    trace = _FakeTrace(raw, failure="retained trace is raw .vcd")

    output, result = finalize_trace(
        _current(trace, ("dump.vcd",), (tmp_path,)), None, convert_timeout_s=60
    )

    assert result.status == "incident"
    assert "TRACE_INCIDENT" in output
    assert "TRACE_OK" not in output and "TRACE_FALLBACK" not in output


def test_budget_starts_and_pending_incident_is_published_before_conversion(tmp_path: Path):
    """The verdict is fixed first: the pending incident precedes postprocess."""
    trace = _FakeTrace(failure="no store")
    seen: list[tuple[str, ...]] = []

    def publish(pending) -> None:
        seen.append((pending.status, *trace.events))

    finalize_trace(
        _current(trace),
        None,
        convert_timeout_s=42,
        publish_pending=publish,
        raw_vcd=tmp_path / "dump.vcd",
    )

    assert seen == [("incident", "budget:42")]
    assert trace.events[:2] == ["budget:42", "postprocess"]

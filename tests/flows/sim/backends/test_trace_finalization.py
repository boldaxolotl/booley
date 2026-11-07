"""Shared trace finalization: what a finished run's waveform is worth to every native adapter."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.flows.sim.backends import shared as backend_shared
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


def test_ok_store_reports_metadata(tmp_path: Path, capsys):
    store = tmp_path / "trace.fst"
    store.write_bytes(b"fst")

    output, result = finalize_trace(_current(_FakeTrace(store, owned=True)), None)

    metadata = TraceArtifact(store, 3, "tb", 3, 7).metadata_line()
    assert output == f"\nTRACE_OK: {store}\n{metadata}"
    assert f"TRACE_OK: {store}\n{metadata}" in capsys.readouterr().out
    assert (result.status, result.path) == ("ok", str(store))
    assert (result.top_scope, result.signal_count, result.total_ticks) == ("tb", 3, 7)


def test_unqueryable_store_is_an_incident(tmp_path: Path, capsys):
    store = tmp_path / "trace.fst"
    store.write_bytes(b"fst")
    trace = _FakeTrace(store, failure="FST has no signals", owned=True)
    sim_proc, conversion = SimpleNamespace(returncode=1), SimpleNamespace(name="bwave")

    output, result = finalize_trace(_current(trace), sim_proc, conversion=conversion)

    reason = "trace requested but the retained waveform is not queryable: FST has no signals"
    assert trace.incidents == [(reason, sim_proc, conversion)]
    assert output == f"\nERROR: {reason}\nTRACE_INCIDENT: trace_incident.txt"
    assert f"ERROR: {reason}" in capsys.readouterr().out
    assert (result.status, result.path, result.detail) == (
        "incident",
        "trace_incident.txt",
        reason,
    )


def test_missing_trace_names_declared_patterns(tmp_path: Path):
    current = _current(_FakeTrace(), ("missing.vcd",), (tmp_path,))

    _, result = finalize_trace(current, None)

    assert result.status == "incident"
    assert result.detail == (
        f"{OPENING} (no fresh, nonempty file matching [flows.sim].trace_files "
        "['missing.vcd'] was produced by the current run)"
    )


@pytest.mark.parametrize(
    ("roots", "expected"),
    [((Path("searched"),), OPENING + KNOB_HINT), ((), OPENING)],
    ids=["roots-searched", "no-roots"],
)
def test_missing_trace_hints_the_knob_only_when_declared_files_are_searched(
    roots: tuple[Path, ...], expected: str
):
    _, result = finalize_trace(_current(_FakeTrace(), roots=roots), None)

    assert result.detail == expected


@pytest.mark.parametrize(
    ("vcd_fallback", "oversize", "status"),
    [(True, False, "ok"), (False, False, "incident"), (True, True, "incident")],
    ids=["enabled", "disabled", "oversize-even-when-enabled"],
)
def test_vcd_fallback_only_when_enabled(
    tmp_path: Path, monkeypatch, vcd_fallback: bool, oversize: bool, status: str
):
    """A raw VCD that cannot be converted stands only if the adapter opted in and it fit the cap."""
    monkeypatch.setattr(backend_shared, "MAX_ADOPTED_VCD_BYTES", 16)
    raw = tmp_path / "dump.vcd"
    raw.write_bytes(b"0" * (64 if oversize else 8))
    trace = _FakeTrace(raw, failure="retained trace is raw .vcd")

    output, result = finalize_trace(
        _current(trace, ("dump.vcd",), (tmp_path,)), None, vcd_fallback=vcd_fallback
    )

    assert result.status == status
    assert "TRACE_OK" not in output
    if status == "ok":
        assert output == f"\nTRACE_FALLBACK: {raw}"
        assert result.path == str(raw)
        assert "native FST conversion unavailable" in result.detail
    else:
        assert "TRACE_INCIDENT" in output and "TRACE_FALLBACK" not in output

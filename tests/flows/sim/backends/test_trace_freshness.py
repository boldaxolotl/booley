"""#2543: a traced run reports only the waveform the current run produced.

Every case drives one adapter's real trace path (``TraceSession`` included)
with files pre-created before the run and files written "by the simulator"
during it. Only the B-Wave process seam is stubbed: VCD conversion, and the
store-queryability probe that needs the native binary.
"""

from __future__ import annotations

import re
import sys
from collections import deque
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import NamedTuple

import pytest
from tests.conftest import MINIMAL_FST_BYTES

from booley.bwave import waveform_store
from booley.flows.sim import trace_session
from booley.flows.sim.adapter_transport import (
    AdapterTransportIdentity,
    partial_result_identity,
    read_adapter_result,
)
from booley.flows.sim.backends import cocotb as crun
from booley.flows.sim.backends import icarus as ir
from booley.flows.sim.backends import verilator as vr
from booley.flows.sim.backends.shared import CurrentRunTraces
from booley.flows.sim.backends.trace_finalization import finalize_trace
from booley.flows.sim.trace_session import TraceArtifact, TraceInspection, TraceSession

_VCD_TEXT = "$date today $end\n$scope module tb $end\n$upscope $end\n$enddefinitions $end\n"
ADAPTERS = ["icarus", "verilator", "cocotb"]


class Dirs(NamedTuple):
    build: Path
    run: Path
    work: Path


Produce = Callable[[Dirs], None]
# A simulator process that already exited cleanly.
_FINISHED = SimpleNamespace(returncode=0, pid=0, poll=lambda: 0)


@pytest.fixture(autouse=True)
def _b_wave_seam(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the cache under tmp_path; stub conversion and the queryability probe."""
    cache = tmp_path / "cache"
    monkeypatch.setattr(
        trace_session,
        "waveform_cache_dir",
        lambda *_args, **_kwargs: cache.mkdir(exist_ok=True) or cache,
    )
    monkeypatch.setattr(trace_session, "convert_vcd", _convert(True))
    monkeypatch.setattr(waveform_store, "convert_vcd", _convert(True))

    def inspect(_self, path: Path | None) -> TraceInspection:
        if path is None:
            return TraceInspection(None, "no retained trace artifact was found")
        if path.suffix.lower() != ".fst":
            return TraceInspection(None, f"retained trace is raw {path.suffix}, not an FST store")
        return TraceInspection(TraceArtifact(path, path.stat().st_size, "tb", 1, 1))

    monkeypatch.setattr(TraceSession, "inspect", inspect)


def _convert(success: bool):
    """Stand in for ``bwave build``: a present VCD becomes a valid store, or fails."""

    def convert(vcd: Path, destination: Path, **_kwargs):
        if not success or not vcd.is_file():
            return waveform_store.ConversionResult(
                destination, failure_kind="conversion_failed", detail="conversion failed"
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(MINIMAL_FST_BYTES)
        return waveform_store.ConversionResult(
            destination,
            inspection=waveform_store.inspect_store(destination, probe_reader=False),
        )

    return convert


def _fail_conversion(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(trace_session, "convert_vcd", _convert(False))
    monkeypatch.setattr(waveform_store, "convert_vcd", _convert(False))


def _own_trace(adapter: str) -> Produce:
    """The waveform Booley itself arranges for *adapter*, as the simulator writes it."""
    if adapter == "verilator":
        return lambda dirs: (dirs.work / "trace.fst").write_bytes(MINIMAL_FST_BYTES)
    return lambda dirs: (dirs.run / "dump.vcd").write_text(_VCD_TEXT)


def _run(
    adapter: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *,
    stale: dict[str, bytes | str] | None = None,
    produce: Produce = lambda _dirs: None,
    trace_files: tuple[str, ...] = (),
) -> str:
    """Run one traced simulation and return everything it reported.

    *stale* files (paths relative to ``tmp_path``, under ``build``/``run``/
    ``work``) exist before the run starts; *produce* writes during it.
    """
    dirs = Dirs(tmp_path / "build", tmp_path / "run", tmp_path / "work")
    for directory in dirs:
        directory.mkdir()
    for name, content in (stale or {}).items():
        path = tmp_path / name
        path.write_bytes(content) if isinstance(content, bytes) else path.write_text(content)
    declared = list(trace_files)
    if adapter == "icarus":
        (dirs.build / "sim_cfg.scr").write_text("")
        monkeypatch.setattr(ir, "_find_vvp", lambda: "vvp")
        run = ir._prepare_icarus_run(dirs.build, dirs.run, dirs.work, [], True, "tb", declared)
        assert not isinstance(run, str)
        produce(dirs)
        output, _ = finalize_trace(
            run.current, _FINISHED, convert_timeout_s=60, raw_vcd=run.run_cwd / "dump.vcd"
        )
    elif adapter == "verilator":
        (dirs.build / "Vtb_top").touch()

        def execute(*_args, **_kwargs):
            produce(dirs)
            return (
                deque(["[SIM_RESULT] PASSED\n"]),
                _FINISHED,
                vr.RunTermination(),
            )

        monkeypatch.setattr(vr, "_execute_supervised", execute)
        output = vr.run_verilated_binary(
            top_module="tb_top",
            bin_dir=dirs.build,
            run_cwd=dirs.run,
            work_dir=dirs.work,
            vcd=True,
            trace_mode=vr.TraceMode.NATIVE_FST,
            trace_args=["--trace={file}"],
            trace_files=declared,
        )
    else:
        monkeypatch.setattr(crun, "_prepare_invocation", lambda **_kw: ({}, ["sim"]))
        run = crun._prepare_cocotb_run(
            build_dir=dirs.build,
            run_cwd=dirs.run,
            work_dir=dirs.work,
            eda_tool="icarus",
            module="test_counter",
            tests=[],
            plusargs=[],
            vcd=True,
            trace_scope="tb",
        )
        assert not isinstance(run, str)
        produce(dirs)
        monkeypatch.setattr(
            crun, "_evaluate_verdict", lambda output, *_a, **_kw: (output, True, None)
        )
        crun._complete_cocotb_run(run, deque(), _FINISHED, crun.RunTermination(), "compact", None)
        output = ""
    return output + "\n" + capsys.readouterr().out


def _ok_path(text: str) -> str:
    match = re.search(r"TRACE_OK: (\S+)", text)
    assert match, text
    return match.group(1)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX path assertions")
@pytest.mark.parametrize("fresh", [False, True], ids=["stale_only", "with_fresh_trace"])
@pytest.mark.parametrize("adapter", ADAPTERS)
def test_stale_work_dir_fst_is_never_the_trace(adapter, fresh, tmp_path, monkeypatch, capsys):
    """AC1/AC2: a leftover store beside the work dir is neither adopted nor fatal."""
    out = _run(
        adapter,
        tmp_path,
        monkeypatch,
        capsys,
        stale={"work/old.fst": MINIMAL_FST_BYTES},
        produce=_own_trace(adapter) if fresh else lambda _dirs: None,
    )

    assert "old.fst" not in out
    if fresh:
        assert _ok_path(out) == str(tmp_path / "work" / "trace.fst")
    else:
        assert "TRACE_OK" not in out
        assert "TRACE_INCIDENT" in out


def test_icarus_converted_declared_vcd_beats_stale_declared_fst(tmp_path, monkeypatch, capsys):
    """AC3: the fresh declared VCD's converted store is the trace."""
    out = _run(
        "icarus",
        tmp_path,
        monkeypatch,
        capsys,
        stale={"work/tb.fst": MINIMAL_FST_BYTES},
        produce=lambda dirs: (dirs.run / "tb.vcd").write_text(_VCD_TEXT),
        trace_files=("tb.fst", "tb.vcd"),
    )

    assert _ok_path(out) == str(tmp_path / "work" / "trace.fst")
    assert "TRACE_INCIDENT" not in out


def test_icarus_unconvertible_declared_vcd_is_incident_despite_stale_fst(
    tmp_path, monkeypatch, capsys
):
    """A VCD that fails to convert is an incident; the stale store is not consulted."""
    _fail_conversion(monkeypatch)
    out = _run(
        "icarus",
        tmp_path,
        monkeypatch,
        capsys,
        stale={"work/tb.fst": MINIMAL_FST_BYTES},
        produce=lambda dirs: (dirs.run / "tb.vcd").write_text(_VCD_TEXT),
        trace_files=("tb.fst", "tb.vcd"),
    )

    assert "TRACE_INCIDENT" in out
    assert "TRACE_FALLBACK" not in out and "TRACE_OK" not in out
    assert "no fresh" not in out


@pytest.mark.parametrize("adapter", ["icarus", "verilator"])
def test_shadowed_stale_declared_trace_is_not_adopted(adapter, tmp_path, monkeypatch, capsys):
    """AC5: the run removes the earlier root's copy; a later root's stale copy stays stale."""
    out = _run(
        adapter,
        tmp_path,
        monkeypatch,
        capsys,
        stale={"run/tb.fst": MINIMAL_FST_BYTES, "build/tb.fst": MINIMAL_FST_BYTES},
        produce=lambda dirs: (dirs.run / "tb.fst").unlink(),
        trace_files=("tb.fst",),
    )

    assert "TRACE_OK" not in out
    assert "TRACE_INCIDENT" in out


@pytest.mark.parametrize("adapter", ["icarus", "verilator"])
def test_two_fresh_declared_stores_give_named_incident(adapter, tmp_path, monkeypatch, capsys):
    """AC6/AC9: with no Booley store, several fresh declared stores are an incident."""

    def produce(dirs: Dirs) -> None:
        (dirs.run / "a.fst").write_bytes(MINIMAL_FST_BYTES)
        (dirs.run / "b.fst").write_bytes(MINIMAL_FST_BYTES)

    out = _run(
        adapter, tmp_path, monkeypatch, capsys, produce=produce, trace_files=("a.fst", "b.fst")
    )

    assert "TRACE_OK" not in out
    assert "TRACE_INCIDENT" in out
    assert "a.fst" in out
    assert "b.fst" in out
    assert "no fresh" not in out


@pytest.mark.parametrize("adapter", ADAPTERS)
def test_fresh_undeclared_fst_is_not_adopted(adapter, tmp_path, monkeypatch, capsys):
    """AC7: a store nobody declared is not evidence about this run."""
    out = _run(
        adapter,
        tmp_path,
        monkeypatch,
        capsys,
        produce=lambda dirs: (dirs.work / "extra.fst").write_bytes(MINIMAL_FST_BYTES),
    )

    assert "TRACE_OK" not in out
    assert "TRACE_INCIDENT" in out


def test_stale_store_never_returned_after_failed_declared_vcd_conversion(
    tmp_path, monkeypatch, capsys
):
    """A failed conversion leaves the raw VCD; the stale store beside it must not win."""
    _fail_conversion(monkeypatch)
    out = _run(
        "verilator",
        tmp_path,
        monkeypatch,
        capsys,
        stale={"work/tb.fst": MINIMAL_FST_BYTES},
        produce=lambda dirs: (dirs.run / "tb.vcd").write_text(_VCD_TEXT),
        trace_files=("tb.fst", "tb.vcd"),
    )

    assert "TRACE_OK" not in out
    assert "retained waveform is not queryable" in out
    assert "no fresh" not in out


def _spy_conversion(monkeypatch: pytest.MonkeyPatch, partial: AdapterTransportIdentity) -> list:
    """Fail every conversion, recording what ``<result>.partial`` holds while it runs."""
    seen: list = []
    failing = _convert(False)

    def convert(vcd: Path, destination: Path, **kwargs):
        seen.append(read_adapter_result(partial_result_identity(partial)))
        return failing(vcd, destination, **kwargs)

    monkeypatch.setattr(trace_session, "convert_vcd", convert)
    monkeypatch.setattr(waveform_store, "convert_vcd", convert)
    return seen


def _check_pending_partial(adapter, sim_output, tmp_path, monkeypatch) -> None:
    """Killed mid-conversion, the attempt still has the simulation verdict plus an incident."""
    identity = AdapterTransportIdentity(
        "cocotb" if adapter == "cocotb" else adapter.split("-")[0],
        "attempt",
        "acme:lib:dut:1#sim",
        () if adapter == "cocotb" else ("smoke",),
        tmp_path / "result.json",
    )
    seen = _spy_conversion(monkeypatch, identity)
    dirs = Dirs(tmp_path / "build", tmp_path / "run", tmp_path / "work")
    for directory in dirs:
        directory.mkdir()
    passed = "PASSED" in sim_output

    if adapter == "cocotb":
        monkeypatch.setattr(crun, "_prepare_invocation", lambda **_kw: ({}, ["sim"]))
        run = crun._prepare_cocotb_run(
            build_dir=dirs.build,
            run_cwd=dirs.run,
            work_dir=dirs.work,
            eda_tool="icarus",
            module="test_counter",
            tests=[],
            plusargs=[],
            vcd=True,
            trace_scope="tb",
        )
        assert not isinstance(run, str)
        (dirs.run / "dump.vcd").write_text(_VCD_TEXT)
        monkeypatch.setattr(
            crun, "_evaluate_verdict", lambda output, *_a, **_kw: (output, passed, None)
        )
        crun._complete_cocotb_run(
            run,
            deque([sim_output]),
            SimpleNamespace(returncode=0 if passed else 1, pid=0, poll=lambda: 0),
            crun.RunTermination(),
            "compact",
            identity,
        )
    elif adapter == "icarus":
        (dirs.build / "sim_cfg.scr").write_text("")
        monkeypatch.setattr(ir, "_find_vvp", lambda: "vvp")
        run = ir._prepare_icarus_run(dirs.build, dirs.run, dirs.work, [], True, "tb", ["tb.vcd"])
        assert not isinstance(run, str)
        (dirs.run / "tb.vcd").write_text(_VCD_TEXT)
        publish = ir.native_pending_publisher(
            identity,
            sim_output,
            0 if passed else 1,
            pass_sentinels=None,
            fail_sentinels=None,
            termination=vr.RunTermination(),
        )
        finalize_trace(run.current, _FINISHED, convert_timeout_s=60, publish_pending=publish)
    else:  # the Verilator VCD fallback: a raw trace.vcd converted after the run
        (dirs.work / "trace.vcd").write_text(_VCD_TEXT)
        session = TraceSession(dirs.work, "tb", backend="verilator")
        trace = vr._TraceRuntime(
            session,
            CurrentRunTraces(session, (), (), {}),
            vr.TraceMode.VCD_FIFO,
            None,
        )
        paths = vr._RunPaths(dirs.build, dirs.run, dirs.work)
        vr._finalize_verilated_run(
            deque([sim_output]),
            SimpleNamespace(returncode=0 if passed else 1, pid=0, poll=lambda: 0),
            paths,
            trace,
            None,
            None,
            vr.RunTermination(),
            identity,
        )

    assert seen, "conversion never ran"
    pending = seen[0]
    assert pending.termination == "completed"  # a verdict, never a timeout
    assert pending.trace is not None and pending.trace.status == "incident"
    assert pending.passed is False
    if adapter != "cocotb":
        # The simulation verdict is already fixed: a pass is degraded by the trace
        # incident, a failing simulation stays failed, neither is a timeout.
        assert pending.failure_kind == "artifact"
        assert pending.test_results[0].verdict == ("inconclusive" if passed else "fail")
        assert pending.simulator_returncode == (0 if passed else 1)


def test_pending_partial_holds_verdict_during_conversion(tmp_path, monkeypatch):
    """Every adapter, passing and failing simulation: the verdict is fixed before conversion."""
    for adapter in ("icarus", "verilator-fallback", "cocotb"):
        for sim_output in ("[SIM_RESULT] PASSED\n", "[SIM_RESULT] FAILED\n"):
            case = tmp_path / f"{adapter}-{sim_output[13:19].strip()}"
            case.mkdir()
            _check_pending_partial(adapter, sim_output, case, monkeypatch)

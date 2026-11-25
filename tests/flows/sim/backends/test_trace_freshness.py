"""#2543: a traced run reports only the waveform the current run produced.

Every case drives one adapter's real trace path (``TraceSession`` included)
with files pre-created before the run and files written "by the simulator"
during it. Only the B-Wave process seam is stubbed: VCD conversion, and the
store-queryability probe that needs the native binary.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Callable
from pathlib import Path
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
from booley.flows.sim.backends.cocotb import run_cocotb_sim
from booley.flows.sim.backends.icarus import run_icarus_image
from booley.flows.sim.backends.verilator import TraceMode, run_verilated_binary
from booley.flows.sim.trace_session import TraceArtifact, TraceInspection, TraceSession

_VCD_TEXT = "$date today $end\n$scope module tb $end\n$upscope $end\n$enddefinitions $end\n"
ADAPTERS = ["icarus", "verilator", "cocotb"]


class Dirs(NamedTuple):
    build: Path
    run: Path
    work: Path


Produce = Callable[[Dirs], None]


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
            destination, inspection=waveform_store.inspect_store(destination, probe_reader=False)
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


@pytest.fixture(autouse=True)
def _simulator_tools(tool_stub, cocotb_config) -> None:
    """The ``vvp`` and ``cocotb-config`` the Icarus and cocotb runs look up on PATH."""
    tool_stub("vvp", "exit 0")
    cocotb_config()


@pytest.fixture
def run(tmp_path: Path, capsys: pytest.CaptureFixture[str], fake_launch):
    """``run(adapter, ...)``: one traced simulation through its public entry point.

    *stale* files (paths relative to ``tmp_path``, under ``build``/``run``/
    ``work``) exist before the run starts; *produce* writes during it. Returns
    everything the run reported.
    """

    def run_traced(
        adapter: str,
        *,
        stale: dict[str, bytes | str] | None = None,
        produce: Produce = lambda _dirs: None,
        trace_files: tuple[str, ...] = (),
    ) -> str:
        dirs = Dirs(tmp_path / "build", tmp_path / "run", tmp_path / "work")
        for directory in dirs:
            directory.mkdir()
        for name, content in (stale or {}).items():
            path = tmp_path / name
            path.write_bytes(content) if isinstance(content, bytes) else path.write_text(content)
        (dirs.build / "sim_cfg.scr").write_text("")
        (dirs.build / "Vtb_top").touch()
        fake_launch.during = lambda _launch: produce(dirs)
        paths = {"run_cwd": dirs.run, "work_dir": dirs.work}
        if adapter == "icarus":
            output = run_icarus_image(
                **paths,
                build_dir=dirs.build,
                vcd=True,
                trace_scope="tb",
                trace_files=list(trace_files),
            ).output
        elif adapter == "verilator":
            output = run_verilated_binary(
                **paths,
                top_module="tb_top",
                bin_dir=dirs.build,
                vcd=True,
                trace_mode=TraceMode.NATIVE_FST,
                trace_args=["--trace={file}"],
                trace_files=list(trace_files),
            ).output
        else:
            run_cocotb_sim(
                **paths,
                build_dir=dirs.build,
                eda_tool="icarus",
                cocotb_module="test_counter",
                vcd=True,
                expected_trace_scope="tb",
            )
            output = ""
        return output + "\n" + capsys.readouterr().out

    return run_traced


def _ok_path(text: str) -> str:
    match = re.search(r"TRACE_OK: (\S+)", text)
    assert match, text
    return match.group(1)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX path assertions")
@pytest.mark.parametrize("fresh", [False, True], ids=["stale_only", "with_fresh_trace"])
@pytest.mark.parametrize("adapter", ADAPTERS)
def test_stale_work_dir_fst_is_never_the_trace(adapter, fresh, tmp_path, run):
    """AC1/AC2: a leftover store beside the work dir is neither adopted nor fatal."""
    out = run(
        adapter,
        stale={"work/old.fst": MINIMAL_FST_BYTES},
        produce=_own_trace(adapter) if fresh else lambda _dirs: None,
    )

    assert "old.fst" not in out
    if fresh:
        assert _ok_path(out) == str(tmp_path / "work" / "trace.fst")
    else:
        assert "TRACE_OK" not in out
        assert "TRACE_INCIDENT" in out


def test_icarus_converted_declared_vcd_beats_stale_declared_fst(tmp_path, run):
    """AC3: the fresh declared VCD's converted store is the trace."""
    out = run(
        "icarus",
        stale={"work/tb.fst": MINIMAL_FST_BYTES},
        produce=lambda dirs: (dirs.run / "tb.vcd").write_text(_VCD_TEXT),
        trace_files=("tb.fst", "tb.vcd"),
    )

    assert _ok_path(out) == str(tmp_path / "work" / "trace.fst")
    assert "TRACE_INCIDENT" not in out


def test_icarus_unconvertible_declared_vcd_is_incident_despite_stale_fst(monkeypatch, run):
    """A VCD that fails to convert is an incident; the stale store is not consulted."""
    _fail_conversion(monkeypatch)
    out = run(
        "icarus",
        stale={"work/tb.fst": MINIMAL_FST_BYTES},
        produce=lambda dirs: (dirs.run / "tb.vcd").write_text(_VCD_TEXT),
        trace_files=("tb.fst", "tb.vcd"),
    )

    assert "TRACE_INCIDENT" in out
    assert "TRACE_FALLBACK" not in out and "TRACE_OK" not in out
    assert "no fresh" not in out


@pytest.mark.parametrize("adapter", ["icarus", "verilator"])
def test_shadowed_stale_declared_trace_is_not_adopted(adapter, run):
    """AC5: the run removes the earlier root's copy; a later root's stale copy stays stale."""
    out = run(
        adapter,
        stale={"run/tb.fst": MINIMAL_FST_BYTES, "build/tb.fst": MINIMAL_FST_BYTES},
        produce=lambda dirs: (dirs.run / "tb.fst").unlink(),
        trace_files=("tb.fst",),
    )

    assert "TRACE_OK" not in out
    assert "TRACE_INCIDENT" in out


@pytest.mark.parametrize("adapter", ["icarus", "verilator"])
def test_two_fresh_declared_stores_give_named_incident(adapter, run):
    """AC6/AC9: with no Booley store, several fresh declared stores are an incident."""

    def produce(dirs: Dirs) -> None:
        (dirs.run / "a.fst").write_bytes(MINIMAL_FST_BYTES)
        (dirs.run / "b.fst").write_bytes(MINIMAL_FST_BYTES)

    out = run(adapter, produce=produce, trace_files=("a.fst", "b.fst"))

    assert "TRACE_OK" not in out
    assert "TRACE_INCIDENT" in out
    assert "a.fst" in out
    assert "b.fst" in out
    assert "no fresh" not in out


@pytest.mark.parametrize("adapter", ADAPTERS)
def test_fresh_undeclared_fst_is_not_adopted(adapter, run):
    """AC7: a store nobody declared is not evidence about this run."""
    out = run(
        adapter, produce=lambda dirs: (dirs.work / "extra.fst").write_bytes(MINIMAL_FST_BYTES)
    )

    assert "TRACE_OK" not in out
    assert "TRACE_INCIDENT" in out


def test_stale_store_never_returned_after_failed_declared_vcd_conversion(monkeypatch, run):
    """A failed conversion leaves the raw VCD; the stale store beside it must not win."""
    _fail_conversion(monkeypatch)
    out = run(
        "verilator",
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


def _check_cocotb_verdicts(pending, identity, passed: bool) -> None:
    """Per-test verdicts are fixed too, and the final publish agrees with them."""
    verdicts = {test.name: test.verdict for test in pending.test_results}
    final = read_adapter_result(identity)
    assert verdicts == {test.name: test.verdict for test in final.test_results}
    assert verdicts == ({"t_ok": "inconclusive"} if passed else {"t_ok": "fail", "t_bad": "fail"})


def _check_pending_partial(adapter, sim_output, tmp_path, monkeypatch, fake_launch) -> None:
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
    (dirs.build / "sim_cfg.scr").write_text("")
    (dirs.build / "Vtb_top").touch()
    passed = "PASSED" in sim_output
    fake_launch.output = [sim_output]
    fake_launch.returncode = 0 if passed else 1
    # The real entry points, so dropping a ``publish_pending=`` wiring fails here.
    paths = {"run_cwd": dirs.run, "work_dir": dirs.work, "transport": identity}

    if adapter == "cocotb":

        def during(launch) -> None:
            (dirs.run / "dump.vcd").write_text(_VCD_TEXT)
            failing = (
                "" if passed else '<testcase name="t_bad"><failure message="boom"/></testcase>'
            )
            Path(launch.env["COCOTB_RESULTS_FILE"]).write_text(
                f'<testsuite><testcase name="t_ok"/>{failing}</testsuite>'
            )

        fake_launch.during = during
        run_cocotb_sim(
            **paths,
            build_dir=dirs.build,
            eda_tool="icarus",
            cocotb_module="test_counter",
            vcd=True,
            expected_trace_scope="tb",
        )
    elif adapter == "icarus":
        fake_launch.during = lambda _launch: (dirs.run / "tb.vcd").write_text(_VCD_TEXT)
        run_icarus_image(
            **paths, build_dir=dirs.build, vcd=True, trace_scope="tb", trace_files=["tb.vcd"]
        )
    else:  # the Verilator VCD fallback: no FIFO, a raw trace.vcd converted after the run
        monkeypatch.setattr(trace_session, "start_streaming_conversion", lambda *_a, **_k: None)
        fake_launch.during = lambda _launch: (dirs.work / "trace.vcd").write_text(_VCD_TEXT)
        run_verilated_binary(
            **paths,
            top_module="tb_top",
            bin_dir=dirs.build,
            vcd=True,
            trace_scope="tb",
            trace_mode=TraceMode.VCD_FIFO,
        )

    assert seen, "conversion never ran"
    pending = seen[0]
    assert pending.termination == "completed"  # a verdict, never a timeout
    assert pending.trace is not None and pending.trace.status == "incident"
    assert pending.passed is False
    if adapter == "cocotb":
        _check_cocotb_verdicts(pending, identity, passed)
        return
    # The simulation verdict is already fixed: a pass is degraded by the trace
    # incident, a failing simulation stays failed, neither is a timeout.
    assert pending.failure_kind == "artifact"
    assert pending.test_results[0].verdict == ("inconclusive" if passed else "fail")
    assert pending.simulator_returncode == (0 if passed else 1)


def test_pending_partial_holds_verdict_during_conversion(tmp_path, monkeypatch, fake_launch):
    """Every adapter, passing and failing simulation: the verdict is fixed before conversion."""
    for adapter in ("icarus", "verilator-fallback", "cocotb"):
        for sim_output in ("[SIM_RESULT] PASSED\n", "[SIM_RESULT] FAILED\n"):
            case = tmp_path / f"{adapter}-{sim_output[13:19].strip()}"
            case.mkdir()
            _check_pending_partial(adapter, sim_output, case, monkeypatch, fake_launch)

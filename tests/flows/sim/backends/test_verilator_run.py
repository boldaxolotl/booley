"""Unit tests for the edalize-era Verilator run-half (booley.flows.sim.backends.verilator)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from tests.conftest import MINIMAL_FST_BYTES
from tests.time_budget import hang_guard_s

from booley.flows.sim.adapter_contract import PreparedSimulationWork
from booley.flows.sim.adapter_transport import AdapterTransportIdentity, read_adapter_result
from booley.flows.sim.backends import shared as backend_shared
from booley.flows.sim.backends import verilator as vr
from booley.flows.sim.backends.supervision import SimulatorLaunch
from booley.flows.sim.result import SIM_INFRA_ERROR_PREFIX
from booley.flows.sim.trace_session import TraceSession


def _build_dir(tmp_path: Path) -> Path:
    """A build dir holding an edalize-style flat ``Vtb_top`` that the fake launch never runs."""
    build = tmp_path / "build"
    build.mkdir()
    (build / "Vtb_top").write_text("#!/bin/sh\n")
    return build


@pytest.mark.parametrize(
    ("plusargs", "tail"),
    [
        # bare plusargs gain a leading +, already-prefixed ones are left alone
        (["test_id=1", "+already"], ["+test_id=1", "+already"]),
        # ADR 0072: the seed plusarg reaches the binary as ``+verilator+seed+<n>``
        (["test_id=1", "verilator+seed+2147483647"], ["+test_id=1", "+verilator+seed+2147483647"]),
        # SETUP-7: a '-'/'--' selector is a getopt argument, forwarded without a '+'
        (["--meminit=ram,boot.elf"], ["--meminit=ram,boot.elf"]),
    ],
)
def test_binary_is_launched_flat_with_the_plusargs(tmp_path: Path, fake_launch, plusargs, tail):
    """Edalize links V<top> flat in the build dir (no obj_dir/)."""
    build = _build_dir(tmp_path)
    vr.run_verilated_binary(top_module="tb_top", bin_dir=build, plusargs=plusargs)
    launch = fake_launch.launches[0]
    assert launch.command == [str(build / "Vtb_top"), *tail]
    if sys.platform != "win32":  # a DPI .so placed beside the binary still resolves
        assert str(build) in launch.env["LD_LIBRARY_PATH"]


class _StandInConversion:
    """A FIFO conversion whose ``finish`` records when the adapter cleans it up."""

    pid = 0
    return_code = 0

    def __init__(self, events: list[str]):
        self.events = events

    def poll(self) -> int:
        return 0

    def finish(self):
        self.events.append("finish")


@pytest.fixture
def fifo_conversion(monkeypatch) -> list[str]:
    """``start_streaming_conversion`` yields a stand-in; the list records ``finish`` calls."""
    events: list[str] = []
    monkeypatch.setattr(
        "booley.flows.sim.trace_session.start_streaming_conversion",
        lambda *_args, **_kwargs: _StandInConversion(events),
    )
    return events


@pytest.mark.parametrize(
    ("mode", "dest", "trace_args", "tail"),
    [
        # F-15: the trace CLI is the project's to declare; the default is Booley's plusargs
        (vr.TraceMode.NATIVE_FST, "trace.fst", None, ["+trace", "+tracefile={trace}"]),
        (vr.TraceMode.NATIVE_FST, "trace.fst", ["--trace={file}"], ["--trace={trace}"]),
        (vr.TraceMode.VCD_FIFO, "trace.fifo", ["--trace={file}"], ["--trace={trace}"]),
    ],
)
def test_trace_arguments_follow_the_declared_contract(
    tmp_path: Path, fake_launch, fifo_conversion, mode, dest, trace_args, tail
):
    build, work = _build_dir(tmp_path), tmp_path / "work"
    vr.run_verilated_binary(
        top_module="tb_top",
        bin_dir=build,
        work_dir=work,
        vcd=True,
        trace_scope="tb",
        trace_mode=mode,
        trace_args=trace_args,
    )
    expected = [arg.format(trace=work / dest) for arg in tail]
    assert fake_launch.launches[0].command == [str(build / "Vtb_top"), *expected]


def test_file_templates_dropped_without_a_destination(tmp_path: Path, fake_launch, monkeypatch):
    # No FIFO means nowhere to write; an unrendered template would reach the
    # binary as the literal string "{file}".
    monkeypatch.setattr(
        "booley.flows.sim.trace_session.start_streaming_conversion", lambda *_a, **_k: None
    )
    build = _build_dir(tmp_path)
    vr.run_verilated_binary(
        top_module="tb_top",
        bin_dir=build,
        vcd=True,
        trace_scope="tb",
        trace_args=["-t", "--trace={file}"],
    )
    assert fake_launch.launches[0].command == [str(build / "Vtb_top"), "-t"]


@pytest.mark.skipif(sys.platform == "win32", reason="the FIFO recipe is POSIX only")
def test_launch_passes_guard_inputs_and_cleans_the_fifo_conversion(
    tmp_path: Path, fake_launch, fifo_conversion
):
    """Every guard input reaches the shared runner; the converter is finished after it."""
    build = _build_dir(tmp_path)
    fake_launch.during = lambda launch: fifo_conversion.append("launch")

    vr.run_verilated_binary(
        top_module="tb_top",
        bin_dir=build,
        vcd=True,
        trace_scope="tb",
        timeout=42,
        max_rundir_bytes=1234,
        runtime_inputs=("memory.hex",),
    )

    (launch,) = fake_launch.launches
    assert launch == SimulatorLaunch(
        "Verilator",
        [str(build / "Vtb_top"), "+trace", f"+tracefile={build / 'trace.fifo'}"],
        build,
        launch.env,
        42,
        build,
        1234,
        ("memory.hex",),
        trace=launch.trace,
        conversion=launch.conversion,
        streams=launch.streams,
    )
    assert isinstance(launch.trace, TraceSession)
    assert launch.conversion is not None  # the stall monitor needs the converter
    assert fifo_conversion == ["launch", "finish"]


def _verilator_work(tmp_path: Path, **overrides: object) -> PreparedSimulationWork:
    """A Verilator launch whose build dir and work dir live under *tmp_path*."""
    fields: dict[str, object] = {
        "adapter": "verilator",
        "build_dir": str(tmp_path / "build"),
        "run_cwd": "",
        "timeout_s": 30,
        "top": "tb_top",
    }
    fields.update(overrides)
    return PreparedSimulationWork(**fields)  # type: ignore[arg-type]


def test_document_fields_reach_run_verilated_binary(tmp_path: Path, monkeypatch):
    from booley.flows.sim import run_guard
    from booley.flows.sim.adapter_contract import encode_simulation_work

    seen: dict[str, object] = {}

    def record(**kwargs: object) -> SimpleNamespace:
        seen.update(kwargs)
        return SimpleNamespace(exit_code=0)

    monkeypatch.setattr(run_guard, "install_parent_death_guard", lambda: None)
    monkeypatch.setattr(vr, "run_verilated_binary", record)
    result_path = tmp_path / "adapter-result.json"
    work = _verilator_work(
        tmp_path,
        run_cwd=str(tmp_path / "util"),
        work_dir=str(tmp_path / "work"),
        plusargs=("test_id=2", "verbose"),
        trace=True,
        trace_mode="native_fst",
        trace_scope="tb.dut",
        trace_args=("--trace={file}",),
        trace_files=("fpu.vcd",),
        trace_convert_timeout_s=90,
        booley_dir=str(tmp_path / ".booley"),
        runtime_inputs=("firmware.hex",),
        pass_sentinels=("ALL TESTS PASSED.",),
        fail_sentinels=("ERROR!", "TIMEOUT"),
        max_rundir_bytes=5000,
        tests=("t1", "t2"),
        adapter_result_path=str(result_path),
        attempt_token="attempt-1",
        target_identity="acme:lib:core:1#sim",
    )

    assert vr.main([encode_simulation_work(work)]) == 0

    assert seen == {
        "top_module": "tb_top",
        "bin_dir": tmp_path / "build",
        "run_cwd": tmp_path / "util",
        "work_dir": tmp_path / "work",
        "vcd": True,
        "plusargs": ["test_id=2", "verbose"],
        "timeout": 30,
        "trace_args": ["--trace={file}"],
        "trace_files": ["fpu.vcd"],
        "trace_mode": vr.TraceMode.NATIVE_FST,
        "pass_sentinels": ["ALL TESTS PASSED."],
        "fail_sentinels": ["ERROR!", "TIMEOUT"],
        "max_rundir_bytes": 5000,
        "runtime_inputs": ("firmware.hex",),
        "trace_convert_timeout": 90,
        "booley_dir": tmp_path / ".booley",
        "transport": AdapterTransportIdentity(
            adapter="verilator",
            attempt_token="attempt-1",
            target_identity="acme:lib:core:1#sim",
            selected_tests=("t1", "t2"),
            result_path=result_path,
        ),
    }  # fmt: skip
    # The scope is not forwarded (the run-half resolves it itself).
    assert "trace_scope" not in seen


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX V<top> stub")
def test_document_launch_exits_with_the_sentinel_verdict(tmp_path: Path, monkeypatch, capsys):
    for printed, expected_exit in (("ALL TESTS PASSED.", 0), ("ERROR!", 1)):
        _launch_prints(tmp_path / str(expected_exit), monkeypatch, capsys, printed, expected_exit)


def _launch_prints(tmp_path: Path, monkeypatch, capsys, printed: str, expected_exit: int):
    from booley.flows.sim import run_guard
    from booley.flows.sim.adapter_contract import encode_simulation_work

    monkeypatch.setattr(run_guard, "install_parent_death_guard", lambda: None)
    build = tmp_path / "build"
    build.mkdir(parents=True)
    exe = build / "Vtb_top"
    # The simulator's own exit code (3) must not decide the verdict.
    exe.write_bytes(f"#!/bin/sh\necho '{printed}'\nexit 3\n".encode())
    exe.chmod(0o755)
    work = _verilator_work(
        tmp_path,
        work_dir=str(tmp_path / "work"),
        pass_sentinels=("ALL TESTS PASSED.",),
        fail_sentinels=("ERROR!",),
    )

    assert vr.main([encode_simulation_work(work)]) == expected_exit
    assert printed in capsys.readouterr().out


def test_missing_exe_is_an_infra_error_not_a_verdict(tmp_path: Path):
    """SETUP-F-41b: no binary means no observation — the marker lets a grading
    caller refuse to score the run instead of reading rc!=0 as a FAIL. The
    run-half still creates the work dir it owns (a missing parent broke os.mkfifo)."""
    work_dir = tmp_path / "sim" / "config_a"  # nested, does not exist
    out = vr.run_verilated_binary(top_module="tb_top", bin_dir=tmp_path, work_dir=work_dir).output
    assert "not found" in out
    assert SIM_INFRA_ERROR_PREFIX in out
    assert (work_dir / "run.log").exists()


def test_cocotb_vtop_binary_is_named_not_silently_run(tmp_path: Path):
    """SETUP-F-40: the cocotb flow builds Vtop, not V<top>. Picking it up here
    would run a VPI-driven binary with no cocotb env (an empty pass), so the
    run-half must name the mismatch instead."""
    (tmp_path / "Vtop").write_text("#!/bin/sh\n", encoding="utf-8")

    out = vr.run_verilated_binary(top_module="ravenoc_wrapper", bin_dir=tmp_path).output
    assert "Cocotb Target" in out
    assert "booley.flows.sim.backends.cocotb" in out


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="run-half execs a POSIX sim binary (#!/bin/sh stub); real sims run in-container",
)
def test_run_verilated_binary_writes_run_log_end_to_end(tmp_path: Path, capsys):
    """The full run path persists the raw output to <work_dir>/run.log —
    including on a FAIL (nonzero rc + FAIL sentinel)."""
    bin_dir = tmp_path / "build"
    bin_dir.mkdir()
    exe = bin_dir / "Vtb_top"
    exe.write_text("#!/bin/sh\necho 'TB chatter'\necho '[SIM_RESULT] FAILED'\nexit 1\n")
    exe.chmod(0o755)

    work_dir = tmp_path / "work"
    vr.run_verilated_binary(
        top_module="tb_top", bin_dir=bin_dir, work_dir=work_dir, timeout=hang_guard_s()
    )

    log = (work_dir / "run.log").read_text(encoding="utf-8")
    assert "TB chatter" in log
    assert "[SIM_RESULT] FAILED" in log
    assert (work_dir / "result.json").exists()  # run.log sits beside result.json


@pytest.mark.parametrize(
    ("kind", "failure_kind", "missing_path"),
    [
        ("trace_stall", "infrastructure", ""),
        ("disk_budget", "infrastructure", ""),
        ("fatal_init", "missing_input", "memory.hex"),
    ],
)
def test_guard_termination_is_published_to_adapter_transport(
    tmp_path: Path, fake_launch, kind: str, failure_kind: str, missing_path: str
) -> None:
    fake_launch.termination = vr.RunTermination(
        kind, f"{kind} stopped the simulator", failure_kind, missing_path
    )
    fake_launch.returncode = 9
    fake_launch.output = ["guard fired\n"]
    identity = AdapterTransportIdentity(
        "verilator", "attempt", "acme:lib:dut:1#sim", ("smoke",), tmp_path / "adapter.json"
    )

    vr.run_verilated_binary(top_module="tb_top", bin_dir=_build_dir(tmp_path), transport=identity)

    result = read_adapter_result(identity)
    assert result.simulator_returncode == 9
    assert result.termination == kind
    assert result.failure_kind == failure_kind
    assert result.missing_input_path == missing_path


@pytest.fixture
def bwave_info(monkeypatch) -> None:
    """The native bwave binary reports a queryable one-signal waveform."""
    monkeypatch.setattr("booley.bwave.waveform_store.native_bwave_binary", lambda: "/bin/bwave")
    info = {
        "scope_prefix": "tb_top",
        "root_scopes": ["tb_top"],
        "signal_count": 1,
        "total_ticks": 2,
        "signals": [{"name": "clk"}],
    }
    monkeypatch.setattr(
        vr.subprocess,
        "run",
        lambda *_args, **_kwargs: vr.subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json.dumps({"data": info}), stderr=""
        ),
    )


@pytest.mark.usefixtures("bwave_info")
def test_native_fst_run_writes_regular_file_without_fifo(tmp_path: Path, fake_launch):
    bin_dir = _build_dir(tmp_path)
    work_dir = tmp_path / "trace"
    fake_launch.during = lambda _launch: (work_dir / "trace.fst").write_bytes(MINIMAL_FST_BYTES)

    output = vr.run_verilated_binary(
        top_module="tb_top",
        bin_dir=bin_dir,
        work_dir=work_dir,
        vcd=True,
        trace_mode=vr.TraceMode.NATIVE_FST,
        trace_args=["--trace={file}"],
    ).output

    assert f"TRACE_OK: {work_dir / 'trace.fst'}" in output
    assert "TRACE_METADATA:" in output
    assert (work_dir / "trace.fst").read_bytes() == MINIMAL_FST_BYTES
    assert not (work_dir / "trace.fifo").exists()


@pytest.mark.usefixtures("bwave_info", "fake_launch")
def test_declared_native_fst_must_be_fresh_for_current_run(tmp_path: Path):
    bin_dir = _build_dir(tmp_path)
    (bin_dir / "hardcoded.fst").write_bytes(MINIMAL_FST_BYTES)

    output = vr.run_verilated_binary(
        top_module="tb_top",
        bin_dir=bin_dir,
        vcd=True,
        trace_mode=vr.TraceMode.NATIVE_FST,
        trace_args=["-t"],
        trace_files=["hardcoded.fst"],
    ).output

    assert "TRACE_OK:" not in output
    assert "TRACE_INCIDENT:" in output
    assert "current run" in output


# ---------------------------------------------------------------------------
# Per-run safety guards: missing $readmemh (SETUP-23) + disk budget (SETUP-25)
# ---------------------------------------------------------------------------


class TestDeclaredTraceFiles:
    """F-22: a custom main()'s dump can be declared instead of guessed."""

    class _FakeTrace:
        """Trace seam stub: the store exists only once a VCD was converted."""

        owned_outputs: frozenset[Path] = frozenset()

        def __init__(self, store: Path | None = None):
            self.store = store
            self.postprocessed: list[Path] = []

        def postprocess(self, path: Path) -> None:
            self.postprocessed.append(path)

        def find(self):
            return self.store if self.postprocessed else None

        def write_incident(self, reason, **_kw):
            return Path("trace_incident.txt")

    @staticmethod
    def _current(trace, patterns: list[str], roots: list[Path]):
        return backend_shared.CurrentRunTraces(trace, tuple(patterns), tuple(roots), {})

    def test_declared_vcd_is_postprocessed_and_adopted(self, tmp_path: Path):
        run_cwd = tmp_path / "tests"
        run_cwd.mkdir()
        (run_cwd / "fpu.vcd").write_text("$var wire 1 ! clk $end\n")
        store = tmp_path / "trace.fst"
        store.write_bytes(b"fst")
        trace = self._FakeTrace(store)
        current = self._current(trace, ["fpu.vcd"], [run_cwd, tmp_path])
        assert current.select().path == store
        assert trace.postprocessed == [run_cwd / "fpu.vcd"]

    def test_globs_and_non_vcd_artifacts(self, tmp_path: Path):
        (tmp_path / "dump_003.fst").write_bytes(b"fst")
        trace = self._FakeTrace(None)
        current = self._current(trace, ["dump_*.fst"], [tmp_path])
        assert current.select().path == tmp_path / "dump_003.fst"
        assert trace.postprocessed == []  # already a store; no conversion

    @pytest.mark.skipif(sys.platform == "win32", reason="NTFS would allocate the sparse 2 GiB")
    def test_vcd_over_2gib_is_converted(self, tmp_path: Path):
        """No size cap: a declared VCD past 2 GiB is converted and its store selected."""
        store = tmp_path / "trace.fst"
        store.write_bytes(b"fst")
        big = tmp_path / "fpu.vcd"
        with big.open("wb") as stream:
            stream.truncate(2 * 1024**3 + 1)  # sparse: no real 2 GiB write
        trace = self._FakeTrace(store)

        selection = self._current(trace, ["fpu.vcd"], [tmp_path]).select()

        assert selection.path == store  # the converted store, what finalize turns into TRACE_OK
        assert trace.postprocessed == [big]

    def test_empty_and_missing_candidates_are_ignored(self, tmp_path: Path):
        (tmp_path / "empty.vcd").write_text("")
        trace = self._FakeTrace(None)
        for patterns in (["empty.vcd"], ["nope.vcd"], []):
            assert self._current(trace, patterns, [tmp_path]).select().path is None

"""Unit tests for the edalize-era Verilator run-half (booley.flows.sim.backends.verilator)."""

from __future__ import annotations

import json
import sys
from collections import deque
from pathlib import Path

import pytest
from tests.conftest import MINIMAL_FST_BYTES

from booley.flows.sim.adapter_transport import AdapterTransportIdentity, read_adapter_result
from booley.flows.sim.backends import shared as backend_shared
from booley.flows.sim.backends import verilator as vr
from booley.flows.sim.backends.supervision import SimulatorLaunch
from booley.flows.sim.result import SIM_INFRA_ERROR_PREFIX


def test_find_binary_locates_flat_vtop(tmp_path: Path):
    """Edalize links V<top> flat in the build dir (no obj_dir/)."""
    exe = tmp_path / "Vtb_top"
    exe.write_text("#!/bin/sh\n")
    assert vr._find_binary(tmp_path, "tb_top") == exe
    assert vr._find_binary(tmp_path, "missing") is None


def test_build_run_cmd_appends_plusargs(tmp_path: Path):
    exe = tmp_path / "Vtop"
    cmd, env = vr._build_run_cmd(exe, tmp_path, ["test_id=1", "+already"])
    assert cmd[0] == str(exe)
    # bare plusargs gain a leading +, already-prefixed ones are left alone
    assert "+test_id=1" in cmd
    assert "+already" in cmd
    # Trace arguments are appended by _setup_trace, which owns the destination.
    assert "+trace" not in cmd
    # LD_LIBRARY_PATH widened to the binary dir (harmless for a static binary)
    if "LD_LIBRARY_PATH" in env:
        assert str(tmp_path) in env["LD_LIBRARY_PATH"]


def test_build_run_cmd_passes_the_seed_option_the_runtime_reads(tmp_path: Path):
    # ADR 0072: the engine renders the seed as the plusarg ``verilator+seed+<n>``;
    # the binary must receive Verilator's own ``+verilator+seed+<n>`` spelling.
    cmd, _ = vr._build_run_cmd(
        tmp_path / "Vtop", tmp_path, ["test_id=1", "iters=5", "verilator+seed+2147483647"]
    )
    assert cmd[1:] == ["+test_id=1", "+iters=5", "+verilator+seed+2147483647"]


def test_build_run_cmd_forwards_getopt_arg_verbatim(tmp_path: Path):
    # SETUP-7: a '-'/'--' selector is a getopt argument, forwarded to the
    # binary's main WITHOUT a '+' prefix (unlike bare plusargs).
    cmd, _ = vr._build_run_cmd(tmp_path / "Vtop", tmp_path, ["--meminit=ram,boot.elf"])
    assert "--meminit=ram,boot.elf" in cmd
    assert "+--meminit=ram,boot.elf" not in cmd


class TestTraceArgContract:
    """F-15: the trace CLI is the project's to declare, not Booley's to assume.

    Ibex's VerilatorSimCtrl enables capture only for getopt `-t`/`--trace=FILE`.
    Booley's generic plusarg pair was ignored, the run passed, and a 443-byte
    header-only FST was accepted as a traced simulation.
    """

    def test_default_is_booleys_own_plusarg_convention(self, tmp_path: Path):
        rendered = vr._render_trace_args(None, tmp_path / "trace.fifo")
        assert rendered == ["+trace", f"+tracefile={tmp_path / 'trace.fifo'}"]

    def test_project_contract_replaces_the_default(self, tmp_path: Path):
        fifo = tmp_path / "trace.fifo"
        rendered = vr._render_trace_args(["--trace={file}"], fifo)
        assert rendered == [f"--trace={fifo}"]
        assert "+trace" not in rendered

    def test_file_templates_dropped_without_a_destination(self):
        # The non-FIFO path has nowhere to write; an unrendered template would
        # reach the binary as the literal string "{file}".
        assert vr._render_trace_args(["-t", "--trace={file}"], None) == ["-t"]

    def test_setup_bwave_appends_the_configured_contract(self, tmp_path: Path):
        class FakeTrace:
            fifo_path = tmp_path / "trace.fifo"

            def start_fifo(self):
                return None, True, None

        cmd = ["/bin/Vtop"]
        vr._setup_bwave(FakeTrace(), cmd, ["--trace={file}"])
        assert cmd == ["/bin/Vtop", f"--trace={tmp_path / 'trace.fifo'}"]

    def test_trace_arg_round_trips_through_the_cli(self):
        # The `=` form is mandatory for an option-like value: passing it as a
        # separate argv item makes argparse read it as a runner option (F-12).
        args = vr._parse_args(
            [
                "--bin-dir",
                "build/sim",
                "--top",
                "tb",
                "--trace",
                "--trace-mode",
                "native_fst",
                "--trace-arg=--trace={file}",
            ]
        )
        assert args.trace_args == ["--trace={file}"]
        assert args.trace_mode == vr.TraceMode.NATIVE_FST


def test_parse_args_round_trips_run_options():
    args = vr._parse_args(
        [
            "--bin-dir",
            "build/sim",
            "--top",
            "tb_top",
            "--run-cwd",
            "util/sim",
            "--timeout",
            "120",
            "--trace",
            "--trace-scope",
            "tb.dut",
            "--plusarg",
            "test_id=2",
            "--plusarg",
            "verbose",
        ]
    )
    assert args.bin_dir == "build/sim"
    assert args.top == "tb_top"
    assert args.run_cwd == "util/sim"
    assert args.timeout == 120
    assert args.trace is True
    assert args.trace_scope == "tb.dut"
    assert args.plusargs == ["test_id=2", "verbose"]


def test_parse_args_round_trips_sentinels():
    args = vr._parse_args(
        [
            "--bin-dir",
            "b",
            "--top",
            "tb",
            "--pass-sentinel",
            "ALL TESTS PASSED.",
            "--fail-sentinel",
            "ERROR!",
            "--fail-sentinel",
            "TIMEOUT",
        ]
    )
    assert args.pass_sentinels == ["ALL TESTS PASSED."]
    assert args.fail_sentinels == ["ERROR!", "TIMEOUT"]


def test_run_verilated_binary_missing_exe_returns_error(tmp_path: Path):
    """A missing binary yields a clear error string (no crash)."""
    out = vr.run_verilated_binary(top_module="tb_top", bin_dir=tmp_path)
    assert "not found" in out


def test_missing_exe_is_marked_as_infra_not_a_verdict(tmp_path: Path):
    """SETUP-F-41b: no binary means no observation — the marker lets a grading
    caller refuse to score the run instead of reading rc!=0 as a FAIL."""
    out = vr.run_verilated_binary(top_module="tb_top", bin_dir=tmp_path)
    assert SIM_INFRA_ERROR_PREFIX in out
    assert (tmp_path / "run.log").exists()


def test_cocotb_vtop_binary_is_named_not_silently_run(tmp_path: Path):
    """SETUP-F-40: the cocotb flow builds Vtop, not V<top>. Picking it up here
    would run a VPI-driven binary with no cocotb env (an empty pass), so the
    run-half must name the mismatch instead."""
    (tmp_path / "Vtop").write_text("#!/bin/sh\n", encoding="utf-8")
    assert vr._find_binary(tmp_path, "ravenoc_wrapper") is None

    out = vr.run_verilated_binary(top_module="ravenoc_wrapper", bin_dir=tmp_path)
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
    vr.run_verilated_binary(top_module="tb_top", bin_dir=bin_dir, work_dir=work_dir, timeout=30)

    log = (work_dir / "run.log").read_text(encoding="utf-8")
    assert "TB chatter" in log
    assert "[SIM_RESULT] FAILED" in log
    assert (work_dir / "result.json").exists()  # run.log sits beside result.json


def test_run_verilated_binary_creates_missing_work_dir(tmp_path: Path):
    """The run-half owns its output dir: a caller-derived trace/result dir that
    doesn't exist yet is created (else os.mkfifo / write_result_json would fail
    on a missing parent — the bug that broke the first coverage A.2 run)."""
    bin_dir = tmp_path / "build"
    bin_dir.mkdir()
    work_dir = tmp_path / "sim" / "config_a"  # nested, does not exist
    # No binary present → returns early after the mkdir, before any run.
    vr.run_verilated_binary(top_module="tb_top", bin_dir=bin_dir, work_dir=work_dir)
    assert work_dir.is_dir()


@pytest.mark.parametrize(
    ("kind", "failure_kind", "missing_path"),
    [
        ("trace_stall", "infrastructure", ""),
        ("disk_budget", "infrastructure", ""),
        ("fatal_init", "missing_input", "memory.hex"),
    ],
)
def test_guard_termination_is_published_to_adapter_transport(
    tmp_path: Path, monkeypatch, kind: str, failure_kind: str, missing_path: str
) -> None:
    termination = vr.RunTermination(
        kind, f"{kind} stopped the simulator", failure_kind, missing_path
    )
    process = type("Process", (), {"returncode": 9})()
    monkeypatch.setattr(
        vr,
        "_execute_supervised",
        lambda *_args: (deque(["guard fired\n"]), process, termination),
    )
    monkeypatch.setattr(
        vr,
        "_finalize_verilated_run",
        lambda *_args: ("guard fired\n", None),
    )
    identity = AdapterTransportIdentity(
        "verilator", "attempt", "acme:lib:dut:1#sim", ("smoke",), tmp_path / "adapter.json"
    )
    paths = vr._RunPaths(tmp_path, tmp_path, tmp_path)
    trace = vr._TraceRuntime(None, None, vr.TraceMode.VCD_FIFO, None)

    vr._execute_verilated_and_publish([], paths, {}, 30, trace, 0, (), None, None, False, identity)

    result = read_adapter_result(identity)
    assert result.simulator_returncode == 9
    assert result.termination == kind
    assert result.failure_kind == failure_kind
    assert result.missing_input_path == missing_path


def test_execute_supervised_cleans_fifo_conversion(tmp_path: Path, monkeypatch):
    class FakeSession:
        def __init__(self):
            self.cleaned = None

        def cleanup_fifo(self, conversion):
            self.cleaned = conversion

    session = FakeSession()
    conversion = object()
    trace = vr._TraceRuntime(
        session=session,
        current=None,
        mode=vr.TraceMode.VCD_FIFO,
        conversion=conversion,
    )
    paths = vr._RunPaths(tmp_path, tmp_path, tmp_path)
    monkeypatch.setattr(
        vr,
        "run_supervised",
        lambda *_args, **_kwargs: (deque(), object(), vr.RunTermination()),
    )

    vr._execute_supervised([], paths, {}, 1, trace, 0, ())

    assert session.cleaned is conversion


def test_execute_passes_guard_inputs_to_the_runner(tmp_path: Path, monkeypatch):
    """AC3 wiring: every guard input reaches the shared runner's launch spec."""
    launches: list[SimulatorLaunch] = []

    class FakeSession:
        def cleanup_fifo(self, conversion):
            pass

    session = FakeSession()
    conversion = object()
    trace = vr._TraceRuntime(
        session=session,
        current=None,
        mode=vr.TraceMode.VCD_FIFO,
        conversion=conversion,
    )
    run_cwd = tmp_path / "cwd"
    work_dir = tmp_path / "work"
    paths = vr._RunPaths(tmp_path, run_cwd, work_dir)

    def record(launch):
        launches.append(launch)
        return deque(), object(), vr.RunTermination()

    monkeypatch.setattr(vr, "run_supervised", record)

    vr._execute_supervised(["Vtb_top", "+x"], paths, {"K": "V"}, 42, trace, 1234, ("memory.hex",))

    (launch,) = launches
    assert launch == SimulatorLaunch(
        "Verilator",
        ["Vtb_top", "+x"],
        run_cwd,
        {"K": "V"},
        42,
        work_dir,
        1234,
        ("memory.hex",),
        trace=session,
        conversion=conversion,
    )


class _FinishedProcess:
    """Small cross-platform stand-in for a successfully exited simulator."""

    pid = 0
    returncode = 0

    def poll(self) -> int:
        return self.returncode


def _stub_verilated_execution(
    monkeypatch: pytest.MonkeyPatch,
    *,
    trace_path: Path | None = None,
) -> None:
    """Replace OS process startup while retaining run-finalization behavior."""

    def execute(*_args, **_kwargs):
        if trace_path is not None:
            trace_path.write_bytes(MINIMAL_FST_BYTES)
        return deque(["[SIM_RESULT] PASSED\n"]), _FinishedProcess(), vr.RunTermination()

    monkeypatch.setattr(vr, "_execute_supervised", execute)


def test_native_fst_run_writes_regular_file_without_fifo(tmp_path: Path, monkeypatch):
    bin_dir = tmp_path / "build"
    work_dir = tmp_path / "trace"
    bin_dir.mkdir()
    exe = bin_dir / "Vtb_top"
    exe.touch()
    _stub_verilated_execution(monkeypatch, trace_path=work_dir / "trace.fst")
    monkeypatch.setattr("booley.bwave.waveform_store.native_bwave_binary", lambda: "/bin/bwave")
    monkeypatch.setattr(
        vr.subprocess,
        "run",
        lambda *_args, **_kwargs: vr.subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps(
                {
                    "data": {
                        "scope_prefix": "tb_top",
                        "root_scopes": ["tb_top"],
                        "signal_count": 1,
                        "total_ticks": 2,
                        "signals": [{"name": "clk"}],
                    }
                }
            ),
            stderr="",
        ),
    )

    output = vr.run_verilated_binary(
        top_module="tb_top",
        bin_dir=bin_dir,
        work_dir=work_dir,
        vcd=True,
        trace_mode=vr.TraceMode.NATIVE_FST,
        trace_args=["--trace={file}"],
    )

    assert f"TRACE_OK: {work_dir / 'trace.fst'}" in output
    assert "TRACE_METADATA:" in output
    assert (work_dir / "trace.fst").read_bytes() == MINIMAL_FST_BYTES
    assert not (work_dir / "trace.fifo").exists()


def test_declared_native_fst_must_be_fresh_for_current_run(tmp_path: Path, monkeypatch):
    bin_dir = tmp_path / "build"
    bin_dir.mkdir()
    stale = bin_dir / "hardcoded.fst"
    stale.write_bytes(MINIMAL_FST_BYTES)
    exe = bin_dir / "Vtb_top"
    exe.touch()
    _stub_verilated_execution(monkeypatch)
    monkeypatch.setattr("booley.bwave.waveform_store.native_bwave_binary", lambda: "/bin/bwave")
    monkeypatch.setattr(
        vr.subprocess,
        "run",
        lambda *_args, **_kwargs: vr.subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps(
                {
                    "data": {
                        "scope_prefix": "tb_top",
                        "root_scopes": ["tb_top"],
                        "signal_count": 1,
                        "total_ticks": 2,
                        "signals": [{"name": "clk"}],
                    }
                }
            ),
            stderr="",
        ),
    )

    output = vr.run_verilated_binary(
        top_module="tb_top",
        bin_dir=bin_dir,
        vcd=True,
        trace_mode=vr.TraceMode.NATIVE_FST,
        trace_args=["-t"],
        trace_files=["hardcoded.fst"],
    )

    assert "TRACE_OK:" not in output
    assert "TRACE_INCIDENT:" in output
    assert "current run" in output


@pytest.mark.parametrize("bad", ["0", "-5"])
def test_parse_args_rejects_non_positive_timeout(bad: str):
    # A non-positive timeout would make the deadline math time out instantly.
    with pytest.raises(SystemExit):
        vr._parse_args(["--bin-dir", "b", "--top", "t", "--timeout", bad])


# ---------------------------------------------------------------------------
# Per-run safety guards: missing $readmemh (SETUP-23) + disk budget (SETUP-25)
# ---------------------------------------------------------------------------


def test_parse_args_round_trips_max_rundir_bytes():
    args = vr._parse_args(["--bin-dir", "b", "--top", "t", "--max-rundir-bytes", "5000"])
    assert args.max_rundir_bytes == 5000
    # Unset -> guard disabled (0).
    assert vr._parse_args(["--bin-dir", "b", "--top", "t"]).max_rundir_bytes == 0


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


def test_trace_file_round_trips_through_the_cli():
    args = vr._parse_args(["--bin-dir", "b", "--top", "t", "--trace", "--trace-file=fpu.vcd"])
    assert args.trace_files == ["fpu.vcd"]
    assert vr._parse_args(["--bin-dir", "b", "--top", "t"]).trace_files == []

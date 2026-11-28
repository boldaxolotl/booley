"""#2929: declared ``trace_files`` VCDs stream through named pipes on POSIX.

Each case runs a real adapter (Verilator or Icarus) whose simulator is a small
Python script acting on the declared path, with the native B-Wave binary fake
standing in for the native ``bwave build``. #4233: every launch, traced or not,
first sweeps the pipes a killed run's ownership record names; the Cocotb launch is covered too.
"""

from __future__ import annotations

import json
import math
import os
import stat
import sys
from pathlib import Path
from typing import NamedTuple

import pytest
from tests.bwave.native_binary import answer_vcd_build, fake_native_binary
from tests.conftest import MINIMAL_FST_BYTES
from tests.flows.sim.test_simulation_record_crash_matrix import _KILLED, _run_killed
from tests.flows.sim.test_trace_overlay import _needs_kill, _records
from tests.time_budget import hang_guard_s, scaled

from booley.bwave import waveform_store
from booley.flows.sim import trace_session
from booley.flows.sim.adapter_transport import AdapterTransportIdentity, read_adapter_result
from booley.flows.sim.backends import icarus as icarus_adapter
from booley.flows.sim.backends import verilator as verilator_adapter
from booley.flows.sim.backends.declared_streams import DeclaredTraceStreams
from booley.flows.sim.staged_input_ledger import StagedInputLedger, sweep_cancelled_run_inputs
from booley.flows.sim.trace_session import TraceSession

pytestmark = pytest.mark.skipif(os.name != "posix", reason="named pipes need POSIX")

_RUN_TIMEOUT_S = hang_guard_s()
_VCD = (
    "$timescale 1ns $end\n$scope module tb $end\n$var wire 1 ! clk $end\n"
    "$upscope $end\n$enddefinitions $end\n#0\n0!\n#5\n1!\n"
)

# A simulator that follows FAKE_SIM: ``;``-separated ``op:arg`` steps in its cwd.
_FAKE_SIM = f"""#!{sys.executable}
import json, os, stat, time

VCD = {_VCD!r}
FST = {MINIMAL_FST_BYTES!r}


def kind(path):
    try:
        mode = os.lstat(path).st_mode
    except OSError:
        return "missing"
    return "fifo" if stat.S_ISFIFO(mode) else "file"


types = {{}}
for step in os.environ["FAKE_SIM"].split(";"):
    op, _, arg = step.partition(":")
    if op == "record":
        types.update({{path: kind(path) for path in arg.split(",")}})
        with open("types.json", "w") as out:
            json.dump(types, out)
    elif op == "write":
        with open(arg, "w") as out:
            out.write(VCD)
    elif op == "write_fst":
        with open(arg, "wb") as out:
            out.write(FST)
    elif op == "write_cut":
        with open(arg, "w") as out:
            out.write(VCD[:-1])
    elif op == "flood":
        with open(arg, "w") as out:
            out.write(VCD + "#9\\n1!\\n" * 150000)
    elif op == "unlink":
        if os.path.lexists(arg):
            os.unlink(arg)
        with open(arg, "w") as out:
            out.write(VCD)
    elif op == "replace":
        with open("swap.tmp", "w") as out:
            out.write(VCD)
        os.replace("swap.tmp", arg)
    elif op == "hang":
        time.sleep(60)
print("[SIM_RESULT] PASSED")
"""


class Dirs(NamedTuple):
    build: Path
    run: Path
    work: Path


class Outcome(NamedTuple):
    output: str
    dirs: Dirs

    def types(self) -> dict[str, str]:
        return json.loads((self.dirs.run / "types.json").read_text(encoding="utf-8"))

    def attempts(self, kind: str) -> list[dict[str, str]]:
        status = json.loads((self.dirs.work / "trace_status.json").read_text(encoding="utf-8"))
        return [attempt for attempt in status["attempts"] if attempt["kind"] == kind]


@pytest.fixture(autouse=True)
def _streaming_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Re-enable streaming (the sim conftest disables it) and keep the cache local."""
    monkeypatch.setattr(waveform_store, "can_stream_waveform", lambda: True)
    answer_vcd_build(fake_native_binary(tmp_path / "bwave", monkeypatch))
    cache = tmp_path / "cache"
    monkeypatch.setattr(
        trace_session,
        "waveform_cache_dir",
        lambda *_args, **_kwargs: cache.mkdir(exist_ok=True) or cache,
    )


def _refusing_bwave(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = fake_native_binary(tmp_path / "refusing", monkeypatch)
    fake.answer("build", stderr="fake bwave refused the input\n", returncode=1)


def _stage(adapter: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, steps: str) -> Dirs:
    """Create the build, run and work directories and the fake simulator for *adapter*."""
    dirs = Dirs(tmp_path / "build", tmp_path / "run", tmp_path / "work")
    for directory in dirs:
        directory.mkdir(parents=True, exist_ok=True)
    sim = dirs.build / "fake_sim"
    sim.write_text(_FAKE_SIM, encoding="utf-8")
    sim.chmod(0o755)
    monkeypatch.setenv("FAKE_SIM", steps)
    if adapter == "icarus":
        (dirs.build / "sim_cfg.scr").write_text("", encoding="utf-8")
        # The adapter resolves `vvp` on PATH: a directory holding only the fake simulator.
        fake_bin = tmp_path / "fake-bin"
        fake_bin.mkdir(exist_ok=True)
        (fake_bin / "vvp").symlink_to(sim)
        monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")
    else:
        (dirs.build / "Vtb_top").symlink_to(sim)
    return dirs


def _run(
    adapter: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    steps: str,
    entries: list[str],
    *,
    precreate: dict[str, str] | None = None,
    timeout: int = _RUN_TIMEOUT_S,
    trace_mode: verilator_adapter.TraceMode = verilator_adapter.TraceMode.NATIVE_FST,
    transport: AdapterTransportIdentity | None = None,
) -> Outcome:
    """Run one traced simulation of *adapter* whose simulator performs *steps*."""
    dirs = _stage(adapter, tmp_path, monkeypatch, steps)
    for name, content in (precreate or {}).items():
        (dirs.run / name).write_text(content, encoding="utf-8")
    if adapter == "icarus":
        run = icarus_adapter.run_icarus_image(
            build_dir=dirs.build,
            run_cwd=dirs.run,
            work_dir=dirs.work,
            vcd=True,
            trace_files=entries,
            timeout=timeout,
            transport=transport,
            booley_dir=tmp_path / ".booley",
        )
    else:
        run = verilator_adapter.run_verilated_binary(
            top_module="tb_top",
            bin_dir=dirs.build,
            run_cwd=dirs.run,
            work_dir=dirs.work,
            vcd=True,
            trace_mode=trace_mode,
            trace_args=["--trace={file}"],
            trace_files=entries,
            timeout=timeout,
            transport=transport,
            booley_dir=tmp_path / ".booley",
        )
    return Outcome(run.output, dirs)


_CUT_WARNING = (
    "WARNING: input does not end with a newline; the final line (byte {offset}) "
    "may be a truncated record"
)


@pytest.mark.parametrize(
    ("adapter", "steps", "entries", "mode", "offset"),
    [
        pytest.param("icarus", "write_cut:dump.vcd", [], None, 109, id="post_run"),
        pytest.param(
            "verilator",
            "write_cut:../work/trace.fifo",
            [],
            verilator_adapter.TraceMode.VCD_FIFO,
            109,
            id="fifo",
        ),
        pytest.param(
            "verilator", "write_cut:fpu.vcd", ["fpu.vcd"], None, 109, id="declared_stream"
        ),
    ],
)
def test_conversion_warning_reaches_the_trace_result(
    adapter, steps, entries, mode, offset, tmp_path, monkeypatch
):
    """#5059: a successful conversion's `WARNING:` lines travel with its trace result."""
    identity = AdapterTransportIdentity(
        adapter=adapter,
        attempt_token="token",
        target_identity="target",
        selected_tests=(),
        result_path=tmp_path / "result.json",
    )
    kwargs = {"trace_mode": mode} if mode else {}
    out = _run(adapter, tmp_path, monkeypatch, steps, entries, transport=identity, **kwargs)

    warning = _CUT_WARNING.format(offset=offset)
    assert warning in out.output
    result = read_adapter_result(identity)
    trace = result.trace
    assert result.passed
    assert trace is not None
    assert (trace.status, trace.warnings) == ("ok", (warning,))


_FIFO_DIAGNOSTICS = (
    "[bwave] WARNING: bwave exited with rc=1",
    "[bwave] stderr: fake bwave: no VCD header",
)
_POST_RUN_DIAGNOSTICS = ("VCD not found:", "[bwave] WARNING: post-process failed (rc=None)")


@pytest.mark.parametrize(
    ("adapter", "mode", "steps", "entries", "subject", "kind", "evidence", "diagnostics"),
    [
        pytest.param(
            "verilator",
            verilator_adapter.TraceMode.VCD_FIFO,
            "write:fpu.vcd",
            ["fpu.vcd"],
            "the FIFO conversion",
            "fifo_cleanup",
            "fake bwave: no VCD header",
            _FIFO_DIAGNOSTICS,
            id="fifo-recovered",
        ),
        pytest.param(
            "verilator",
            verilator_adapter.TraceMode.VCD_FIFO,
            "",
            [],
            None,
            "fifo_cleanup",
            "fake bwave: no VCD header",
            _FIFO_DIAGNOSTICS,
            id="fifo-no-waveform",
        ),
        pytest.param(
            "icarus",
            verilator_adapter.TraceMode.NATIVE_FST,
            "write:fpu.vcd",
            ["fpu.vcd"],
            "the post-run conversion of dump.vcd",
            "vcd_postprocess_build",
            "VCD not found:",
            _POST_RUN_DIAGNOSTICS,
            id="post-run-recovered",
        ),
        pytest.param(
            "icarus",
            verilator_adapter.TraceMode.NATIVE_FST,
            "",
            [],
            None,
            "vcd_postprocess_build",
            "VCD not found:",
            _POST_RUN_DIAGNOSTICS,
            id="post-run-no-waveform",
        ),
    ],
)
def test_failed_conversion_diagnostics_wait_for_the_waveform_outcome(
    adapter,
    mode,
    steps,
    entries,
    subject,
    kind,
    evidence,
    diagnostics,
    tmp_path,
    monkeypatch,
    capsys,
):
    """#5316: a declared trace that replaces a failed conversion leaves one note, not bwave errors."""
    out = _run(adapter, tmp_path, monkeypatch, steps, entries, trace_mode=mode)

    printed = capsys.readouterr().out
    lines = printed.splitlines()
    notes = [line for line in lines if line.startswith("[trace] note:")]
    stderr_path = tmp_path / "work" / "trace.fst.stderr"
    if subject:
        assert notes == [
            f"[trace] note: {subject} did not produce the trace; used the declared trace "
            f"instead (bwave output: {stderr_path})"
        ]
        assert not any(text in printed for text in diagnostics)
    else:
        assert not notes
        error = next(
            i for i, line in enumerate(lines) if line.startswith("ERROR: trace requested")
        )
        for text in diagnostics:
            assert any(text in line for line in lines[:error])
    assert evidence in out.attempts(kind)[-1]["detail"]
    assert evidence in stderr_path.read_text(encoding="utf-8")


def test_failed_supervision_still_releases_held_fifo_diagnostics(monkeypatch):
    """finalize_trace never runs when the simulator supervisor raises, so cleanup must release."""
    from types import SimpleNamespace

    events: list[str] = []
    session = SimpleNamespace(
        cleanup_fifo=lambda _conversion: events.append("cleanup"),
        report_failed_conversions=lambda *, declared_trace_used: events.append(
            f"report:{declared_trace_used}"
        ),
    )
    trace = SimpleNamespace(
        session=session, current=None, mode=verilator_adapter.TraceMode.VCD_FIFO, conversion=None
    )

    def _boom(_launch):
        raise KeyboardInterrupt

    monkeypatch.setattr(verilator_adapter, "run_supervised", _boom)

    with pytest.raises(KeyboardInterrupt):
        verilator_adapter._execute_supervised(
            [], SimpleNamespace(run_cwd=None, work_dir=None), {}, 1, trace, 0, ()
        )

    assert events == ["cleanup", "report:False"]


def _no_pipe_left(run_dir: Path, name: str) -> None:
    assert not os.path.lexists(run_dir / name)
    assert not [path for path in run_dir.iterdir() if path.name.endswith(".booley-pipe")]
    assert not _records(run_dir.parent)


@pytest.mark.parametrize("adapter", ["verilator", "icarus"])
def test_declared_vcd_streams_to_trace_ok(adapter, tmp_path, monkeypatch):
    out = _run(adapter, tmp_path, monkeypatch, "record:fpu.vcd;write:fpu.vcd", ["fpu.vcd"])

    assert out.types() == {"fpu.vcd": "fifo"}
    # Published as the session's own trace.fst, so `bwave register SIM_DIR` finds it.
    assert f"TRACE_OK: {tmp_path / 'work' / 'trace.fst'}" in out.output
    # The streamed store is moved onto trace.fst, so the run keeps one copy.
    assert not (tmp_path / "work" / "trace_streams" / "0-fpu.fst").exists()
    assert sorted(p.name for p in (tmp_path / "work").rglob("*.fst")) == ["trace.fst"]
    assert "[SIM_RESULT] PASSED" in out.output
    _no_pipe_left(out.dirs.run, "fpu.vcd")
    # Icarus always converts its own dump.vcd; the declared path is never converted.
    assert not [a for a in out.attempts("vcd_postprocess") if "fpu.vcd" in a.get("detail", "")]


def _assert_host_without_streaming_converts_after_the_run(tmp_path, monkeypatch):
    """Windows or no streaming support: the entry stays a plain file, converted afterwards."""
    monkeypatch.setattr(waveform_store, "can_stream_waveform", lambda: False)
    plain = _run("verilator", tmp_path, monkeypatch, "write:fpu.vcd;record:fpu.vcd", ["fpu.vcd"])

    assert plain.types() == {"fpu.vcd": "file"}
    assert "TRACE_OK" in plain.output
    assert any("fpu.vcd" in a["detail"] for a in plain.attempts("vcd_postprocess"))


@pytest.mark.parametrize(
    ("entry", "written", "op"),
    [
        pytest.param("g*.vcd", "gold.vcd", "write", id="glob"),
        pytest.param("{tmp}/abs.vcd", "{tmp}/abs.vcd", "write", id="absolute"),
        pytest.param("../outside.vcd", "../outside.vcd", "write", id="outside_run_dir"),
        pytest.param("other.fst", "other.fst", "write_fst", id="fst"),
        pytest.param(None, "fpu.vcd", "write", id="no_stream_support"),
    ],
)
def test_ineligible_entries_are_not_piped(entry, written, op, tmp_path, monkeypatch):
    """An entry that cannot stream stays a plain file next to one that does."""
    if entry is None:  # Windows or no streaming support
        _assert_host_without_streaming_converts_after_the_run(tmp_path, monkeypatch)
        return
    written = written.format(tmp=tmp_path)
    entries = [entry.format(tmp=tmp_path), "fpu.vcd"]
    steps = f"{op}:{written};record:fpu.vcd,{written};write:fpu.vcd"
    out = _run("verilator", tmp_path, monkeypatch, steps, entries)

    types = out.types()
    assert types.pop("fpu.vcd") == "fifo"
    assert set(types.values()) <= {"file"}
    assert "TRACE_OK" in out.output
    assert "TRACE_INCIDENT" not in out.output
    if written.endswith(".vcd"):  # the plain VCD was converted after the run
        details = [a["detail"] for a in out.attempts("vcd_postprocess")]
        assert any(Path(written).name in detail for detail in details)


@pytest.mark.parametrize("how", ["unlink", "replace"])
def test_unlinked_or_replaced_path_falls_back(how, tmp_path, monkeypatch):
    """A testbench that swaps the pipe for a file keeps the verdict; that file is converted."""
    out = _run("verilator", tmp_path, monkeypatch, f"record:fpu.vcd;{how}:fpu.vcd", ["fpu.vcd"])

    assert out.types() == {"fpu.vcd": "fifo"}
    assert "[SIM_RESULT] PASSED" in out.output
    assert f"TRACE_OK: {tmp_path / 'work' / 'trace.fst'}" in out.output
    assert [a["status"] for a in out.attempts("declared_stream")] == ["started", "failed"]
    assert any("fpu.vcd" in a["detail"] for a in out.attempts("vcd_postprocess"))
    _no_pipe_left(out.dirs.run, "fpu.vcd")


_NO_HEADER_TEXT = (
    "streaming fpu.vcd failed: fake bwave: no VCD header (if the testbench seeks in, "
    "reads back, or reopens this dump, make it write the dump once, front to back, "
    "or dump FST instead)"
)


def _finished_streams(tmp_path: Path, budget_s: float) -> DeclaredTraceStreams:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    session = TraceSession(tmp_path / "work")
    session.start_conversion_budget(budget_s)
    streams = DeclaredTraceStreams.start(session, ("fpu.vcd",), run_dir, tmp_path / ".booley")
    streams.close_inputs()
    streams.finish()
    return streams


def test_budget_exhausted_stream_keeps_its_text(tmp_path, monkeypatch):
    fake_native_binary(tmp_path / "idle", monkeypatch).answer("build", hang_s=hang_guard_s())
    streams = _finished_streams(tmp_path, 0)

    assert streams.failures == {
        "fpu.vcd": "streaming fpu.vcd failed: no usable waveform was written"
    }


def test_never_opened_pipe_keeps_verdict_and_reports_incident(tmp_path, monkeypatch):
    out = _run("verilator", tmp_path, monkeypatch, "record:fpu.vcd", ["fpu.vcd"])

    assert "[SIM_RESULT] PASSED" in out.output
    assert "TRACE_INCIDENT" in out.output
    assert _NO_HEADER_TEXT in out.output
    _no_pipe_left(out.dirs.run, "fpu.vcd")


def test_converter_exit_does_not_block_writer(tmp_path, monkeypatch):
    """A converter that died is drained, so a simulator writing 1 MiB still finishes."""
    _refusing_bwave(tmp_path, monkeypatch)
    out = _run(
        "verilator", tmp_path, monkeypatch, "flood:fpu.vcd", ["fpu.vcd"], timeout=hang_guard_s()
    )

    assert "[SIM_RESULT] PASSED" in out.output
    assert "streaming fpu.vcd failed: fake bwave refused the input" in out.output
    _no_pipe_left(out.dirs.run, "fpu.vcd")


def test_pipe_removed_after_timeout_kill(tmp_path, monkeypatch):
    out = _run(
        "verilator",
        tmp_path,
        monkeypatch,
        "record:fpu.vcd;hang",
        ["fpu.vcd"],
        timeout=math.ceil(scaled(3)),
    )

    assert "timed out" in out.output
    assert out.types() == {"fpu.vcd": "fifo"}
    _no_pipe_left(out.dirs.run, "fpu.vcd")


def _killed_stream(tmp_path: Path, *, name: str = "fpu.vcd", before_confirmation: bool = False):
    """Leave the pipe of a run killed (like a SIGKILL) right after it made the pipe."""
    (tmp_path / "run").mkdir(exist_ok=True)

    def worker() -> None:
        _refusing_bwave(tmp_path, pytest.MonkeyPatch())
        if before_confirmation:

            def dying(*_args: object) -> None:
                # No public entry point stops between creating the pipe and confirming it.
                os._exit(_KILLED)

            StagedInputLedger.created = dying  # type: ignore[method-assign]
        session = TraceSession(tmp_path / "killed-work")
        DeclaredTraceStreams.start(session, (name,), tmp_path / "run", tmp_path / ".booley")
        os._exit(_KILLED)

    _run_killed(worker, tmp_path / "trace.txt")


@_needs_kill
@pytest.mark.parametrize("how", ["next-run", "recovery-sweep"])
@pytest.mark.parametrize("stage", ["before-confirmation", "recorded"])
def test_killed_runs_pipe_is_gone_once_the_next_run_starts(stage, how, tmp_path, monkeypatch):
    """AC1: a killed run's pipe, before or after it was confirmed, is removed by the next sweep."""
    _killed_stream(tmp_path, before_confirmation=stage == "before-confirmation")
    run = tmp_path / "run"
    left = [path.name for path in run.iterdir()]
    assert left == (["fpu.vcd"] if stage == "recorded" else [left[0]])
    assert stat.S_ISFIFO((run / left[0]).lstat().st_mode)
    assert len(_records(tmp_path)) == 1

    if how == "recovery-sweep":
        sweep_cancelled_run_inputs(tmp_path / ".booley")
        _no_pipe_left(run, "fpu.vcd")
        assert not list(run.iterdir())
        return
    out = _run("verilator", tmp_path, monkeypatch, "record:fpu.vcd;write:fpu.vcd", ["fpu.vcd"])

    assert [a["status"] for a in out.attempts("declared_stream")] == ["started", "success"]
    _no_pipe_left(run, "fpu.vcd")


@_needs_kill
def test_interrupted_sweep_keeps_the_pipe_recorded(tmp_path):
    """A sweep killed before it unlinks the pipe leaves it recorded, so the next sweep removes it."""
    _killed_stream(tmp_path)
    booley = tmp_path / ".booley"

    def sweep() -> None:
        def dying(*_args: object, **_kwargs: object) -> None:
            # No public entry point stops a sweep before its unlink: the OS call is replaced.
            os._exit(_KILLED)

        Path.unlink = dying  # type: ignore[method-assign]
        sweep_cancelled_run_inputs(booley)
        os._exit(_KILLED)

    _run_killed(sweep, tmp_path / "sweep-trace.txt")
    assert stat.S_ISFIFO((tmp_path / "run" / "fpu.vcd").lstat().st_mode)
    assert len(_records(tmp_path)) == 1

    sweep_cancelled_run_inputs(booley)
    _no_pipe_left(tmp_path / "run", "fpu.vcd")


@_needs_kill
def test_unremovable_staged_pipe_stays_recorded_for_the_next_sweep(tmp_path, monkeypatch):
    """A pipe whose unlink fails keeps its record, so a later sweep still removes it."""
    _killed_stream(tmp_path, before_confirmation=True)
    run = tmp_path / "run"
    real_unlink = Path.unlink

    def refuse(self: Path, missing_ok: bool = False) -> None:
        if self.name.endswith(".booley-pipe"):
            raise PermissionError("run directory is read-only")
        real_unlink(self, missing_ok=missing_ok)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", refuse)
        sweep_cancelled_run_inputs(tmp_path / ".booley")
    assert len(list(run.iterdir())) == 1
    assert len(_records(tmp_path)) == 1

    sweep_cancelled_run_inputs(tmp_path / ".booley")
    assert not list(run.iterdir())
    assert not _records(tmp_path)


def test_failed_converter_start_leaves_no_pipe_and_no_descriptor(tmp_path, monkeypatch):
    """A failure after the pipe exists removes it and closes what was opened."""
    real_open, opened = os.open, []

    def open_once(path: str, flags: int, *args: int) -> int:
        if opened:
            raise OSError("too many open files")
        opened.append(real_open(path, flags, *args))
        return opened[0]

    monkeypatch.setattr(os, "open", open_once)
    fifo = tmp_path / ".staged.booley-pipe"
    with pytest.raises(OSError, match="too many open files"):
        waveform_store.start_streaming_conversion(
            fifo, tmp_path / "t.fst", stderr_path=tmp_path / "t.stderr", replace_existing=False
        )

    assert not fifo.exists()
    with pytest.raises(OSError, match="Bad file descriptor"):
        os.fstat(opened[0])


@_needs_kill
def test_live_runs_pipe_survives_a_sweep(tmp_path):
    """AC4: the pipe of a run that is still alive is left alone."""
    run = tmp_path / "run"
    run.mkdir()
    streams = DeclaredTraceStreams.start(
        TraceSession(tmp_path / "work"), ("fpu.vcd",), run, tmp_path / ".booley"
    )
    inode = (run / "fpu.vcd").lstat().st_ino

    sweep_cancelled_run_inputs(tmp_path / ".booley")

    assert stat.S_ISFIFO((run / "fpu.vcd").lstat().st_mode)
    assert (run / "fpu.vcd").lstat().st_ino == inode
    assert len(_records(tmp_path)) == 1
    streams.close_inputs()
    streams.finish()
    _no_pipe_left(run, "fpu.vcd")


def test_existing_user_file_is_kept_and_falls_back(tmp_path, monkeypatch):
    out = _run(
        "verilator",
        tmp_path,
        monkeypatch,
        "record:a.vcd,b.vcd;write:b.vcd",
        ["a.vcd", "b.vcd"],
        precreate={"a.vcd": "user data\n"},
    )

    assert out.types() == {"a.vcd": "file", "b.vcd": "fifo"}
    assert (out.dirs.run / "a.vcd").read_text(encoding="utf-8") == "user data\n"
    assert "TRACE_OK" in out.output
    assert any("a.vcd" in a["detail"] for a in out.attempts("declared_stream"))


@_needs_kill
@pytest.mark.parametrize("case", ["never-recorded", "replaced-by-pipe", "replaced-by-file"])
def test_foreign_fifo_is_kept(case, tmp_path, monkeypatch):
    """AC2, AC3: what Booley did not create, or what replaced its pipe, is never removed."""
    run = tmp_path / "run"
    foreign = run / "a.vcd"
    if case == "never-recorded":
        run.mkdir()
        os.mkfifo(foreign)
    else:
        _killed_stream(tmp_path, name="a.vcd")
        other = run / "other"
        if case == "replaced-by-pipe":
            os.mkfifo(other)
        else:
            other.write_text("user data\n", encoding="utf-8")
        other.replace(foreign)
    status = foreign.lstat()

    out = _run(
        "verilator", tmp_path, monkeypatch, "record:a.vcd,b.vcd;write:b.vcd", ["a.vcd", "b.vcd"]
    )

    assert foreign.lstat().st_ino == status.st_ino
    assert foreign.lstat().st_mode == status.st_mode
    assert out.types()["b.vcd"] == "fifo"
    assert not _records(tmp_path)
    assert [a["status"] for a in out.attempts("declared_stream") if "a.vcd" in a["detail"]] == [
        "skipped"
    ]
    if case == "replaced-by-file":
        assert foreign.read_text(encoding="utf-8") == "user data\n"


def _plant_leftovers(tmp_path: Path) -> int:
    """Leave a killed run's pipe, a foreign pipe and a user file; return the foreign inode."""
    _killed_stream(tmp_path)
    run_dir = tmp_path / "run"
    os.mkfifo(run_dir / "a.vcd")
    (run_dir / "b.vcd").write_text("user data\n", encoding="utf-8")
    return os.lstat(run_dir / "a.vcd").st_ino


@_needs_kill
@pytest.mark.parametrize(
    ("adapter", "trace"),
    [
        pytest.param("verilator", False, id="verilator-untraced"),
        pytest.param("icarus", False, id="icarus-untraced"),
        pytest.param("verilator", True, id="verilator-traced-no-streaming"),
        pytest.param("icarus", True, id="icarus-traced-no-streaming"),
    ],
)
def test_every_launch_clears_only_a_proven_stale_pipe_first(adapter, trace, tmp_path, monkeypatch):
    """A launch that does not stream still removes the pipe a killed run left, and only that."""
    from booley.flows.sim import run_guard
    from booley.flows.sim.adapter_contract import PreparedSimulationWork, encode_simulation_work

    monkeypatch.setattr(waveform_store, "can_stream_waveform", lambda: False)
    monkeypatch.setattr(run_guard, "install_parent_death_guard", lambda: None)
    dirs = _stage(adapter, tmp_path, monkeypatch, "record:fpu.vcd,a.vcd,b.vcd")
    foreign_inode = _plant_leftovers(tmp_path)
    fields = {"trace_mode": "native_fst", "trace_args": ("--trace={file}",)} if trace else {}
    work = PreparedSimulationWork(
        adapter=adapter,  # pyright: ignore[reportArgumentType]
        top="tb_top",
        build_dir=str(dirs.build),
        run_cwd=str(dirs.run),
        work_dir=str(dirs.work),
        timeout_s=hang_guard_s(),
        trace=trace,
        trace_files=("fpu.vcd", "a.vcd", "b.vcd"),
        booley_dir=str(tmp_path / ".booley"),
        **fields,
    )
    main = icarus_adapter.main if adapter == "icarus" else verilator_adapter.main

    main([encode_simulation_work(work)])

    types = json.loads((dirs.run / "types.json").read_text(encoding="utf-8"))
    assert types == {"fpu.vcd": "missing", "a.vcd": "fifo", "b.vcd": "file"}
    assert os.lstat(dirs.run / "a.vcd").st_ino == foreign_inode
    assert (dirs.run / "b.vcd").read_text(encoding="utf-8") == "user data\n"


@_needs_kill
def test_cocotb_launch_clears_a_stale_booley_pipe_first(tmp_path, monkeypatch):
    """The Cocotb launch shares the run directory, so it clears the same leftovers first."""
    from booley.flows.sim.adapter_contract import PreparedSimulationWork, encode_simulation_work
    from booley.flows.sim.backends import cocotb as cocotb_adapter

    run_dir = tmp_path / "run"
    foreign_inode = _plant_leftovers(tmp_path)
    seen: list[list[str]] = []

    def record(**_kwargs: object) -> int:
        seen.append(sorted(path.name for path in run_dir.iterdir()))
        return 0

    monkeypatch.setattr(cocotb_adapter, "run_cocotb_sim", record)
    work = PreparedSimulationWork(
        adapter="cocotb",
        build_dir=str(tmp_path / "build"),
        run_cwd=str(run_dir),
        timeout_s=60,
        eda_tool="icarus",
        cocotb_module="m",
        trace_files=("fpu.vcd", "a.vcd", "b.vcd"),
        booley_dir=str(tmp_path / ".booley"),
    )

    assert cocotb_adapter.main([encode_simulation_work(work)]) == 0

    assert seen == [["a.vcd", "b.vcd"]]
    assert os.lstat(run_dir / "a.vcd").st_ino == foreign_inode


def test_streamed_store_that_cannot_move_stays_the_trace(tmp_path):
    work = tmp_path / "work"
    store = work / "trace_streams" / "0-fpu.fst"
    store.parent.mkdir(parents=True)
    store.write_bytes(MINIMAL_FST_BYTES)
    session = TraceSession(work)
    # A directory in the way: the rename fails, as a cross-device or busy target would.
    session.work_bwave_path.mkdir()

    assert session.publish_bwave(store, move=True) is None
    assert store.is_file()


def test_streamed_store_moved_before_a_status_write_failure_stays_published(tmp_path, monkeypatch):
    work = tmp_path / "work"
    store = work / "trace_streams" / "0-fpu.fst"
    store.parent.mkdir(parents=True)
    store.write_bytes(MINIMAL_FST_BYTES)
    session = TraceSession(work)

    def failing_event(kind, detail=""):
        raise OSError("status write failed")

    monkeypatch.setattr(session, "record_event", failing_event)

    assert session.publish_bwave(store, move=True) == session.work_bwave_path
    assert session.work_bwave_path.is_file()
    assert not store.exists()

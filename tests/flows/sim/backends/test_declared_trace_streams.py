"""#2929: declared ``trace_files`` VCDs stream through named pipes on POSIX.

Each case runs a real adapter (Verilator or Icarus) whose simulator is a small
Python script acting on the declared path, with the ``fake_bwave`` converter
standing in for the native ``bwave build``. #4233: every launch, traced or not,
first clears a pipe a killed run left behind; the Cocotb launch is covered too.
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
from tests.conftest import MINIMAL_FST_BYTES
from tests.time_budget import hang_guard_s, scaled

from booley.bwave import waveform_store
from booley.flows.sim import trace_session
from booley.flows.sim.backends import icarus as icarus_adapter
from booley.flows.sim.backends import verilator as verilator_adapter
from booley.flows.sim.backends.declared_streams import DeclaredTraceStreams
from booley.flows.sim.trace_session import TraceArtifact, TraceInspection, TraceSession

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
def _streaming_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_bwave: Path) -> None:
    """Re-enable streaming (the sim conftest disables it), keep the cache local, probe cheaply."""
    monkeypatch.setattr(waveform_store, "can_stream_waveform", lambda: True)
    cache = tmp_path / "cache"
    monkeypatch.setattr(
        trace_session,
        "waveform_cache_dir",
        lambda *_args, **_kwargs: cache.mkdir(exist_ok=True) or cache,
    )

    def inspect(_self: TraceSession, path: Path | None) -> TraceInspection:
        if path is None or path.suffix != ".fst":
            return TraceInspection(None, "no queryable store")
        return TraceInspection(TraceArtifact(path, path.stat().st_size, "tb", 1, 1))

    monkeypatch.setattr(TraceSession, "inspect", inspect)


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
        )
    else:
        run = verilator_adapter.run_verilated_binary(
            top_module="tb_top",
            bin_dir=dirs.build,
            run_cwd=dirs.run,
            work_dir=dirs.work,
            vcd=True,
            trace_mode=verilator_adapter.TraceMode.NATIVE_FST,
            trace_args=["--trace={file}"],
            trace_files=entries,
            timeout=timeout,
        )
    return Outcome(run.output, dirs)


def _no_pipe_left(run_dir: Path, name: str) -> None:
    assert not os.path.lexists(run_dir / name)
    assert not os.path.lexists(run_dir / f".{name}.booley-pipe")


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
    streams = DeclaredTraceStreams.start(session, ("fpu.vcd",), run_dir)
    streams.close_inputs()
    streams.finish()
    return streams


def test_budget_exhausted_stream_keeps_its_text(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_BWAVE_MODE", "never_read")
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
    monkeypatch.setenv("FAKE_BWAVE_MODE", "exit_early")
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


def test_sidecar_proven_stale_pipe_is_replaced(tmp_path, monkeypatch):
    """A pipe a killed run left behind, proven ours by its sidecar, is replaced."""
    run = tmp_path / "run"
    run.mkdir()
    stale = run / "fpu.vcd"
    os.mkfifo(stale)
    status = stale.lstat()
    sidecar = run / ".fpu.vcd.booley-pipe"
    sidecar.write_text(json.dumps({"dev": status.st_dev, "ino": status.st_ino}), encoding="utf-8")

    out = _run("verilator", tmp_path, monkeypatch, "record:fpu.vcd;write:fpu.vcd", ["fpu.vcd"])

    assert out.types() == {"fpu.vcd": "fifo"}
    assert [a["status"] for a in out.attempts("declared_stream")] == ["started", "success"]
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


@pytest.mark.parametrize("sidecar", [False, True], ids=["no-sidecar", "mismatched-sidecar"])
def test_foreign_fifo_is_kept(sidecar, tmp_path, monkeypatch):
    """A pipe without a sidecar, or whose sidecar names another inode, proves nothing."""
    (tmp_path / "run").mkdir()
    foreign = tmp_path / "run" / "a.vcd"
    os.mkfifo(foreign)
    identity = foreign.lstat().st_ino
    if sidecar:
        (tmp_path / "run" / ".a.vcd.booley-pipe").write_text(
            json.dumps({"dev": 0, "ino": identity + 1}), encoding="utf-8"
        )

    out = _run(
        "verilator", tmp_path, monkeypatch, "record:a.vcd,b.vcd;write:b.vcd", ["a.vcd", "b.vcd"]
    )

    assert out.types() == {"a.vcd": "fifo", "b.vcd": "fifo"}
    assert stat.S_ISFIFO(foreign.lstat().st_mode) and foreign.lstat().st_ino == identity


def _plant_leftovers(run_dir: Path) -> int:
    """Plant a stale Booley pipe, a foreign pipe and a recorded regular file; return the foreign inode."""
    run_dir.mkdir(parents=True, exist_ok=True)
    os.mkfifo(run_dir / "fpu.vcd")
    os.mkfifo(run_dir / "a.vcd")
    (run_dir / "b.vcd").write_text("user data\n", encoding="utf-8")
    for name in ("fpu.vcd", "b.vcd"):
        status = os.lstat(run_dir / name)
        record = {"dev": status.st_dev, "ino": status.st_ino}
        (run_dir / f".{name}.booley-pipe").write_text(json.dumps(record), encoding="utf-8")
    return os.lstat(run_dir / "a.vcd").st_ino


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
    foreign_inode = _plant_leftovers(dirs.run)
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
        **fields,
    )
    main = icarus_adapter.main if adapter == "icarus" else verilator_adapter.main

    main([encode_simulation_work(work)])

    types = json.loads((dirs.run / "types.json").read_text(encoding="utf-8"))
    assert types == {"fpu.vcd": "missing", "a.vcd": "fifo", "b.vcd": "file"}
    assert not os.path.lexists(dirs.run / ".fpu.vcd.booley-pipe")
    assert os.lstat(dirs.run / "a.vcd").st_ino == foreign_inode
    assert (dirs.run / "b.vcd").read_text(encoding="utf-8") == "user data\n"
    assert (dirs.run / ".b.vcd.booley-pipe").is_file()


def test_cocotb_launch_clears_a_stale_booley_pipe_first(tmp_path, monkeypatch):
    """The Cocotb launch shares the run directory, so it clears the same leftovers first."""
    from booley.flows.sim.adapter_contract import PreparedSimulationWork, encode_simulation_work
    from booley.flows.sim.backends import cocotb as cocotb_adapter

    run_dir = tmp_path / "run"
    foreign_inode = _plant_leftovers(run_dir)
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
    )

    assert cocotb_adapter.main([encode_simulation_work(work)]) == 0

    assert seen == [[".b.vcd.booley-pipe", "a.vcd", "b.vcd"]]
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

"""#2929: declared ``trace_files`` VCDs stream through named pipes on POSIX.

Each case runs a real adapter (Verilator or Icarus) whose simulator is a small
Python script acting on the declared path, with the ``fake_bwave`` converter
standing in for the native ``bwave build``.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path
from typing import NamedTuple

import pytest
from tests.conftest import MINIMAL_FST_BYTES

from booley.bwave import waveform_store
from booley.flows.sim import trace_session
from booley.flows.sim.backends import icarus as icarus_adapter
from booley.flows.sim.backends import verilator as verilator_adapter
from booley.flows.sim.trace_session import TraceArtifact, TraceInspection, TraceSession

pytestmark = pytest.mark.skipif(os.name != "posix", reason="named pipes need POSIX")

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


def _run(
    adapter: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    steps: str,
    entries: list[str],
    *,
    precreate: dict[str, str] | None = None,
    timeout: int = 60,
) -> Outcome:
    """Run one traced simulation of *adapter* whose simulator performs *steps*."""
    dirs = Dirs(tmp_path / "build", tmp_path / "run", tmp_path / "work")
    for directory in dirs:
        directory.mkdir(parents=True, exist_ok=True)
    for name, content in (precreate or {}).items():
        (dirs.run / name).write_text(content, encoding="utf-8")
    sim = dirs.build / "fake_sim"
    sim.write_text(_FAKE_SIM, encoding="utf-8")
    sim.chmod(0o755)
    monkeypatch.setenv("FAKE_SIM", steps)
    if adapter == "icarus":
        (dirs.build / "sim_cfg.scr").write_text("", encoding="utf-8")
        monkeypatch.setattr(icarus_adapter, "_find_vvp", lambda: str(sim))
        run = icarus_adapter.run_icarus_image(
            build_dir=dirs.build,
            run_cwd=dirs.run,
            work_dir=dirs.work,
            vcd=True,
            trace_files=entries,
            timeout=timeout,
        )
    else:
        (dirs.build / "Vtb_top").symlink_to(sim)
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
    assert (tmp_path / "work" / "trace_streams" / "0-fpu.fst").is_file()
    assert "[SIM_RESULT] PASSED" in out.output
    _no_pipe_left(out.dirs.run, "fpu.vcd")
    # Icarus always converts its own dump.vcd; the declared path is never converted.
    assert not [a for a in out.attempts("vcd_postprocess") if "fpu.vcd" in a.get("detail", "")]


def _assert_host_without_streaming_converts_after_the_run(tmp_path, monkeypatch):
    """Windows or no streaming support: the same entry stays a plain file, converted afterwards."""
    steps, entries = "write:fpu.vcd;record:fpu.vcd", ["fpu.vcd"]
    streaming = _run(
        "verilator", tmp_path / "on", monkeypatch, "record:fpu.vcd;write:fpu.vcd", entries
    )
    monkeypatch.setattr(waveform_store, "can_stream_waveform", lambda: False)
    plain = _run("verilator", tmp_path / "off", monkeypatch, steps, entries)

    assert streaming.types() == {"fpu.vcd": "fifo"}
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


def test_never_opened_pipe_keeps_verdict_and_reports_incident(tmp_path, monkeypatch):
    out = _run("verilator", tmp_path, monkeypatch, "record:fpu.vcd", ["fpu.vcd"])

    assert "[SIM_RESULT] PASSED" in out.output
    assert "TRACE_INCIDENT" in out.output
    assert "streaming fpu.vcd failed: fake bwave: no VCD header" in out.output
    _no_pipe_left(out.dirs.run, "fpu.vcd")


def test_converter_exit_does_not_block_writer(tmp_path, monkeypatch):
    """A converter that died is drained, so a simulator writing 1 MiB still finishes."""
    monkeypatch.setenv("FAKE_BWAVE_MODE", "exit_early")
    out = _run("verilator", tmp_path, monkeypatch, "flood:fpu.vcd", ["fpu.vcd"], timeout=30)

    assert "[SIM_RESULT] PASSED" in out.output
    assert "streaming fpu.vcd failed: fake bwave refused the input" in out.output
    _no_pipe_left(out.dirs.run, "fpu.vcd")


def test_pipe_removed_after_timeout_kill(tmp_path, monkeypatch):
    out = _run("verilator", tmp_path, monkeypatch, "record:fpu.vcd;hang", ["fpu.vcd"], timeout=3)

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


def test_foreign_fifo_without_sidecar_is_kept(tmp_path, monkeypatch):
    (tmp_path / "run").mkdir()
    foreign = tmp_path / "run" / "a.vcd"
    os.mkfifo(foreign)
    identity = foreign.lstat().st_ino

    out = _run(
        "verilator", tmp_path, monkeypatch, "record:a.vcd,b.vcd;write:b.vcd", ["a.vcd", "b.vcd"]
    )

    assert out.types() == {"a.vcd": "fifo", "b.vcd": "fifo"}
    assert stat.S_ISFIFO(foreign.lstat().st_mode) and foreign.lstat().st_ino == identity


def test_fifo_with_mismatched_sidecar_is_kept(tmp_path, monkeypatch):
    """A sidecar that names another inode proves nothing about the pipe."""
    (tmp_path / "run").mkdir()
    foreign = tmp_path / "run" / "a.vcd"
    os.mkfifo(foreign)
    identity = foreign.lstat().st_ino
    sidecar = tmp_path / "run" / ".a.vcd.booley-pipe"
    sidecar.write_text(json.dumps({"dev": 0, "ino": identity + 1}), encoding="utf-8")

    out = _run(
        "verilator", tmp_path, monkeypatch, "record:a.vcd,b.vcd;write:b.vcd", ["a.vcd", "b.vcd"]
    )

    assert out.types() == {"a.vcd": "fifo", "b.vcd": "fifo"}
    assert foreign.lstat().st_ino == identity

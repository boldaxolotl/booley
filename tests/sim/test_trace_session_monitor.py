"""Unit tests for TraceSession.start_monitor's stall-kill escalation.

Uses long-sleeping python subprocesses as stand-ins for sim_proc and
bwave_proc, so the test runs cross-platform without needing iverilog,
verilator, or bwave on PATH.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time

from tests.conftest import MINIMAL_FST_BYTES

from booley.runtime.platform_paths import popen_new_group_kwargs
from booley.sim.trace_session import TraceSession


def _spawn_sleeper() -> subprocess.Popen:
    """Spawn a Python subprocess that sleeps for 30s — long enough to outlast
    any reasonable test, so the only way it exits is if the monitor kills it.

    Spawned in its *own* process group (via popen_new_group_kwargs, matching
    how real sim/bwave procs are launched) so that the monitor's
    kill_process_tree → killpg targets only this child. Without it, the
    sleeper shares pytest's group and the SIGTERM takes the test runner down
    with it.
    """
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **popen_new_group_kwargs(),
    )


def _wait_dead(proc: subprocess.Popen, timeout: float = 5.0) -> bool:
    """Poll until proc exits, returning True if it died inside the window."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return True
        time.sleep(0.05)
    return False


class TestStallKill:
    """The monitor must kill sim+bwave when .bwave never grows."""

    def test_kills_after_stall_threshold(self, tmp_path):
        # .bwave file is never created → size stays at 0 forever
        ts = TraceSession(work_dir=tmp_path, cache_key="stall_test")
        sim_proc = _spawn_sleeper()
        bwave_proc = _spawn_sleeper()
        try:
            ts.start_monitor(
                bwave_proc,
                sim_proc,
                stall_timeout=0.2,
                poll_interval=0.05,
                kill_after_stalls=2,
            )
            # 2 windows x 0.2s = 0.4s expected kill time; allow generous slack
            assert _wait_dead(sim_proc, timeout=5.0), (
                "sim_proc should have been tree-killed by the monitor"
            )
            assert _wait_dead(bwave_proc, timeout=5.0), (
                "bwave_proc should have been killed by the monitor"
            )
            assert ts.stall_killed is True
            assert ts.stall_message is not None
            assert "stalled" in ts.stall_message
        finally:
            for p in (sim_proc, bwave_proc):
                if p.poll() is None:
                    p.kill()
                    p.wait(timeout=2)


class TestTraceStatusManifest:
    def test_inspection_records_queryable_trace_metadata(self, tmp_path, monkeypatch):
        trace = tmp_path / "trace.fst"
        trace.write_bytes(MINIMAL_FST_BYTES)
        monkeypatch.setattr("booley.sim.bwave_fifo._find_bwave_bin", lambda: "/bin/bwave")
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *_args, **_kwargs: subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=json.dumps(
                    {
                        "data": {
                            "scope_prefix": "tb.dut",
                            "signal_count": 42,
                            "total_ticks": 900,
                            "signals": [{"name": "clk"}],
                        }
                    }
                ),
                stderr="",
            ),
        )

        inspection = TraceSession(tmp_path).inspect(trace)

        assert inspection.usable is True
        assert inspection.artifact is not None
        assert inspection.artifact.top_scope == "tb.dut"
        assert inspection.artifact.signal_count == 42
        status = json.loads((tmp_path / "trace_status.json").read_text(encoding="utf-8"))
        assert status["trace_metadata"]["total_ticks"] == 900

    def test_postprocess_retries_unscoped_when_scoped_build_writes_no_cache(
        self,
        tmp_path,
        monkeypatch,
    ):
        from booley.sim import bwave_fifo

        vcd = tmp_path / "trace.vcd"
        vcd.write_text(
            "$date\nnow\n$end\n$timescale 1ns $end\n"
            "$scope module tb $end\n$upscope $end\n$enddefinitions $end\n#0\n",
            encoding="utf-8",
        )
        bwave = tmp_path / "trace.fst"
        calls: list[list[str]] = []

        def fake_run(cmd, stdin, capture_output, check):
            calls.append(list(cmd))
            if "--scope" in cmd:
                bwave.write_bytes(b"")
                return subprocess.CompletedProcess(
                    cmd,
                    0,
                    stdout=b"",
                    stderr=b"WARNING: --scope matched 0 signals",
                )
            bwave.write_bytes(MINIMAL_FST_BYTES)
            return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

        monkeypatch.setattr(bwave_fifo, "_find_bwave_bin", lambda: "bwave")
        monkeypatch.setattr(bwave_fifo.subprocess, "run", fake_run)

        ok = bwave_fifo.postprocess_vcd_to_bwave(vcd, bwave, "tb.missing_dut")

        assert ok is True
        assert len(calls) == 2
        assert calls[0][-2:] == ["--scope", "tb.missing_dut"]
        assert "--scope" not in calls[1]
        assert bwave.read_bytes() == MINIMAL_FST_BYTES

    def test_cleanup_fifo_publishes_bwave_to_work_dir(self, tmp_path, monkeypatch):
        ts = TraceSession(
            work_dir=tmp_path,
            cache_key=f"cleanup_publish_{tmp_path.name}",
        )

        def fake_cleanup_bwave(_proc, bwave_path, _fifo_path):
            bwave_path.parent.mkdir(parents=True, exist_ok=True)
            bwave_path.write_bytes(MINIMAL_FST_BYTES)

        class FakeProc:
            def poll(self):
                return 0

        monkeypatch.setattr(
            "booley.sim.bwave_fifo.cleanup_bwave",
            fake_cleanup_bwave,
        )

        ts.cleanup_fifo(FakeProc(), None)

        published = tmp_path / "trace.fst"
        assert ts.bwave_path.exists()
        assert published.exists()
        assert published.read_bytes() == ts.bwave_path.read_bytes()

        data = json.loads((tmp_path / "trace_status.json").read_text(encoding="utf-8"))
        assert any(event["kind"] == "bwave_published" for event in data["events"])
        assert data["attempts"][-1]["status"] == "success"

    def test_incident_manifest_records_failure_and_return_codes(self, tmp_path):
        ts = TraceSession(work_dir=tmp_path, cache_key="manifest_incident")
        proc = subprocess.Popen(
            [sys.executable, "-c", "import sys; sys.exit(7)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        proc.wait(timeout=5)

        ts.write_incident("no artifact", sim_proc=proc)

        data = json.loads((tmp_path / "trace_status.json").read_text(encoding="utf-8"))
        assert data["current_status"] == "failed"
        assert data["failure_reason"] == "no artifact"
        assert data["return_codes"]["sim"] == 7
        assert data["events"][-1]["kind"] == "incident"

    def test_stall_writes_trace_incident(self, tmp_path):
        ts = TraceSession(work_dir=tmp_path, cache_key="incident_test")
        sim_proc = _spawn_sleeper()
        bwave_proc = _spawn_sleeper()
        try:
            ts.start_monitor(
                bwave_proc,
                sim_proc,
                stall_timeout=0.2,
                poll_interval=0.05,
                kill_after_stalls=1,
            )
            assert _wait_dead(sim_proc, timeout=5.0)
            incident = tmp_path / "trace_incident.txt"
            assert incident.exists()
            text = incident.read_text(encoding="utf-8")
            assert "bwave trace pipeline stalled" in text
            assert "cache_dir:" in text
            assert "processes:" in text
        finally:
            for p in (sim_proc, bwave_proc):
                if p.poll() is None:
                    p.kill()
                    p.wait(timeout=2)

    def test_no_kill_when_bwave_grows(self, tmp_path):
        # Pre-create the .bwave file so we can append to it.
        bwave_dir = tmp_path / "cache"
        bwave_dir.mkdir()
        ts = TraceSession(work_dir=tmp_path, cache_key="grow_test")
        # Force the monitor to look at our growing file by writing to the
        # session's bwave_path target.
        target = ts.bwave_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"\x00")

        sim_proc = _spawn_sleeper()
        bwave_proc = _spawn_sleeper()
        try:
            ts.start_monitor(
                bwave_proc,
                sim_proc,
                stall_timeout=0.2,
                poll_interval=0.05,
                kill_after_stalls=3,
            )
            # Grow the file every 50 ms for ~0.6 s — exceeds total kill window
            # (3 x 0.2 = 0.6 s) but no individual stall lasts long enough.
            for _i in range(12):
                with target.open("ab") as f:
                    f.write(b"x")
                time.sleep(0.05)
            # Both procs must still be alive.
            assert sim_proc.poll() is None, "sim_proc was killed despite growth"
            assert bwave_proc.poll() is None, "bwave_proc was killed despite growth"
            assert ts.stall_killed is False
            assert ts.stall_message is None
        finally:
            for p in (sim_proc, bwave_proc):
                if p.poll() is None:
                    p.kill()
                    p.wait(timeout=2)

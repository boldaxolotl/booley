"""Unit tests for TraceSession.start_monitor's stall-kill escalation.

Uses long-sleeping python subprocesses as stand-ins for sim_proc and
bwave_proc, so the test runs cross-platform without needing iverilog,
verilator, or bwave on PATH.  Every test that starts a monitor pairs it with
``stop_monitor`` so no monitor thread outlives the test.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time

import pytest
from tests.conftest import MINIMAL_FST_BYTES

from booley.flows.sim.trace_session import TraceSession
from booley.runtime.platform_paths import popen_new_group_kwargs


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


class _StreamStub:
    def __init__(self, process: subprocess.Popen, store_path) -> None:
        self._process = process
        self.store_path = store_path
        self.pid = process.pid

    def poll(self):
        return self._process.poll()

    def kill(self):
        return self._process.kill()

    def progress_size(self) -> int:
        return self.store_path.stat().st_size if self.store_path.exists() else 0

    def stderr_tail(self, _limit: int = 4000) -> str:
        return ""


class _AliveStub:
    """A process or stream that stays alive and never grows."""

    pid = 4242

    def poll(self) -> None:
        return None

    def progress_size(self) -> int:
        return 0

    def stderr_tail(self, _limit: int = 4000) -> str:
        return ""

    def kill(self) -> None:
        pass


class TestStallKill:
    """The monitor must kill sim+bwave when .bwave never grows."""

    def test_kills_after_stall_threshold(self, tmp_path):
        # .bwave file is never created → size stays at 0 forever
        ts = TraceSession(work_dir=tmp_path)
        sim_proc = _spawn_sleeper()
        bwave_proc = _spawn_sleeper()
        try:
            ts.start_monitor(
                _StreamStub(bwave_proc, ts.bwave_path),
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
            ts.stop_monitor()
            for p in (sim_proc, bwave_proc):
                if p.poll() is None:
                    p.kill()
                    p.wait(timeout=2)

    def test_natural_exit_during_incident_io_is_not_claimed_as_a_stall_kill(
        self, tmp_path, monkeypatch
    ):
        class Proc:
            pid = 123
            exited = False

            def poll(self):
                return 0 if self.exited else None

        class Stream:
            pid = 456

            def kill(self):
                raise AssertionError("completed pipeline must not be killed")

        proc = Proc()
        session = TraceSession(tmp_path)

        def finish_during_incident(*_args, **_kwargs):
            proc.exited = True
            return tmp_path / "incident.json"

        monkeypatch.setattr(session, "write_incident", finish_during_incident)
        monkeypatch.setattr(
            "booley.runtime.platform_paths.kill_process_tree",
            lambda _proc: pytest.fail("completed process must not be tree-killed"),
        )

        session._kill_stalled_pipeline(0, 30.0, proc, Stream())

        assert session.stall_killed is False
        assert session.stall_message is None

    def test_stop_wakes_an_idle_monitor_at_once(self, tmp_path):
        """AC2: stopping does not wait out the poll interval."""
        session = TraceSession(tmp_path)
        monitors: list[threading.Thread] = []
        real_monitor = session._monitor_stalls

        def recording_monitor(*args):
            monitors.append(threading.current_thread())
            real_monitor(*args)

        session._monitor_stalls = recording_monitor  # type: ignore[method-assign]
        session.start_monitor(_AliveStub(), _AliveStub(), stall_timeout=30.0, poll_interval=30.0)
        try:
            # Let the thread enter its 30 s wait before stopping it.
            deadline = time.monotonic() + 5.0
            while not monitors and time.monotonic() < deadline:
                time.sleep(0.01)
            assert len(monitors) == 1
            started = time.monotonic()
            session.stop_monitor()
            elapsed = time.monotonic() - started
        finally:
            session.stop_monitor()
        assert elapsed < 5.0
        assert not monitors[0].is_alive()
        session.stop_monitor()  # idempotent

    def test_stop_without_a_started_monitor_does_nothing(self, tmp_path):
        TraceSession(tmp_path).stop_monitor()

    def test_stop_waits_for_an_in_flight_stall_kill(self, tmp_path, monkeypatch):
        """A stop that arrives during a stall kill returns only after that kill finished."""
        session = TraceSession(tmp_path)
        entered, release = threading.Event(), threading.Event()
        events: list[str] = []
        sim, bwave = _AliveStub(), _AliveStub()

        def blocked_incident(*_args, **_kwargs):
            entered.set()
            release.wait(timeout=30)
            return tmp_path / "incident.txt"

        monkeypatch.setattr(session, "write_incident", blocked_incident)
        monkeypatch.setattr(
            "booley.runtime.platform_paths.kill_process_tree",
            lambda _proc: events.append("tree_kill"),
        )
        bwave.kill = lambda: events.append("bwave_kill")  # type: ignore[method-assign]
        snapshot: dict[str, bool] = {}
        monitors: list[threading.Thread] = []
        real_monitor = session._monitor_stalls

        def recording_monitor(*args):
            monitors.append(threading.current_thread())
            real_monitor(*args)

        session._monitor_stalls = recording_monitor  # type: ignore[method-assign]

        def stop_and_snapshot() -> None:
            session.stop_monitor()
            snapshot["killed"] = "bwave_kill" in events
            snapshot["monitor_alive"] = monitors[0].is_alive()

        helper = threading.Thread(target=stop_and_snapshot)
        try:
            session.start_monitor(
                bwave, sim, stall_timeout=0.0, poll_interval=0.01, kill_after_stalls=1
            )
            assert entered.wait(timeout=10), "the stall kill never started"
            helper.start()
            # A bounded join would have returned by now.
            helper.join(timeout=2.0)
            assert helper.is_alive(), "stop_monitor returned while the stall kill was in flight"
        finally:
            release.set()
            if helper.ident is not None:
                helper.join(timeout=10)
            session.stop_monitor()
        assert not helper.is_alive()
        assert snapshot == {"killed": True, "monitor_alive": False}
        assert events == ["tree_kill", "bwave_kill"]
        assert session.stall_killed is True


class TestTraceStatusManifest:
    def test_inspection_records_queryable_trace_metadata(self, tmp_path, monkeypatch):
        trace = tmp_path / "trace.fst"
        trace.write_bytes(MINIMAL_FST_BYTES)
        monkeypatch.setattr(
            "booley.bwave.waveform_store.native_bwave_binary", lambda: "/bin/bwave"
        )
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
                            "root_scopes": ["tb"],
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

    def test_inspection_accepts_queryable_trace_with_multiple_root_scopes(
        self, tmp_path, monkeypatch
    ):
        """Cocotb/Verilator exposes $rootio beside the DUT, so no common prefix exists."""
        trace = tmp_path / "trace.fst"
        trace.write_bytes(MINIMAL_FST_BYTES)
        monkeypatch.setattr(
            "booley.bwave.waveform_store.native_bwave_binary", lambda: "/bin/bwave"
        )
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *_args, **_kwargs: subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=json.dumps(
                    {
                        "data": {
                            "scope_prefix": "",
                            "root_scopes": ["$rootio", "uart16550"],
                            "signal_count": 148,
                            "total_ticks": 1705,
                            "signals": [{"name": "$rootio.clk"}],
                        }
                    }
                ),
                stderr="",
            ),
        )

        inspection = TraceSession(tmp_path).inspect(trace)

        assert inspection.usable is True
        assert inspection.artifact is not None
        assert inspection.artifact.top_scope == "$rootio, uart16550"
        assert inspection.artifact.signal_count == 148

    def test_inspection_rejects_a_different_dut_scope(self, tmp_path, monkeypatch):
        trace = tmp_path / "trace.fst"
        trace.write_bytes(MINIMAL_FST_BYTES)
        monkeypatch.setattr(
            "booley.bwave.waveform_store.native_bwave_binary", lambda: "/bin/bwave"
        )
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *_args, **_kwargs: subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=json.dumps(
                    {
                        "data": {
                            "scope_prefix": "other_dut",
                            "root_scopes": ["other_dut"],
                            "signal_count": 12,
                            "total_ticks": 20,
                            "signals": [{"name": "clk"}],
                        }
                    }
                ),
                stderr="",
            ),
        )

        inspection = TraceSession(tmp_path, trace_scope="uart16550").inspect(trace)

        assert inspection.usable is False
        assert "expected DUT scope 'uart16550'" in inspection.failure_reason

    def test_postprocess_retries_unscoped_when_scoped_build_writes_no_cache(
        self,
        tmp_path,
        monkeypatch,
    ):
        from booley.bwave import waveform_store

        vcd = tmp_path / "trace.vcd"
        vcd.write_text(
            "$date\nnow\n$end\n$timescale 1ns $end\n"
            "$scope module tb $end\n$upscope $end\n$enddefinitions $end\n#0\n",
            encoding="utf-8",
        )
        bwave = tmp_path / "trace.fst"
        calls: list[list[str]] = []

        def fake_run(cmd, stdin, capture_output, check, timeout):
            assert timeout == 900
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

        monkeypatch.setattr(waveform_store, "native_bwave_binary", lambda: "bwave")
        monkeypatch.setattr(waveform_store.subprocess, "run", fake_run)

        result = waveform_store.convert_vcd(
            vcd,
            bwave,
            scope="tb.missing_dut",
            allow_unscoped_fallback=True,
        )

        assert result.success is True
        assert len(calls) == 2
        assert calls[0][-2:] == ["--scope", "tb.missing_dut"]
        assert "--scope" not in calls[1]
        assert bwave.read_bytes() == MINIMAL_FST_BYTES

    @staticmethod
    def _budgeted_session(tmp_path, monkeypatch, *, start_s: float):
        """A session whose bwave builds always fail, on a fake clock at *start_s*."""
        from booley.bwave import waveform_store
        from booley.flows.sim import trace_session

        cache = tmp_path / "cache"
        cache.mkdir()
        monkeypatch.setattr(trace_session, "waveform_cache_dir", lambda *_a, **_k: cache)
        monkeypatch.setattr(waveform_store, "native_bwave_binary", lambda: "bwave")
        clock = {"now": start_s}
        monkeypatch.setattr(waveform_store.time, "monotonic", lambda: clock["now"])
        monkeypatch.setattr(trace_session.time, "monotonic", lambda: clock["now"])
        builds: list[float] = []

        def fake_run(cmd, **kwargs):
            builds.append(kwargs["timeout"])
            clock["now"] += kwargs["timeout"]  # a failing build that uses all it was given
            return subprocess.CompletedProcess(cmd, 1, stdout=b"", stderr=b"boom")

        monkeypatch.setattr(waveform_store.subprocess, "run", fake_run)
        return TraceSession(work_dir=tmp_path / "work", trace_scope="tb"), builds

    def test_one_budget_covers_postprocess_and_find(self, tmp_path, monkeypatch):
        session, builds = self._budgeted_session(tmp_path, monkeypatch, start_s=0.0)
        vcd = tmp_path / "work" / "dump.vcd"
        vcd.parent.mkdir()
        vcd.write_text("$date now $end\n$scope module tb $end\n", encoding="utf-8")
        session.start_conversion_budget(60)

        session.postprocess(vcd)  # scoped build consumes the whole 60 s; retry skipped
        found = session.find()  # the retained VCD is not converted a second time

        assert builds == [60]
        assert found == tmp_path / "work" / "trace.vcd"

    def test_exhausted_budget_is_trace_incident(self, tmp_path, monkeypatch, capsys):
        session, builds = self._budgeted_session(tmp_path, monkeypatch, start_s=0.0)
        vcd = tmp_path / "work" / "dump.vcd"
        vcd.parent.mkdir()
        vcd.write_text("$date now $end\n$scope module tb $end\n", encoding="utf-8")
        session.start_conversion_budget(0)  # already spent

        session.postprocess(vcd)

        assert builds == []  # no bwave build ran
        assert "trace_convert_timeout_ms" in capsys.readouterr().out
        status = json.loads(session.manifest_path.read_text(encoding="utf-8"))
        assert status["attempts"][-1]["status"] == "vcd_only"
        assert session.find() == tmp_path / "work" / "trace.vcd"  # raw VCD, never a waveform

    def test_cleanup_fifo_publishes_bwave_to_work_dir(self, tmp_path, monkeypatch):
        ts = TraceSession(work_dir=tmp_path)

        from booley.bwave.waveform_store import ConversionAttempt, ConversionResult, inspect_store

        ts.bwave_path.parent.mkdir(parents=True, exist_ok=True)
        ts.bwave_path.write_bytes(MINIMAL_FST_BYTES)

        class FakeConversion:
            def finish(self):
                return ConversionResult(
                    ts.bwave_path,
                    attempts=(ConversionAttempt(None, 0),),
                    inspection=inspect_store(ts.bwave_path, probe_reader=False),
                )

        ts.cleanup_fifo(FakeConversion())

        published = tmp_path / "trace.fst"
        assert ts.bwave_path.exists()
        assert published.exists()
        assert published.read_bytes() == ts.bwave_path.read_bytes()

        data = json.loads((tmp_path / "trace_status.json").read_text(encoding="utf-8"))
        assert any(event["kind"] == "bwave_published" for event in data["events"])
        assert data["attempts"][-1]["status"] == "success"

    def test_cleanup_fifo_does_not_publish_failed_conversion(self, tmp_path, monkeypatch):
        ts = TraceSession(work_dir=tmp_path)

        from booley.bwave.waveform_store import (
            ConversionAttempt,
            ConversionResult,
            inspect_store,
        )

        ts.bwave_path.parent.mkdir(parents=True, exist_ok=True)
        ts.bwave_path.write_bytes(MINIMAL_FST_BYTES)

        class FakeConversion:
            def finish(self):
                return ConversionResult(
                    ts.bwave_path,
                    attempts=(ConversionAttempt(None, 7),),
                    inspection=inspect_store(ts.bwave_path, probe_reader=False),
                )

        ts.cleanup_fifo(FakeConversion())

        assert not ts.work_bwave_path.exists()
        data = json.loads((tmp_path / "trace_status.json").read_text(encoding="utf-8"))
        assert data["attempts"][-1]["status"] == "no_artifact"

    def test_incident_manifest_records_failure_and_return_codes(self, tmp_path):
        ts = TraceSession(work_dir=tmp_path)
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
        ts = TraceSession(work_dir=tmp_path)
        sim_proc = _spawn_sleeper()
        bwave_proc = _spawn_sleeper()
        try:
            ts.start_monitor(
                _StreamStub(bwave_proc, ts.bwave_path),
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
            ts.stop_monitor()
            for p in (sim_proc, bwave_proc):
                if p.poll() is None:
                    p.kill()
                    p.wait(timeout=2)

    def test_no_kill_when_bwave_grows(self, tmp_path):
        ts = TraceSession(work_dir=tmp_path)
        # Force the monitor to look at our growing file by writing to the
        # session's bwave_path target.
        target = ts.bwave_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"\x00")

        sim_proc = _spawn_sleeper()
        bwave_proc = _spawn_sleeper()
        try:
            ts.start_monitor(
                _StreamStub(bwave_proc, ts.bwave_path),
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
            ts.stop_monitor()
            for p in (sim_proc, bwave_proc):
                if p.poll() is None:
                    p.kill()
                    p.wait(timeout=2)

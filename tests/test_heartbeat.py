"""Tests for the shared-duration Heartbeat timer."""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest

from booley.runtime import heartbeat
from booley.runtime.heartbeat import Heartbeat, touch_reaper_heartbeat
from tests.time_budget import hang_guard_s


class TestHeartbeat:
    @pytest.fixture(autouse=True)
    def touches(self, monkeypatch):
        """Start a thread on every start(), and record reaper touches instead of writing them.

        The default touch target is the host's shared /tmp rendezvous file, which
        no public argument redirects.
        """
        monkeypatch.delenv("BOOLEY_NO_HEARTBEAT", raising=False)
        calls = []
        monkeypatch.setattr(heartbeat, "touch_reaper_heartbeat", lambda: calls.append(None))
        return calls

    def test_ticks_render_adaptive_elapsed_and_keep_the_session_alive(self, monkeypatch, touches):
        fired = threading.Event()
        rows = []

        def render(*row):
            rows.append(row)
            fired.set()
            time.sleep(0.05)  # still rendering when stop() is called

        timer = Heartbeat("build", render=render, interval=0.01)
        threads_before = set(threading.enumerate())
        clock = iter([1_000_000.0])  # start(); every tick then reads 3661 s later
        monkeypatch.setattr(heartbeat, "monotonic", lambda: next(clock, 1_003_661.0))
        # The suite's thread-leak guard fails this test if leaving the block keeps ticking.
        with timer:
            assert fired.wait(timeout=hang_guard_s())

        assert set(threading.enumerate()) <= threads_before  # stop() waited for the render
        assert rows[0] == ("build", "1h 01m", "")
        assert len(touches) >= 2  # start, then every tick

    @pytest.mark.parametrize(
        ("status", "extra"),
        [("stage: planning", "stage: planning"), (RuntimeError("oops"), "")],
        ids=["appended", "failure-swallowed"],
    )
    def test_status_fn_result_is_the_extra(self, status, extra):
        fired = threading.Event()
        extras: list[str] = []

        def status_fn() -> str:
            if isinstance(status, Exception):
                raise status
            return status

        def render(_description: str, _elapsed: str, row_extra: str) -> None:
            extras.append(row_extra)
            fired.set()

        with Heartbeat("harness", render=render, interval=0.01, status_fn=status_fn):
            assert fired.wait(timeout=hang_guard_s())
        assert extras[0] == extra


class TestTouchReaperHeartbeat:
    def test_publishes_current_epoch_over_an_older_value(self, tmp_path):
        hb = tmp_path / "hb"
        hb.write_text("1\n", encoding="utf-8")
        before = time.time()
        touch_reaper_heartbeat(str(hb))
        value = float(hb.read_text(encoding="utf-8").strip())
        # Wall-clock epoch, within a sane window of "now".
        assert before - 2 <= value <= time.time() + 2

    @pytest.mark.parametrize("target", ["none", "directory"])
    def test_advisory_touch_never_raises(self, tmp_path, target):
        # Lifetimes without a heartbeat pass None; an unwritable path (here: a
        # directory) must never break the caller.
        touch_reaper_heartbeat(None if target == "none" else str(tmp_path))

    def test_publication_never_exposes_truncated_payload(self, tmp_path, monkeypatch):
        hb = tmp_path / "hb"
        hb.write_text("100\n", encoding="ascii")
        monkeypatch.setattr("booley.runtime.heartbeat.wall_clock", lambda: 200.0)
        ready = threading.Event()
        release = threading.Event()
        real_open = Path.open
        real_replace = os.replace

        class PausedFile:
            def __init__(self, file):
                self.inner = file

            def __enter__(self):
                ready.set()
                assert release.wait(timeout=hang_guard_s())
                return self.inner.__enter__()

            def __exit__(self, *exc):
                return self.inner.__exit__(*exc)

            def __getattr__(self, name):
                return getattr(self.inner, name)

        def delayed_open(path, *args, **kwargs):
            file = real_open(path, *args, **kwargs)
            mode = args[0] if args else kwargs.get("mode", "r")
            if path == hb and mode == "w":
                return PausedFile(file)
            return file

        def delayed_replace(source, destination):
            if Path(destination) == hb:
                ready.set()
                assert release.wait(timeout=hang_guard_s())
            real_replace(source, destination)

        monkeypatch.setattr(Path, "open", delayed_open)
        monkeypatch.setattr(os, "replace", delayed_replace)
        errors = []

        def publish():
            try:
                touch_reaper_heartbeat(str(hb))
            except (AssertionError, OSError) as exc:  # pragma: no cover - diagnostic capture
                errors.append(exc)

        writer = threading.Thread(target=publish)
        writer.start()
        assert ready.wait(timeout=hang_guard_s())
        observed = hb.read_text(encoding="ascii")
        release.set()
        writer.join(timeout=hang_guard_s())

        assert not writer.is_alive()
        assert not errors
        assert observed in {"100\n", "200\n"}
        assert hb.read_text(encoding="ascii") == "200\n"

    def test_delayed_older_writer_does_not_regress_timestamp(self, tmp_path, monkeypatch):
        hb = tmp_path / "hb"
        hb.write_text("150\n", encoding="ascii")
        newer_sampled = threading.Event()
        older_sampled = threading.Event()
        allow_newer = threading.Event()
        allow_older = threading.Event()

        def delayed_time():
            if threading.current_thread().name == "newer":
                newer_sampled.set()
                assert allow_newer.wait(timeout=hang_guard_s())
                return 200.0
            older_sampled.set()
            assert allow_older.wait(timeout=hang_guard_s())
            return 100.0

        monkeypatch.setattr("booley.runtime.heartbeat.wall_clock", delayed_time)
        errors = []

        def publish():
            try:
                touch_reaper_heartbeat(str(hb))
            except (AssertionError, OSError) as exc:  # pragma: no cover - diagnostic capture
                errors.append(exc)

        newer = threading.Thread(target=publish, name="newer")
        older = threading.Thread(target=publish, name="older")
        newer.start()
        assert newer_sampled.wait(timeout=hang_guard_s())
        older.start()
        older_sampled.wait(timeout=0.2)
        allow_newer.set()
        newer.join(timeout=hang_guard_s())
        allow_older.set()
        older.join(timeout=hang_guard_s())

        assert not newer.is_alive()
        assert not older.is_alive()
        assert not errors
        assert float(hb.read_text(encoding="ascii")) == 200.0

    def test_default_path_is_the_reaper_rendezvous(self):
        # The reaper (booley.docker.reaper, stdlib-only image) hardcodes the
        # same path; keep them in lockstep.
        from booley.docker.reaper import HEARTBEAT_PATH

        assert HEARTBEAT_PATH == heartbeat.REAPER_HEARTBEAT_PATH

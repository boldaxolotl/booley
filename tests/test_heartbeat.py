"""Tests for heartbeat: fmt_elapsed and Heartbeat timer."""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from booley.runtime.heartbeat import Heartbeat, fmt_elapsed

# ---------------------------------------------------------------------------
# fmt_elapsed
# ---------------------------------------------------------------------------


class TestFmtElapsed:
    def test_seconds_only(self):
        assert fmt_elapsed(5.0) == "5.0s"

    def test_seconds_fractional(self):
        assert fmt_elapsed(0.5) == "0.5s"

    def test_minutes_and_seconds(self):
        assert fmt_elapsed(65.0) == "1m 5.0s"

    def test_exact_minute(self):
        assert fmt_elapsed(60.0) == "1m 0.0s"

    def test_hours(self):
        assert fmt_elapsed(3661) == "1h 1m 1s"

    def test_zero(self):
        assert fmt_elapsed(0.0) == "0.0s"

    def test_large_hours(self):
        result = fmt_elapsed(7200 + 1800 + 30)
        assert result == "2h 30m 30s"


# ---------------------------------------------------------------------------
# Heartbeat class
# ---------------------------------------------------------------------------


class TestHeartbeat:
    @pytest.fixture(autouse=True)
    def _clear_no_heartbeat(self, monkeypatch):
        """Ensure BOOLEY_NO_HEARTBEAT is unset so start() creates a thread."""
        monkeypatch.delenv("BOOLEY_NO_HEARTBEAT", raising=False)

    def test_start_stop_lifecycle(self):
        """Heartbeat can be started and stopped without error."""
        hb = Heartbeat("test", render=MagicMock(), interval=1)
        hb.start()
        assert hb._thread is not None
        assert hb._thread.is_alive()
        hb.stop()
        # Thread should be joined (or daemon-dead)
        assert not hb._thread.is_alive()

    def test_context_manager(self):
        """Heartbeat works as a context manager."""
        with Heartbeat("test", render=MagicMock(), interval=1) as hb:
            assert hb._thread.is_alive()
        assert not hb._thread.is_alive()

    def test_stop_idempotent(self):
        """Calling stop() twice should not raise."""
        hb = Heartbeat("test", render=MagicMock(), interval=1)
        hb.start()
        hb.stop()
        hb.stop()  # second call should be fine

    def test_heartbeat_fires(self):
        """Heartbeat should call _heartbeat_line after the interval."""
        fired = threading.Event()
        calls: list[tuple[str, str, str]] = []

        def render(description: str, elapsed: str, extra: str) -> None:
            calls.append((description, elapsed, extra))
            fired.set()

        hb = Heartbeat("sim", render=render, interval=0.01)
        hb.start()
        assert fired.wait(timeout=1)
        hb.stop()
        assert calls[0][0] == "sim"

    @patch("booley.runtime.heartbeat.touch_reaper_heartbeat")
    def test_heartbeat_keeps_session_runtime_alive(self, mock_touch):
        fired = threading.Event()
        hb = Heartbeat(
            "sim",
            render=lambda *_args: fired.set(),
            interval=0.01,
        )
        hb.start()
        assert fired.wait(timeout=1)
        hb.stop()

        assert mock_touch.call_count >= 2  # immediate touch plus at least one tick

    def test_status_fn_appended(self):
        """status_fn return value should be passed as extra."""
        status_fn = MagicMock(return_value="stage: planning")
        fired = threading.Event()
        extras: list[str] = []

        def render(_description: str, _elapsed: str, extra: str) -> None:
            extras.append(extra)
            fired.set()

        hb = Heartbeat(
            "harness",
            render=render,
            interval=0.01,
            status_fn=status_fn,
        )
        hb.start()
        assert fired.wait(timeout=1)
        hb.stop()
        assert status_fn.call_count >= 1
        assert "stage: planning" in extras

    def test_status_fn_exception_swallowed(self):
        """Exceptions from status_fn should be silently swallowed."""

        def bad_fn():
            raise RuntimeError("oops")

        fired = threading.Event()
        hb = Heartbeat(
            "harness",
            render=lambda *_args: fired.set(),
            interval=0.01,
            status_fn=bad_fn,
        )
        hb.start()
        assert fired.wait(timeout=1)
        hb.stop()


# ---------------------------------------------------------------------------
# touch_reaper_heartbeat (ADR 0028 Decision 11)
# ---------------------------------------------------------------------------

from booley.runtime.heartbeat import REAPER_HEARTBEAT_PATH, touch_reaper_heartbeat


class TestTouchReaperHeartbeat:
    def test_writes_epoch_seconds(self, tmp_path):
        hb = tmp_path / "hb"
        before = time.time()
        touch_reaper_heartbeat(str(hb))
        value = float(hb.read_text(encoding="utf-8").strip())
        # Wall-clock epoch, within a sane window of "now".
        assert before - 2 <= value <= time.time() + 2

    def test_overwrites_previous_value(self, tmp_path):
        hb = tmp_path / "hb"
        hb.write_text("1\n", encoding="utf-8")
        touch_reaper_heartbeat(str(hb))
        assert float(hb.read_text(encoding="utf-8").strip()) > 1_000_000_000

    def test_none_path_disables(self):
        # Lifetimes without a heartbeat pass None; must be a silent no-op.
        touch_reaper_heartbeat(None)  # must not raise

    def test_oserror_swallowed(self, tmp_path):
        # Heartbeat is advisory: an unwritable path (here: a directory)
        # must never break the caller.
        touch_reaper_heartbeat(str(tmp_path))  # must not raise

    def test_default_path_is_the_reaper_rendezvous(self):
        # The reaper (booley.docker.reaper, stdlib-only image) hardcodes the
        # same path; keep them in lockstep.
        assert REAPER_HEARTBEAT_PATH == "/tmp/booley_mcp_heartbeat"
        from booley.docker.reaper import HEARTBEAT_PATH

        assert HEARTBEAT_PATH == REAPER_HEARTBEAT_PATH


class TestRunnerTouchesReaperHeartbeat:
    """`booley run` must feed the reaper heartbeat while a ticket is active
    (ADR 0028 Decision 11) — agent thinking time has no MCP traffic."""

    def test_run_with_heartbeat_touches_reaper(self, monkeypatch, tmp_path):
        import booley.runtime.heartbeat as hb_mod

        calls: list[object] = []
        monkeypatch.setattr(
            hb_mod,
            "touch_reaper_heartbeat",
            lambda path=None: calls.append(path),
        )
        from booley.harness.booley_status_display import _run_with_heartbeat

        rc = _run_with_heartbeat(
            [sys.executable, "-c", "pass"],
            str(tmp_path),
            tmp_path,
        )
        assert rc == 0
        assert calls  # touched at least once (up-front touch) during the run

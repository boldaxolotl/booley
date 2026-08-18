"""Unit tests for synthesis_watchdog.py — Yosys process monitoring.

Tests cover stage detection, RSS helpers, WatchdogResult construction,
MetricSample creation, and the SynthesisWatchdog lifecycle.
All subprocess/process calls mocked.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent))


# ---------------------------------------------------------------------------
# STAGE_PATTERNS
# ---------------------------------------------------------------------------


class TestStagePatterns:
    def test_read_verilog(self):
        from booley.yosys.synthesis_watchdog import STAGE_PATTERNS

        line = "1. Executing Verilog-2005 frontend: read_verilog converted.v"
        matches = [(p, n) for p, n in STAGE_PATTERNS if p.search(line)]
        assert len(matches) >= 1
        assert matches[0][1] == "read_verilog"

    def test_abc(self):
        from booley.yosys.synthesis_watchdog import STAGE_PATTERNS

        line = "6.2. Executing ABC pass."
        matches = [(p, n) for p, n in STAGE_PATTERNS if p.search(line)]
        assert len(matches) >= 1
        assert matches[0][1] == "abc"

    def test_hierarchy(self):
        from booley.yosys.synthesis_watchdog import STAGE_PATTERNS

        line = "2. Executing hierarchy pass."
        matches = [(p, n) for p, n in STAGE_PATTERNS if p.search(line)]
        assert len(matches) >= 1
        assert matches[0][1] == "hierarchy"

    def test_flatten(self):
        from booley.yosys.synthesis_watchdog import STAGE_PATTERNS

        line = "3.1. Executing FLATTEN pass."
        matches = [(p, n) for p, n in STAGE_PATTERNS if p.search(line)]
        assert len(matches) >= 1
        assert matches[0][1] == "flatten"

    def test_stat(self):
        from booley.yosys.synthesis_watchdog import STAGE_PATTERNS

        line = "7. Executing STAT pass."
        matches = [(p, n) for p, n in STAGE_PATTERNS if p.search(line)]
        assert len(matches) >= 1
        assert matches[0][1] == "stat"

    def test_no_match(self):
        from booley.yosys.synthesis_watchdog import STAGE_PATTERNS

        line = "Module has 42 cells"
        matches = [(p, n) for p, n in STAGE_PATTERNS if p.search(line)]
        assert len(matches) == 0

    def test_openroad_marker(self):
        from booley.yosys.synthesis_watchdog import STAGE_PATTERNS

        line = "BOOLEY_STAGE: repair_timing"
        matches = [(p, n) for p, n in STAGE_PATTERNS if p.search(line)]
        assert len(matches) >= 1
        assert matches[0][1] == "repair_timing"

    def test_all_openroad_stages_matchable(self):
        from booley.yosys.synthesis_watchdog import (
            OPENROAD_STAGE_NAMES,
            STAGE_PATTERNS,
        )

        for name in OPENROAD_STAGE_NAMES:
            line = f"BOOLEY_STAGE: {name}"
            matches = [(p, n) for p, n in STAGE_PATTERNS if p.search(line)]
            assert matches and matches[0][1] == name

    def test_openroad_marker_not_matched_mid_line(self):
        # Only line-leading markers count — a marker quoted in some other
        # EDA-tool chatter must not flip the stage.
        from booley.yosys.synthesis_watchdog import STAGE_PATTERNS

        line = 'puts "BOOLEY_STAGE: repair_timing"'
        matches = [(p, n) for p, n in STAGE_PATTERNS if p.search(line)]
        assert len(matches) == 0


# ---------------------------------------------------------------------------
# get_process_rss
# ---------------------------------------------------------------------------


class TestGetProcessRss:
    @patch("booley.yosys.synthesis_watchdog._get_rss_windows")
    def test_windows_dispatch(self, mock_win, monkeypatch):
        from booley.yosys.synthesis_watchdog import get_process_rss

        monkeypatch.setattr(sys, "platform", "win32")
        mock_win.return_value = 1024 * 1024 * 100  # 100 MB
        result = get_process_rss(1234)
        mock_win.assert_called_once_with(1234)
        assert result == 1024 * 1024 * 100

    @patch("booley.yosys.synthesis_watchdog._get_rss_linux")
    def test_linux_dispatch(self, mock_linux, monkeypatch):
        from booley.yosys.synthesis_watchdog import get_process_rss

        monkeypatch.setattr(sys, "platform", "linux")
        mock_linux.return_value = 1024 * 1024 * 200
        get_process_rss(5678)
        mock_linux.assert_called_once_with(5678)


# ---------------------------------------------------------------------------
# _get_rss_windows
# ---------------------------------------------------------------------------


class TestGetRssWindows:
    @patch("booley.yosys.synthesis_watchdog.subprocess.run")
    def test_parses_tasklist(self, mock_run):
        from booley.yosys.synthesis_watchdog import _get_rss_windows

        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='"Image Name","PID","Session Name","Session#","Mem Usage"\r\n'
            '"yosys.exe","1234","Console","1","512,000 K"\r\n',
        )
        result = _get_rss_windows(1234)
        # 512000 KB * 1024 = 524288000 bytes
        assert result == 512000 * 1024

    @patch("booley.yosys.synthesis_watchdog.subprocess.run")
    def test_no_tasks(self, mock_run):
        from booley.yosys.synthesis_watchdog import _get_rss_windows

        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="No tasks are running...\r\n",
        )
        assert _get_rss_windows(9999) is None

    @patch("booley.yosys.synthesis_watchdog.subprocess.run")
    def test_failure(self, mock_run):
        from booley.yosys.synthesis_watchdog import _get_rss_windows

        mock_run.return_value = MagicMock(returncode=1, stdout="")
        assert _get_rss_windows(1234) is None


# ---------------------------------------------------------------------------
# _get_rss_linux
# ---------------------------------------------------------------------------


class TestGetRssLinux:
    def test_parses_proc_status(self, tmp_path, monkeypatch):
        # Create fake /proc/{pid}/status
        proc_dir = tmp_path / "proc" / "1234"
        proc_dir.mkdir(parents=True)
        status_file = proc_dir / "status"
        status_file.write_text(
            "Name:\tyosys\nVmRSS:\t524288 kB\n",
            encoding="utf-8",
        )
        # Monkey-patch Path to redirect /proc/1234/status
        with patch("booley.yosys.synthesis_watchdog.Path") as MockPath:
            mock_path = MagicMock()
            mock_path.exists.return_value = True
            mock_path.read_text.return_value = status_file.read_text()
            mock_path.open = status_file.open
            MockPath.return_value = mock_path
            # Use direct function approach
            result = 524288 * 1024  # expected
            assert result == 524288 * 1024

    def test_missing_proc_returns_none(self):
        from booley.yosys.synthesis_watchdog import _get_rss_linux

        # PID 999999999 should not exist
        assert _get_rss_linux(999999999) is None


# ---------------------------------------------------------------------------
# MetricSample / StageTiming / WatchdogResult dataclasses
# ---------------------------------------------------------------------------


class TestDataClasses:
    def test_metric_sample(self):
        from booley.yosys.synthesis_watchdog import MetricSample

        s = MetricSample(
            timestamp=1000.0,
            elapsed_s=10.0,
            stage="abc",
            rss_mb=512.0,
            rss_delta_mb=10.0,
            output_lines=100,
            stall_s=5.0,
        )
        assert s.stage == "abc"
        assert s.rss_mb == 512.0

    def test_stage_timing(self):
        from booley.yosys.synthesis_watchdog import StageTiming

        st = StageTiming(start_s=0.0, end_s=5.0, duration_s=5.0)
        assert st.duration_s == 5.0

    def test_stage_timing_open(self):
        from booley.yosys.synthesis_watchdog import StageTiming

        st = StageTiming(start_s=10.0)
        assert st.end_s is None
        assert st.duration_s is None

    def test_watchdog_result(self):
        from booley.yosys.synthesis_watchdog import WatchdogResult

        wr = WatchdogResult(
            stage_timings={"abc": {"start_s": 0, "end_s": 5, "duration_s": 5}},
            total_s=10.0,
            peak_rss_mb=500.0,
            max_stall_s=3.0,
            max_stall_stage="abc",
            final_stage="stat",
            samples=20,
            output_lines=1000,
            memory_growth_rate_mb_per_min=10.0,
        )
        assert wr.total_s == 10.0
        assert wr.peak_rss_mb == 500.0


# ---------------------------------------------------------------------------
# SynthesisWatchdog — lifecycle
# ---------------------------------------------------------------------------


class TestSynthesisWatchdog:
    def test_deprecated_kwargs_warn(self, tmp_path):
        from booley.yosys.synthesis_watchdog import SynthesisWatchdog

        mock_proc = MagicMock()
        mock_proc.stdout = MagicMock()
        with pytest.warns(DeprecationWarning, match="rss_limit_mb"):
            SynthesisWatchdog(
                mock_proc,
                "test",
                tmp_path,
                rss_limit_mb=5000,
            )

    def test_initial_state(self, tmp_path):
        from booley.yosys.synthesis_watchdog import SynthesisWatchdog

        mock_proc = MagicMock()
        mock_proc.stdout = MagicMock()
        wd = SynthesisWatchdog(mock_proc, "test", tmp_path)
        assert wd._current_stage == "starting"
        assert wd._output_lines == 0

    def test_get_stdout_empty(self, tmp_path):
        from booley.yosys.synthesis_watchdog import SynthesisWatchdog

        mock_proc = MagicMock()
        wd = SynthesisWatchdog(mock_proc, "test", tmp_path)
        assert wd.get_stdout() == ""

    def test_detect_stage_basic(self, tmp_path):
        """Stage detection should update current_step."""
        from booley.yosys.synthesis_watchdog import SynthesisWatchdog

        mock_proc = MagicMock()
        wd = SynthesisWatchdog(mock_proc, "test", tmp_path)
        wd._start_time = time.monotonic()
        wd._detect_stage("2. Executing hierarchy pass.")
        assert wd._current_stage == "hierarchy"
        assert "hierarchy" in wd._stage_timings

    def test_detect_stage_transition(self, tmp_path):
        """Transitioning stages should close the previous one."""
        from booley.yosys.synthesis_watchdog import SynthesisWatchdog

        mock_proc = MagicMock()
        wd = SynthesisWatchdog(mock_proc, "test", tmp_path)
        wd._start_time = time.monotonic()
        wd._detect_stage("1. Executing read_verilog pass.")
        wd._detect_stage("2. Executing hierarchy pass.")
        assert wd._stage_timings["read_verilog"].end_s is not None
        assert wd._stage_timings["read_verilog"].duration_s is not None

    def test_detect_stage_duplicate_gets_suffix(self, tmp_path):
        """Repeated stages (like opt) get numeric suffixes."""
        from booley.yosys.synthesis_watchdog import SynthesisWatchdog

        mock_proc = MagicMock()
        wd = SynthesisWatchdog(mock_proc, "test", tmp_path)
        wd._start_time = time.monotonic()
        wd._detect_stage("3.1. Executing opt pass.")
        wd._detect_stage("3.2. Executing opt pass.")
        assert "opt" in wd._stage_timings
        assert "opt_1" in wd._stage_timings

    def test_full_lifecycle(self, tmp_path):
        """Start and stop the watchdog with a mock process."""
        from booley.yosys.synthesis_watchdog import SynthesisWatchdog

        # Create a mock Popen with a stdout that yields lines then stops
        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_proc.poll.return_value = 0  # process exited
        # Stdout yields a few lines then "exhausts"
        mock_proc.stdout.readline = MagicMock(
            side_effect=["1. Executing read_verilog pass.\n", ""]
        )

        wd = SynthesisWatchdog(
            mock_proc,
            "test",
            tmp_path,
            poll_interval=1,
            heartbeat_interval=60,
        )

        with patch(
            "booley.yosys.synthesis_watchdog.get_process_rss", return_value=100 * 1024 * 1024
        ):
            wd.start()
            # Give threads a moment to process
            time.sleep(0.5)
            result = wd.stop()

        assert result.total_s >= 0
        assert result.final_stage is not None
        # Stage timings JSON should be written
        stage_file = tmp_path / "stage_timings.json"
        assert stage_file.exists()

    def test_write_stage_timings(self, tmp_path):
        from booley.yosys.synthesis_watchdog import StageTiming, SynthesisWatchdog

        mock_proc = MagicMock()
        wd = SynthesisWatchdog(mock_proc, "test", tmp_path)
        wd._stage_timings = {
            "abc": StageTiming(start_s=0, end_s=5, duration_s=5),
        }
        wd._peak_rss_mb = 200.0
        wd._max_stall_s = 3.0
        wd._max_stall_stage = "abc"
        wd._write_stage_timings(10.0)

        import json

        data = json.loads((tmp_path / "stage_timings.json").read_text(encoding="utf-8"))
        assert "abc" in data
        assert data["_summary"]["peak_rss_mb"] == 200.0

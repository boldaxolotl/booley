"""Synthesis watchdog — monitors Yosys process health and logs metrics.

Replaces the simple Heartbeat for Yosys synthesis calls.  Streams stdout
in real-time, detects synthesis stage transitions, samples RSS memory,
and flags stalls.  All metrics are logged to JSONL for post-run analysis.

Usage:
    watchdog = SynthesisWatchdog(proc, "Yosys my_config", work_dir)
    watchdog.start()
    proc.wait()
    result = watchdog.stop()   # returns WatchdogResult
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re
import subprocess

logger = logging.getLogger(__name__)
import sys
import threading
import time
import warnings
from collections import deque
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TextIO

# Reuse shared elapsed-time formatter from heartbeat module (parent dir)
sys.path.insert(0, str(Path(__file__).parent.parent))
from booley.runtime.heartbeat import fmt_elapsed as _fmt_elapsed

# ============================================================================
# Stage detection patterns
# ============================================================================

# Ordered list of (regex_pattern, stage_name) for Yosys output parsing.
# Yosys prints "X. Executing <COMMAND> pass." for each step.
STAGE_PATTERNS = [
    (re.compile(r"Executing.*\bread_verilog\b", re.IGNORECASE), "read_verilog"),
    (re.compile(r"Executing.*\bchparam\b", re.IGNORECASE), "chparam"),
    (re.compile(r"Executing.*\bhierarchy\b", re.IGNORECASE), "hierarchy"),
    (re.compile(r"Executing.*\bproc\b", re.IGNORECASE), "proc"),
    (re.compile(r"Executing.*\bmemory\b", re.IGNORECASE), "memory"),
    (re.compile(r"Executing.*\bfsm\b", re.IGNORECASE), "fsm"),
    (re.compile(r"Executing.*\btechmap\b", re.IGNORECASE), "techmap"),
    (re.compile(r"Executing.*\bflatten\b", re.IGNORECASE), "flatten"),
    (re.compile(r"Executing.*\bopt\b", re.IGNORECASE), "opt"),
    (re.compile(r"Executing.*\bwrite_verilog\b", re.IGNORECASE), "write_verilog"),
    (re.compile(r"Executing.*\bdfflibmap\b", re.IGNORECASE), "dfflibmap"),
    (re.compile(r"Executing.*\babc\b", re.IGNORECASE), "abc"),
    (re.compile(r"Executing.*\bstat\b", re.IGNORECASE), "stat"),
]

# OpenROAD stages. OpenROAD prints no per-command banners, so the generated
# run_openroad.tcl (openroad_timing.write_openroad_script) emits explicit
# ``puts "BOOLEY_STAGE: <name>"`` markers before each stage — without them the
# whole placement+repair run sat in stage "starting" and stall attribution
# was meaningless. Keep in sync with the marker names in that script.
OPENROAD_STAGE_NAMES = [
    "floorplan",
    "global_placement",
    "place_pins",
    "repair_design",
    "detailed_placement",
    "sta_report_pre_repair",
    "repair_timing",
    "sta_report",
]
STAGE_PATTERNS += [
    (re.compile(rf"^BOOLEY_STAGE: {name}\s*$"), name) for name in OPENROAD_STAGE_NAMES
]


# ============================================================================
# Data classes
# ============================================================================


@dataclass
class MetricSample:
    """Single point-in-time resource measurement."""

    timestamp: float  # absolute time (time.time())
    elapsed_s: float  # seconds since watchdog start
    stage: str  # current Yosys stage
    rss_mb: float | None  # resident set size in MB
    rss_delta_mb: float  # change from previous sample
    output_lines: int  # total stdout lines so far
    stall_s: float  # seconds since last stdout line


@dataclass
class StageTiming:
    """Duration of a single synthesis stage."""

    start_s: float  # seconds since watchdog start
    end_s: float | None = None
    duration_s: float | None = None


@dataclass
class WatchdogResult:
    """Summary produced when the watchdog stops."""

    stage_timings: dict[str, dict]  # stage_name -> {start_s, end_s, duration_s}
    total_s: float
    peak_rss_mb: float | None
    max_stall_s: float  # longest period without output
    max_stall_stage: str  # which stage had the longest stall
    final_stage: str  # last stage detected before exit
    samples: int  # total metric samples collected
    output_lines: int  # total stdout lines captured
    memory_growth_rate_mb_per_min: float | None  # avg growth over entire run


# ============================================================================
# Cross-platform process RSS helpers
# ============================================================================


def get_process_rss(pid: int) -> int | None:
    """Get RSS in bytes for a PID. Cross-platform (Windows + Linux).

    Returns None if the process is gone or parsing fails.
    """
    if sys.platform == "win32":
        return _get_rss_windows(pid)
    return _get_rss_linux(pid)


def _get_rss_windows(pid: int) -> int | None:
    """Get RSS via tasklist (Windows, no psutil needed)."""
    try:
        result = subprocess.run(
            ["tasklist", "/fi", f"pid eq {pid}", "/fo", "csv"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode != 0 or "No tasks" in result.stdout:
            return None

        reader = csv.reader(io.StringIO(result.stdout.strip()))
        rows = list(reader)
        if len(rows) < 2:
            return None

        headers = [h.strip().lower() for h in rows[0]]
        values = rows[1]

        mem_idx = None
        for i, h in enumerate(headers):
            if "mem" in h:
                mem_idx = i
                break
        if mem_idx is None or mem_idx >= len(values):
            return None

        mem_str = values[mem_idx].strip().replace(",", "").replace(".", "")
        mem_str = re.sub(r"[^\d]", "", mem_str)
        if not mem_str:
            return None
        return int(mem_str) * 1024  # tasklist reports in KB

    except (OSError, subprocess.TimeoutExpired):
        return None


def _get_rss_linux(pid: int) -> int | None:
    """Get RSS via /proc/{pid}/status (Linux, no psutil needed)."""
    try:
        status_path = Path(f"/proc/{pid}/status")
        if not status_path.exists():
            return None
        for line in status_path.read_text().splitlines():
            if line.startswith("VmRSS:"):
                # "VmRSS:    123456 kB"
                parts = line.split()
                if len(parts) >= 2:
                    return int(parts[1]) * 1024  # kB -> bytes
        return None
    except (OSError, ValueError):
        return None


# ============================================================================
# SynthesisWatchdog
# ============================================================================


def _warn_deprecated_kwargs(
    rss_limit_mb: float | None,
    stall_limit_s: float,
    abc_stall_limit_s: float,
) -> None:
    """Emit DeprecationWarning for any non-default legacy kwargs."""
    for _name, _val, _default in [
        ("rss_limit_mb", rss_limit_mb, None),
        ("stall_limit_s", stall_limit_s, 0),
        ("abc_stall_limit_s", abc_stall_limit_s, 0),
    ]:
        if _val != _default:
            warnings.warn(
                f"SynthesisWatchdog: '{_name}' is deprecated and ignored. "
                "Kill logic was moved to the synthesis retry developer.",
                DeprecationWarning,
                stacklevel=3,
            )


class SynthesisWatchdog:
    """Monitors a live Yosys subprocess.

    Attach to a Popen instance *before* calling proc.wait().
    Three daemon threads handle output reading, resource sampling,
    and stall detection.  All metrics are appended to a JSONL file.

    Args:
        proc: Live subprocess.Popen with stdout=PIPE, stderr=STDOUT, text=True.
        desc: Human label for log messages (e.g. "Yosys my_config").
        work_dir: Directory for metrics log files.
        poll_interval: Seconds between resource samples (default 10).
        heartbeat_interval: Seconds between heartbeat prints (default 60).
        log_file: Optional open file handle to mirror stdout lines.
        timings_filename: Name of the per-run stage-timings JSON written at
            stop(). Each watched step in a shared work_dir must use its own
            name — e.g. the OpenROAD/OpenSTA timing step after Yosys —
            otherwise the later step's stop() clobbers the earlier one's file.
    """

    def __init__(
        self,
        proc: subprocess.Popen,
        desc: str,
        work_dir: Path,
        poll_interval: int = 10,
        heartbeat_interval: int = 60,
        log_file: TextIO | None = None,
        timings_filename: str = "stage_timings.json",
        # Legacy kwargs — deprecated, accepted for backward compat
        rss_limit_mb: float | None = None,
        stall_limit_s: float = 0,
        abc_stall_limit_s: float = 0,
    ) -> None:
        _warn_deprecated_kwargs(rss_limit_mb, stall_limit_s, abc_stall_limit_s)

        self._proc = proc
        self._desc = desc
        self._work_dir = Path(work_dir)
        self._poll_interval = poll_interval
        self._heartbeat_interval = heartbeat_interval
        self._log_file = log_file
        self._timings_filename = timings_filename

        # Shared state (protected by _lock)
        self._lock = threading.Lock()
        self._current_stage = "starting"
        self._output_lines = 0
        self._last_output_time = time.monotonic()
        # Bounded buffer — keep last 10k lines to prevent OOM on long runs.
        self._stdout_buffer: deque[str] = deque(maxlen=10_000)

        self._init_tracking_state()

    def _init_tracking_state(self) -> None:
        """Initialize stage, resource, and control tracking fields."""
        self._stage_timings: dict[str, StageTiming] = {}
        self._stage_order: list[str] = []

        self._samples: list[MetricSample] = []
        self._peak_rss_mb: float | None = None
        self._max_stall_s = 0.0
        self._max_stall_stage = "starting"

        self._start_time: float | None = None
        self._stop_event = threading.Event()
        self._threads: list[threading.Thread] = []

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start all monitor threads."""
        self._start_time = time.monotonic()
        self._last_output_time = self._start_time
        self._stop_event.clear()

        # Output reader thread
        t_output = threading.Thread(target=self._output_reader_loop, daemon=True, name="wd-output")
        # Resource sampler thread
        t_resource = threading.Thread(
            target=self._resource_sampler_loop, daemon=True, name="wd-resource"
        )

        self._threads = [t_output, t_resource]
        for t in self._threads:
            t.start()

    def join_reader(self) -> None:
        """Block until the output reader thread finishes.

        Call this instead of proc.wait() to avoid a Windows pipe deadlock.
        The reader exits when proc.stdout is closed (i.e. process exits),
        so joining it is equivalent to waiting for the process — but without
        the handle-close race.
        """
        if self._threads:
            self._threads[0].join()  # t_output is first

    def stop(self) -> WatchdogResult:
        """Stop all threads and return collected metrics."""
        self._stop_event.set()
        for t in self._threads:
            t.join(timeout=5)

        # Finalize last stage timing
        elapsed = time.monotonic() - self._start_time
        with self._lock:
            if self._stage_order:
                last = self._stage_order[-1]
                st = self._stage_timings[last]
                if st.end_s is None:
                    st.end_s = elapsed
                    st.duration_s = st.end_s - st.start_s

        # Write stage_timings.json
        self._write_stage_timings(elapsed)

        # Compute memory growth rate
        growth_rate = None
        if len(self._samples) >= 2:
            first_rss = next((s.rss_mb for s in self._samples if s.rss_mb), None)
            last_rss = next((s.rss_mb for s in reversed(self._samples) if s.rss_mb), None)
            if first_rss is not None and last_rss is not None and elapsed > 0:
                growth_rate = (last_rss - first_rss) / (elapsed / 60.0)

        with self._lock:
            final_stage = self._current_stage
            output_lines = self._output_lines

        return WatchdogResult(
            stage_timings={name: asdict(st) for name, st in self._stage_timings.items()},
            total_s=round(elapsed, 2),
            peak_rss_mb=self._peak_rss_mb,
            max_stall_s=round(self._max_stall_s, 1),
            max_stall_stage=self._max_stall_stage,
            final_stage=final_stage,
            samples=len(self._samples),
            output_lines=output_lines,
            memory_growth_rate_mb_per_min=round(growth_rate, 2)
            if growth_rate is not None
            else None,
        )

    def get_stdout(self) -> str:
        """Return all captured stdout as a single string."""
        with self._lock:
            return "\n".join(self._stdout_buffer)

    # ------------------------------------------------------------------
    # Output reader thread
    # ------------------------------------------------------------------

    def _output_reader_loop(self) -> None:
        """Read proc.stdout line-by-line, detect stages, buffer output.

        IMPORTANT: Do NOT print every line to console here.  If Python's
        stdout is a pipe (e.g. running under 'timeout' or captured by a
        parent process), printing all lines can fill the pipe buffer and
        deadlock — the reader blocks on print(), stops consuming Yosys's
        stdout pipe, and Yosys blocks on write().  Stage transitions are
        printed selectively; full output goes to the log file only.
        """
        try:
            for raw_line in iter(self._proc.stdout.readline, ""):
                if self._stop_event.is_set():
                    break
                line = raw_line.rstrip("\n").rstrip("\r")

                with self._lock:
                    self._stdout_buffer.append(line)
                    self._output_lines += 1
                    self._last_output_time = time.monotonic()

                # Mirror to log file if provided
                if self._log_file:
                    self._log_file.write(line + "\n")
                    self._log_file.flush()

                # Detect stage transitions (prints transition lines only)
                self._detect_stage(line)
        except OSError as e:
            logger.debug("Stdout reader stopped: %s", e)

    def _detect_stage(self, line: str) -> None:
        """Check if line indicates a new synthesis stage."""
        for pattern, stage_name in STAGE_PATTERNS:
            if pattern.search(line):
                elapsed = time.monotonic() - self._start_time
                with self._lock:
                    prev_stage = self._current_stage

                    # Close previous stage timing
                    if self._stage_order:
                        prev = self._stage_order[-1]
                        st = self._stage_timings[prev]
                        if st.end_s is None:
                            st.end_s = elapsed
                            st.duration_s = round(st.end_s - st.start_s, 3)

                    # Some stages (opt, write_verilog) can appear multiple times.
                    # Append a suffix to make them unique.
                    unique_name = stage_name
                    if unique_name in self._stage_timings:
                        count = sum(
                            1
                            for s in self._stage_order
                            if s == stage_name or s.startswith(f"{stage_name}_")
                        )
                        unique_name = f"{stage_name}_{count}"

                    self._current_stage = unique_name
                    self._stage_timings[unique_name] = StageTiming(start_s=round(elapsed, 3))
                    self._stage_order.append(unique_name)

                    if prev_stage != stage_name:
                        dur_info = ""
                        if self._stage_order and len(self._stage_order) >= 2:
                            prev_key = self._stage_order[-2]
                            prev_st = self._stage_timings[prev_key]
                            if prev_st.duration_s is not None:
                                dur_info = f" ({prev_key}: {prev_st.duration_s:.1f}s)"
                        # Use stderr to avoid stdout pipe deadlock
                        print(
                            f"  \u25b6 [{self._desc}] stage: {stage_name}{dur_info}",
                            file=sys.stderr,
                            flush=True,
                        )
                break  # only match first pattern

    # ------------------------------------------------------------------
    # Resource sampler thread
    # ------------------------------------------------------------------

    def _sample_rss(self) -> tuple[float | None, float]:
        """Sample RSS and compute delta from previous sample."""
        rss_bytes = get_process_rss(self._proc.pid)
        rss_mb = round(rss_bytes / (1024 * 1024), 1) if rss_bytes else None

        rss_delta = 0.0
        if rss_mb is not None and self._samples:
            prev_rss = self._samples[-1].rss_mb
            if prev_rss is not None:
                rss_delta = round(rss_mb - prev_rss, 1)

        if rss_mb is not None and (self._peak_rss_mb is None or rss_mb > self._peak_rss_mb):
            self._peak_rss_mb = rss_mb

        return rss_mb, rss_delta

    def _read_stall_info(self) -> tuple[float, str, int]:
        """Read stall duration, current stage, and output lines under lock."""
        with self._lock:
            stall_s = round(time.monotonic() - self._last_output_time, 1)
            current_stage = self._current_stage
            output_lines = self._output_lines
        if stall_s > self._max_stall_s:
            self._max_stall_s = stall_s
            self._max_stall_stage = current_stage
        return stall_s, current_stage, output_lines

    def _print_heartbeat(
        self, elapsed: float, current_stage: str, rss_mb: float | None, stall_s: float
    ) -> None:
        """Print heartbeat status line to stderr."""
        rss_info = f"RSS: {rss_mb:.0f}MB" if rss_mb else "RSS: ?"
        stall_info = f"stall: {stall_s:.0f}s" if stall_s > 30 else ""
        parts = [
            f"[{self._desc}]",
            f"elapsed: {_fmt_elapsed(elapsed)}",
            f"stage: {current_stage}",
            rss_info,
        ]
        if stall_info:
            parts.append(stall_info)
        print(f"  ♥ {' | '.join(parts)}", file=sys.stderr, flush=True)

    def _resource_sampler_loop(self) -> None:
        """Periodically sample RSS/CPU and log metrics for diagnostics.

        No kill logic — RAM safety is handled by the pre-flight check in
        the synthesis runner.  This thread just collects metrics and prints
        heartbeat status.
        """
        next_heartbeat = self._start_time + self._heartbeat_interval
        error_log = self._work_dir / "watchdog_errors.log"

        while not self._stop_event.wait(self._poll_interval):
            try:
                if self._proc.poll() is not None:
                    break

                elapsed = time.monotonic() - self._start_time
                rss_mb, rss_delta = self._sample_rss()
                stall_s, current_stage, output_lines = self._read_stall_info()

                sample = MetricSample(
                    timestamp=time.time(),
                    elapsed_s=round(elapsed, 1),
                    stage=current_stage,
                    rss_mb=rss_mb,
                    rss_delta_mb=rss_delta,
                    output_lines=output_lines,
                    stall_s=stall_s,
                )
                self._samples.append(sample)
                self._append_metric(sample)

                now = time.monotonic()
                if now >= next_heartbeat:
                    self._print_heartbeat(elapsed, current_stage, rss_mb, stall_s)
                    next_heartbeat = now + self._heartbeat_interval

            except (OSError, subprocess.SubprocessError, ValueError, IndexError) as exc:
                try:
                    with error_log.open("a", encoding="utf-8") as f:
                        f.write(f"{time.time()}: sampler error: {exc}\n")
                except OSError:
                    pass

    # ------------------------------------------------------------------
    # File I/O
    # ------------------------------------------------------------------

    def _append_metric(self, sample: MetricSample) -> None:
        """Append a metric sample to watchdog_metrics.jsonl."""
        path = self._work_dir / "watchdog_metrics.jsonl"
        try:
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(asdict(sample)) + "\n")
        except OSError as e:
            logger.debug("Failed to write watchdog metric to %s: %s", path, e)

    def _write_stage_timings(self, total_s: float) -> None:
        """Write the stage-timings JSON at the end of a run."""
        data = {name: asdict(st) for name, st in self._stage_timings.items()}
        data["_summary"] = {
            "total_s": round(total_s, 2),
            "peak_rss_mb": self._peak_rss_mb,
            "max_stall_s": round(self._max_stall_s, 1),
            "max_stall_stage": self._max_stall_stage,
            "samples": len(self._samples),
        }
        path = self._work_dir / self._timings_filename
        try:
            path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        except OSError as e:
            logger.warning("Failed to write stage timings to %s: %s", path, e)


# ============================================================================
# Helpers
# ============================================================================

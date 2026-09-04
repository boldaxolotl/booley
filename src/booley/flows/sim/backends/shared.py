"""Simulator-neutral discovery, progress, and declared-trace helpers."""

from __future__ import annotations

import contextlib
import sys
import time
from collections import deque
from pathlib import Path

from booley.flows.run_log import write_run_log_progress
from booley.flows.sim.trace_session import TraceSession

RUN_LOG_PROGRESS_INTERVAL_S = 5.0
MAX_ADOPTED_VCD_BYTES = 2 * 1024**3
TraceFileSnapshot = dict[Path, tuple[int, int, int]]
ChildCpuSnapshot = tuple[float, float]


def child_cpu_snapshot() -> ChildCpuSnapshot | None:
    """Return cumulative child user/system CPU seconds when the OS exposes it."""
    try:
        import resource
    except ImportError:  # pragma: no cover - unavailable on native Windows
        return None
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    return float(usage.ru_utime), float(usage.ru_stime)


def child_cpu_marker(
    started: ChildCpuSnapshot | None,
    finished: ChildCpuSnapshot | None,
) -> str:
    """Format the stable simulator CPU marker for one supervised child run."""
    if started is None or finished is None:
        return ""
    user_s = max(0.0, finished[0] - started[0])
    system_s = max(0.0, finished[1] - started[1])
    return f"BOOLEY_SIM_CPU_SECONDS: user={user_s:.6f} system={system_s:.6f}"


def append_child_cpu_marker(lines: deque[str], started: ChildCpuSnapshot | None) -> None:
    """Print and retain CPU usage accumulated by a completed simulator child."""
    marker = child_cpu_marker(started, child_cpu_snapshot())
    if marker:
        print(marker)
        lines.append(marker + "\n")


def find_icarus_image(build_dir: Path) -> str | None:
    """Locate an Icarus vvp image through its Edalize ``.scr`` sibling."""
    scripts = sorted(build_dir.glob("*.scr"))
    if not scripts:
        return None
    if len(scripts) > 1:
        print(
            f"WARNING: multiple .scr files in {build_dir}; using {scripts[0].name}",
            file=sys.stderr,
        )
    return scripts[0].stem


class RunLogProgress:
    """Periodically persist a simulator's live output tail."""

    def __init__(self, work_dir: Path | None, started: float) -> None:
        self._work_dir = work_dir
        self._started = started
        self._last_flush = started
        self._last_line_at = started

    @property
    def idle_s(self) -> float:
        """Return seconds since the simulator last printed a line."""
        return time.monotonic() - self._last_line_at

    def observe(self, lines: deque[str]) -> None:
        """Record output activity and flush when the interval has elapsed."""
        self._last_line_at = time.monotonic()
        if self._work_dir is None:
            return
        if self._last_line_at - self._last_flush < RUN_LOG_PROGRESS_INTERVAL_S:
            return
        self._last_flush = self._last_line_at
        self._write(lines)

    def final_flush(self, lines: deque[str]) -> None:
        """Persist the final live tail before the completed log replaces it."""
        if self._work_dir is not None:
            self._write(lines)

    def _write(self, lines: deque[str]) -> None:
        with contextlib.suppress(OSError):
            write_run_log_progress(
                self._work_dir,
                "".join(lines),
                elapsed_s=time.monotonic() - self._started,
                line_count=len(lines),
                idle_s=self.idle_s,
            )


def format_idle_note(idle_s: float, line_count: int) -> str:
    """Describe how long a timed-out simulator had stopped producing output."""
    if line_count == 0:
        return " — the simulation printed NO output at all"
    return f" — last output {idle_s:.0f}s ago, {line_count} line(s) total"


def trace_file_stamp(path: Path) -> tuple[int, int, int] | None:
    """Return a cheap trace identity for freshness checks."""
    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_mtime_ns, stat.st_size, stat.st_ino


def _declared_trace_matches(pattern: str, search_dirs: list[Path]) -> list[Path]:
    candidate = Path(pattern)
    if candidate.is_absolute():
        return sorted(path for path in candidate.parent.glob(candidate.name) if path.is_file())
    for base in search_dirs:
        matches = sorted(path for path in base.glob(pattern) if path.is_file())
        if matches:
            return matches
    return []


def snapshot_declared_trace_files(
    trace_files: list[str] | None,
    search_dirs: list[Path],
) -> TraceFileSnapshot:
    """Snapshot declared artifacts so only current-run output can be adopted."""
    snapshot: TraceFileSnapshot = {}
    for pattern in trace_files or []:
        for match in _declared_trace_matches(pattern, search_dirs):
            stamp = trace_file_stamp(match)
            if stamp is not None:
                snapshot[match.resolve()] = stamp
    return snapshot


def adopt_declared_trace_files(
    trace: TraceSession,
    trace_files: list[str] | None,
    search_dirs: list[Path],
    before: TraceFileSnapshot | None = None,
) -> Path | None:
    """Adopt the first fresh, nonempty declared trace artifact."""
    for pattern in trace_files or []:
        for match in _declared_trace_matches(pattern, search_dirs):
            if before is not None and before.get(match.resolve()) == trace_file_stamp(match):
                continue
            if not match.is_file() or (size := match.stat().st_size) == 0:
                continue
            if match.suffix.lower() != ".vcd":
                return match
            if size > MAX_ADOPTED_VCD_BYTES:
                print(
                    f"WARNING: declared trace {match} is {size:,} bytes, over the "
                    f"{MAX_ADOPTED_VCD_BYTES:,}-byte finalize-time conversion cap; "
                    "retaining the raw VCD for incident diagnostics; it cannot "
                    "earn TRACE_OK (convert it separately with `bwave build`, "
                    "or scope the dump down)"
                )
                return match
            trace.postprocess(match)
            found = trace.find()
            if found is not None:
                return found
    return None

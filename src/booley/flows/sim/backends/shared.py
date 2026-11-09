"""Simulator-neutral discovery, progress, and declared-trace helpers."""

from __future__ import annotations

import contextlib
import sys
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from booley.flows.run_log import write_run_log_progress
from booley.flows.sim.backends.declared_streams import DeclaredTraceStreams
from booley.flows.sim.trace_session import TraceSession

RUN_LOG_PROGRESS_INTERVAL_S = 5.0
TraceFileSnapshot = dict[Path, tuple[int, int, int]]
ChildCpuSnapshot = tuple[float, float]
RunTerminationKind = Literal[
    "completed",
    "timeout",
    "disk_budget",
    "fatal_init",
    "sim_time_stall",
    "trace_stall",
]
RunFailureKind = Literal["", "timeout", "infrastructure", "missing_input"]
SimulationFailureKind = Literal[
    "",
    "design",
    "infrastructure",
    "timeout",
    "inconclusive",
    "artifact",
    "missing_input",
]

_TERMINATION_FAILURE_KINDS: dict[RunTerminationKind, frozenset[SimulationFailureKind]] = {
    "completed": frozenset({"", "design", "inconclusive", "artifact"}),
    "timeout": frozenset({"timeout"}),
    "disk_budget": frozenset({"infrastructure"}),
    "fatal_init": frozenset({"infrastructure", "missing_input"}),
    "sim_time_stall": frozenset({"infrastructure"}),
    "trace_stall": frozenset({"infrastructure"}),
}


def allowed_failure_kinds(kind: RunTerminationKind) -> frozenset[SimulationFailureKind]:
    """Return the single shared failure policy for a run termination."""
    return _TERMINATION_FAILURE_KINDS[kind]


@dataclass(frozen=True)
class RunTermination:
    """Structured reason a live simulator stopped.

    Only ``completed`` permits verdict inference from sentinels and return code.
    Every other value records an adapter-owned abort at the process boundary.
    """

    kind: RunTerminationKind = "completed"
    detail: str = ""
    failure_kind: RunFailureKind = ""
    missing_input_path: str = ""

    def __post_init__(self) -> None:
        if self.kind == "completed":
            if self.detail or self.failure_kind or self.missing_input_path:
                raise ValueError("completed termination cannot carry abort evidence")
            return
        expected = allowed_failure_kinds(self.kind)
        if not self.detail or self.failure_kind not in expected:
            raise ValueError(f"invalid {self.kind} termination evidence")

    @property
    def aborted(self) -> bool:
        """Whether a guard, deadline, or fatal initializer stopped the run."""
        return self.kind != "completed"


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


# The dump name Booley itself arranges: Icarus's ``booley_vcd_dump`` module
# hardcodes ``$dumpfile("dump.vcd")`` and cocotb passes it via ``--trace-file``.
BOOLEY_DUMP_VCD_NAME = "dump.vcd"


def trace_file_stamp(path: Path) -> tuple[int, int, int] | None:
    """Return a cheap trace identity for freshness checks."""
    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_mtime_ns, stat.st_size, stat.st_ino


def _declared_trace_matches(patterns: tuple[str, ...], roots: tuple[Path, ...]) -> list[Path]:
    """Return every file a declared pattern names, in pattern then root order.

    Each pattern is searched in every root (an absolute pattern once), so a
    copy that an earlier root's match would shadow is still seen. Files are
    deduplicated by resolved path.
    """
    matches: dict[Path, Path] = {}
    for pattern in patterns:
        candidate = Path(pattern)
        if candidate.is_absolute():
            bases, glob = [candidate.parent], candidate.name
        else:
            bases, glob = list(roots), pattern
        for base in bases:
            for path in sorted(base.glob(glob)):
                if path.is_file():
                    matches.setdefault(path.resolve(), path)
    return list(matches.values())


@dataclass(frozen=True)
class TraceSelection:
    """The waveform one run produced, or the reason none could be chosen."""

    path: Path | None
    # Fresh declared stores when more than one exists and Booley owns none.
    ambiguous: tuple[Path, ...] = ()


def ambiguous_trace_reason(stores: tuple[Path, ...]) -> str:
    """Explain why several fresh declared stores cannot be narrowed to one."""
    names = ", ".join(str(store) for store in stores)
    return (
        "trace requested but the current run produced several declared waveform "
        f"stores and Booley cannot choose one: {names} (narrow "
        "[flows.sim].trace_files to the one waveform to query)"
    )


@dataclass(frozen=True)
class CurrentRunTraces:
    """The one freshness filter that decides which waveform a run produced.

    A run's trace is a Booley-owned output (what :meth:`TraceSession.find`
    returns after :meth:`begin` cleared them) or a file matching the declared
    ``[flows.sim].trace_files`` patterns whose stamp changed since
    :meth:`begin`. Nothing else is ever adopted: a leftover or undeclared
    waveform is not evidence about this run.
    """

    session: TraceSession
    patterns: tuple[str, ...]
    roots: tuple[Path, ...]
    before: TraceFileSnapshot
    # Declared ``.vcd`` entries streaming through pipes; ``None`` when none may.
    streams: DeclaredTraceStreams | None = None

    @classmethod
    def begin(
        cls,
        session: TraceSession,
        *,
        raw_trace_paths: tuple[Path, ...] = (),
        patterns: tuple[str, ...] = (),
        roots: tuple[Path, ...] = (),
        stream_from: Path | None = None,
    ) -> CurrentRunTraces:
        """Clear Booley-owned outputs, snapshot declared files in every root, then stream.

        With *stream_from* (the run directory) eligible declared VCDs become
        pipes, started after the snapshot so freshness evidence is unchanged.
        """
        session.reset_for_run(raw_trace_paths)
        before: TraceFileSnapshot = {}
        for match in _declared_trace_matches(patterns, roots):
            stamp = trace_file_stamp(match)
            if stamp is not None:
                before[match.resolve()] = stamp
        streams = (
            DeclaredTraceStreams.start(session, patterns, stream_from)
            if stream_from is not None
            else None
        )
        return cls(session, patterns, roots, before, streams)

    def close_streams(self) -> None:
        """After the simulator stopped: give converters EOF and remove our pipes."""
        if self.streams is not None:
            self.streams.close_inputs()

    def finish_streams(self) -> None:
        """Wait for the declared-stream converters within the conversion budget."""
        if self.streams is not None:
            self.streams.finish()

    @property
    def stream_failures(self) -> tuple[str, ...]:
        """Why each failed declared stream failed."""
        return tuple(self.streams.failures.values()) if self.streams is not None else ()

    def select(self) -> TraceSelection:
        """Choose this run's waveform: owned output first, else one fresh declared file."""
        owned = self.session.find()
        if owned is not None:
            return TraceSelection(owned)
        fresh = self._fresh_declared()
        streamed = set(self.streams.stores.values()) if self.streams is not None else set()
        # A streamed store stands in for a VCD, so it never makes the choice ambiguous.
        stores = tuple(
            path for path in fresh if path.suffix.lower() != ".vcd" and path not in streamed
        )
        if len(stores) > 1:
            return TraceSelection(None, ambiguous=stores)
        for match in fresh:
            if match.suffix.lower() != ".vcd":
                return TraceSelection(match)
            self.session.postprocess(match)
            # Owned-only lookup: the converted store, or the VCD that
            # postprocess retained when conversion failed.
            converted = self.session.find()
            if converted is not None:
                return TraceSelection(converted)
        return TraceSelection(None)

    def _fresh_declared(self) -> list[Path]:
        """Declared nonempty files written since :meth:`begin`, never Booley-owned names."""
        if not self.patterns:
            return []
        owned = {path.resolve() for path in self.session.owned_outputs}
        stores = self.streams.stores if self.streams is not None else {}
        fresh: list[Path] = []
        seen: set[Path] = set()
        for pattern in self.patterns:
            if pattern in stores:
                fresh.append(stores[pattern])
            for match in _declared_trace_matches((pattern,), self.roots):
                resolved = match.resolve()
                stamp = trace_file_stamp(match)
                if resolved in owned or resolved in seen or stamp is None or stamp[1] == 0:
                    continue
                seen.add(resolved)
                if self.before.get(resolved) == stamp:
                    continue
                fresh.append(match)
        return fresh

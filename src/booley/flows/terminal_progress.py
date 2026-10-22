"""Invocation-scoped human observation, installed only by the built-in CLI.

Completed flow-console directories may be deleted manually. Active directories
contain an ``active`` marker and must be left alone. Observation is advisory;
its failures never change execution evidence or verdicts.
"""

from __future__ import annotations

import codecs
import contextvars
import logging
import os
import queue
import re
import select
import shutil
import stat
import tempfile
import threading
import time
import unicodedata
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, TextIO

from booley.runtime.booley_dir import runtime_dir
from booley.runtime.timefmt import format_age, format_duration, utc_now_rfc3339

_CURRENT: contextvars.ContextVar[TerminalProgress | None] = contextvars.ContextVar(
    "flow_terminal_progress", default=None
)
_LANE: contextvars.ContextVar[_Lane | None] = contextvars.ContextVar(
    "flow_terminal_progress_lane", default=None
)
_ANSI = re.compile(r"\x1b(?:\][^\x07\x1b]*(?:\x07|\x1b\\)|\[[0-?]*[ -/]*[@-~]|[@-_])")
_ENVELOPE = re.compile(
    r"^\[(?:COCOTB_RESULTS|BOOLEY_[A-Z_]+|SIM_RESULTS|SIM_SUMMARY|SIM_INFRA_ERROR)\]"
)
_OWNED_RECORD = re.compile(
    r"^BOOLEY_(?:(?:BUILD|RUN)_STAGE\s|BUILD_(?:MILLISECONDS|SECONDS):|SIM_CPU_SECONDS:)"
)
_STAGE_LINES = re.compile(r"(?m)^BOOLEY_STAGE:[^\n]*$")
_STAGE = re.compile(r"^BOOLEY_STAGE:\s*([\w.-]+)\s*$")


@dataclass(eq=False)
class _Lane:
    """One concurrently running unit of work (a Simulation Campaign lane).

    Lanes keep their own stage, recent output and unflushed summary lines so
    parallel units never relabel, evict or clear each other's progress.
    """

    label: str
    stage: str
    target: str
    execution_role: str | None = None
    # Recent lines for the live TTY rows.
    tail: deque[str] = field(default_factory=lambda: deque(maxlen=6))
    # Lines awaiting the next ``output:`` summary, labelled when produced.
    pending: deque[tuple[str, str, str]] = field(default_factory=lambda: deque(maxlen=6))
    last_output: float | None = None
    closed: bool = False


# A production-time ``(target, stage)`` label for one observed chunk.
_Snapshot = tuple[str, str]
_Sample = tuple[str, bytes, "_Lane | None", "_Snapshot | None"]


class ProgressText(str):
    """Human completion text with the unchanged representation for records."""

    record_text: str

    def __new__(cls, text: str, *, record_text: str) -> ProgressText:
        result = super().__new__(cls, text)
        result.record_text = record_text
        return result


def _record_text(text: str) -> str:
    return text.record_text if isinstance(text, ProgressText) else text


def current_progress() -> TerminalProgress | None:
    """Return the explicitly installed CLI observer, including direct hooks."""
    return _CURRENT.get()


def sanitize(value: str, limit: int = 2048) -> str:
    """Remove terminal controls and bound the UTF-8 presentation size."""
    text = _ANSI.sub("", value).expandtabs(4)
    text = "".join(char for char in text if char.isprintable() or char == "\t")
    return text.encode("utf-8")[:limit].decode("utf-8", errors="ignore")


def fit_width(text: str, width: int) -> str:
    """Clip by terminal cells, including wide and combining Unicode characters."""
    cells = 0
    for index, char in enumerate(text):
        size = (
            0
            if unicodedata.combining(char)
            else 2
            if unicodedata.east_asian_width(char) in {"W", "F"}
            else 1
        )
        if cells + size > width:
            return text[:index]
        cells += size
    return text


class _Lines:
    def __init__(self) -> None:
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.pending = ""
        self.truncated = False

    def feed(self, chunk: bytes, *, final: bool = False, tail_only: bool = False) -> list[str]:
        parts = self.decoder.decode(chunk, final=final).replace("\r", "\n").split("\n")
        if tail_only and len(parts) > 8:
            # Preserve the pending line and every stage marker, but coalesce
            # presentation of floods before doing per-line Unicode sanitation.
            markers = _STAGE_LINES.findall("\n".join(parts[1:-7]))
            parts = [parts[0], *markers, *parts[-7:]]
        lines = []
        for index, part in enumerate(parts):
            available = max(0, 2048 - len(self.pending))
            self.pending += part[:available]
            self.truncated = self.truncated or len(part) > available
            if index < len(parts) - 1 or final:
                if self.pending or index < len(parts) - 1:
                    lines.append(self.pending + (" [truncated]" if self.truncated else ""))
                self.pending = ""
                self.truncated = False
        return lines


class _FileSource:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.signature = self._stat()
        self.offset = self.signature[1] if self.signature else 0
        self.lines = _Lines()
        self.prefix = self._prefix()

    def _prefix(self) -> bytes:
        try:
            with self.path.open("rb") as source:
                return source.read(64)
        except OSError:
            return b""

    def _stat(self) -> tuple[int, int, int] | None:
        try:
            stat = self.path.stat()
            return stat.st_ino, stat.st_size, stat.st_mtime_ns
        except OSError:
            return None

    def read(self) -> bytes:
        signature = self._stat()
        if signature is None or signature == self.signature:
            return b""
        previous = self.signature
        prefix = self._prefix()
        rewritten = bool(self.prefix) and prefix[: len(self.prefix)] != self.prefix
        if previous and (signature[0] != previous[0] or signature[1] < self.offset or rewritten):
            self.offset = 0
            self.lines = _Lines()
        elif previous and signature[1] == self.offset and signature[2] != previous[2]:
            self.offset = 0  # same-size rewrite
            self.lines = _Lines()
        try:
            with self.path.open("rb") as source:
                source.seek(self.offset)
                chunk = source.read(65536)
        except OSError:
            return b""
        self.offset += len(chunk)
        self.prefix = prefix
        # Keep a backlog visible to subsequent ticks, even with unchanged mtime.
        self.signature = signature if self.offset >= signature[1] else None
        return chunk


@dataclass(frozen=True)
class _RegisteredLog:
    target: str
    stage: str
    temporary: bool


class TerminalProgress:
    """Bounded live presentation with a separate append-only observation log."""

    def __init__(
        self,
        flow: str,
        target: str,
        work_dir: Path,
        *,
        tool_output: bool = False,
        stream: TextIO,
        clock: Callable[[], float] = time.monotonic,
        dimensions: Callable[[], os.terminal_size] = shutil.get_terminal_size,
    ) -> None:
        self.flow: str = flow
        self.target: str = target
        self.work_dir: Path = work_dir
        self.stream: TextIO = stream
        self.clock: Callable[[], float] = clock
        self.dimensions: Callable[[], os.terminal_size] = dimensions
        self.tool_output: bool = tool_output
        self.started: float = clock()
        self.command_started: float = self.started
        self.stage: str = "preparing"
        self.tail: deque[str] = deque(maxlen=6)
        self.new_lines: deque[str] = deque(maxlen=6)
        self.last_output: float | None = None
        self.last_record: float = self.started
        self.last_render: float = self.started - 1
        self._initialize_observation()

    def _initialize_observation(self) -> None:
        self.paths: dict[Path, _RegisteredLog] = {}
        self._target_stages: dict[str, str] = {}
        self.files: list[_FileSource] = []
        self.pipe_lines: dict[str, _Lines] = {}
        # Captures already retain full output in memory. Buffer observations too,
        # so a fast tool cannot discard transcript bytes while the writer catches up.
        self.samples: queue.SimpleQueue[_Sample] = queue.SimpleQueue()
        self._command_lock = threading.Lock()
        self._active_commands = 0
        self._execution_role: str | None = None
        # Concurrent lanes, in open order; closed lanes stay until their lines are flushed.
        self._lanes: list[_Lane] = []
        # Per pipe source: the lane and production-time label of its latest chunk,
        # and a stage set by an in-band ``BOOLEY_STAGE:`` marker.
        self._source_lanes: dict[str, tuple[_Lane, _Snapshot]] = {}
        self._source_stages: dict[str, str] = {}
        self.closed: bool = False
        self.abandoned: bool = False
        self.rows: int = 0
        self.log: BinaryIO | None = None
        self.log_path: Path | None = None
        self.directory: Path | None = None
        self.log_failed: bool = False
        self.lock: threading.RLock = threading.RLock()
        self.stop: threading.Event = threading.Event()
        self.worker: threading.Thread | None = None

    @contextmanager
    def installed(self) -> Iterator[TerminalProgress]:
        token = _CURRENT.set(self)
        try:
            yield self
        finally:
            self.close()
            _CURRENT.reset(token)

    def _write(self, text: str) -> None:
        try:
            try:
                fd = self.stream.fileno()
            except (OSError, ValueError):
                fd = None
            if (
                fd is not None
                and os.name != "nt"
                and (os.isatty(fd) or stat.S_ISFIFO(os.fstat(fd).st_mode))
            ):
                self._write_ready_fd(fd, text)
            else:
                self.stream.write(text)
                self.stream.flush()
        except Exception:
            logging.getLogger(__name__).debug("Human terminal unavailable", exc_info=True)
            self.log_failed = True
            self.log_path = None
            self.stop.set()

    def _write_ready_fd(self, fd: int, text: str) -> None:
        payload = text.encode(getattr(self.stream, "encoding", None) or "utf-8", errors="replace")
        offset = 0
        for _ in range(len(payload) + 1):
            if offset >= len(payload):
                return
            _, ready, _ = select.select([], [fd], [], 0.1)
            if not ready:
                raise TimeoutError("human terminal output is blocked")
            # Below PIPE_BUF; writable POSIX pipes accept this without waiting.
            offset += os.write(fd, payload[offset : offset + 1024])

    def _record(
        self,
        text: str,
        *,
        target: str | None = None,
        stage: str | None = None,
        lane: _Lane | None = None,
    ) -> None:
        self._clear()
        # A lane record defaults to the lane's own Target and stage, never the shared ones.
        default_target = self.target if lane is None else lane.target
        default_stage = self.stage if lane is None else lane.stage
        lane_field = "" if lane is None else f"lane={lane.label} "
        self._write(
            f"[booley-progress] {utc_now_rfc3339()} {self.flow} "
            f"Target={sanitize(default_target if target is None else target)} "
            f"{lane_field}"
            f"stage={sanitize(_record_text(default_stage if stage is None else stage))} "
            f"elapsed={int(self.clock() - self.started)}s "
            f"{sanitize(text)}\n"
        )

    def stage_changed(self, stage: str, *, target: str | None = None) -> None:
        """Record a stage change from the producing thread.

        Inside ``progress_lane`` only that lane changes; otherwise the shared
        stage does. Only producer-thread entry points read the lane, so the
        writer thread (which has no lane context) never credits a lane.

        Serial-only: ``endpoint_reporting.emit_progress``/``emit_completion``
        reach this method, but no Simulation Campaign lane calls them.
        """
        lane = _LANE.get()
        if lane is not None:
            self._lane_stage_changed(lane, stage, target)
        else:
            self._serial_stage_changed(stage, target)

    def _lane_stage_changed(self, lane: _Lane, stage: str, target: str | None) -> None:
        if self.abandoned:
            return
        with self.lock:
            new_stage = ProgressText(sanitize(stage), record_text=sanitize(_record_text(stage)))
            if (
                new_stage == lane.stage
                and _record_text(new_stage) == _record_text(lane.stage)
                and (target is None or target == lane.target)
            ):
                return
            if target is not None:
                lane.target = target
            lane.execution_role = next(
                (role for role in ("candidate", "baseline") if new_stage.startswith(role + ": ")),
                None,
            )
            lane.stage = new_stage
            self._record("", lane=lane)

    def _serial_stage_changed(self, stage: str, target: str | None) -> None:
        if self.abandoned:
            return
        with self.lock:
            new_stage = ProgressText(sanitize(stage), record_text=sanitize(_record_text(stage)))
            owner = self.target if target is None else target
            self._target_stages[owner] = new_stage
            if (
                new_stage == self.stage
                and _record_text(new_stage) == _record_text(self.stage)
                and (target is None or target == self.target)
            ):
                return
            if target is not None:
                self.target = target
            self._execution_role = next(
                (role for role in ("candidate", "baseline") if new_stage.startswith(role + ": ")),
                None,
            )
            self.stage = new_stage
            self._record("")

    def log_path_known(
        self, path: Path, *, target: str | None = None, temporary: bool = False
    ) -> None:
        """Register a run log. Serial-only: only elaboration-only and standalone
        paths reach it, never a Simulation Campaign lane."""
        if self.abandoned:
            return
        with self.lock:
            owner = self.target if target is None else target
            stage = self._target_stages.get(owner, "preparing")
            self.paths[path] = _RegisteredLog(owner, stage, temporary)
            self._record(
                f"log={path}" + (" (temporary; copied to transcript)" if temporary else ""),
                target=owner,
                stage=stage,
            )

    def _open_transcript(self) -> None:
        if self.log is not None or self.log_failed:
            return
        try:
            root = runtime_dir(self.work_dir) / "flow-console"
            root.mkdir(parents=True, exist_ok=True)
            self.directory = Path(tempfile.mkdtemp(prefix="invocation-", dir=root))
            (self.directory / "active").touch()
            self.log_path = self.directory / "tool-output.log"
            self.log = self.log_path.open("ab", buffering=0)
            self._record(f"transcript={self.log_path}")
        except (OSError, RuntimeError, ValueError):
            self._log_failure()

    def _log_failure(self) -> None:
        if not self.log_failed:
            self._record(
                "Observation transcript incomplete/unavailable; check Project runtime disk permissions and space"
            )
        self.log_failed = True
        self.log_path = None

    def begin_command(self, stage: str | None = None) -> None:
        if self.abandoned:
            return
        with self._command_lock, self.lock:
            self._active_commands += 1
            self._open_transcript()
            lane = _LANE.get()
            if lane is None:
                self.command_started = self.clock()
                self.tail.clear()
                self.last_output = None
            else:
                # One lane's command start never clears another lane's tail.
                lane.tail.clear()
                lane.last_output = None
                self.command_started = self.clock()
            if stage is not None:
                self.stage_changed(stage)
            self._record("command started", lane=lane)
            if self.worker is None:
                self.stop.clear()
                # An empty context keeps lane lookups out of the writer thread, even
                # on interpreters where new threads inherit the caller's context.
                self.worker = threading.Thread(
                    target=contextvars.Context().run, args=(self._run,), daemon=True
                )
                self.worker.start()

    def watch(
        self, paths: list[Path], *, target: str | None = None, temporary: bool = False
    ) -> None:
        """Replace the watched stage files. Serial-only: it swaps the whole set,
        and only synth/FPGA stages call it, never a Simulation Campaign lane."""
        if self.abandoned:
            return
        with self.lock:
            self.files = [_FileSource(path) for path in paths]
            for path in paths:
                self.log_path_known(path, target=target, temporary=temporary)

    def observe(self, source: str, chunk: bytes) -> None:
        """Never block a pipe reader on terminal or disk I/O.

        The calling lane and its current label are captured here, at
        production, because the writer consumes samples later.
        """
        if self.abandoned:
            return
        lane = _LANE.get()
        snapshot = None if lane is None else (lane.target, lane.stage)
        self.samples.put((source, chunk, lane, snapshot))

    def _consume(
        self,
        source: str,
        chunk: bytes,
        lines: _Lines,
        *,
        lane: _Lane | None = None,
        snapshot: _Snapshot | None = None,
    ) -> None:
        if self.log is not None and not self.log_failed:
            try:
                if lane is None or snapshot is None:
                    where = f"Target={self.target} stage={_record_text(self.stage)}"
                else:
                    stage = self._source_stages.get(source, snapshot[1])
                    where = f"Target={snapshot[0]} lane={lane.label} stage={_record_text(stage)}"
                label = f"\n[{self.flow} {where} source={source}]\n"
                self.log.write(label.encode("utf-8") + chunk)
            except OSError:
                self._log_failure()
        if not self.abandoned:
            for line in lines.feed(chunk, tail_only=not self.tool_output):
                self._line(line, source, lane=lane, snapshot=snapshot)

    def _line(
        self,
        line: str,
        source: str,
        *,
        lane: _Lane | None = None,
        snapshot: _Snapshot | None = None,
    ) -> None:
        clean = sanitize(line)
        marker = _STAGE.fullmatch(clean)
        if marker:
            if lane is None:
                self._serial_stage_changed(self._stage_label(marker[1]), None)
            else:
                self._lane_marker(lane, source, marker[1])
            return
        if _ENVELOPE.match(clean) or _OWNED_RECORD.match(clean):
            return
        if lane is not None and snapshot is not None:
            self._lane_line(lane, snapshot, source, clean)
            return
        # Synth make prints failure-tail copies of the authoritative stage logs.
        if (
            self.flow in {"synth", "fpga"}
            and self.files
            and source.split(":", 1)[0] in {"stdout", "stderr"}
        ):
            return
        if self.flow == "fpga" and source.endswith("runme.log"):
            normalized_source = source.replace("\\", "/")
            if "/synth_1/" in normalized_source:
                self._vivado_stage("synthesis")
            elif "/impl_1/" in normalized_source:
                self._vivado_stage("implementation")
        if self.flow == "fpga" and re.match(
            r"^(?:Starting|Running) (?:report_timing|report_utilization)", clean
        ):
            self._vivado_stage("post-route reports")
        self.last_output = self.clock()
        self.tail.append(clean)
        self.new_lines.append(clean)
        if self.tool_output:
            self._record(f"{source}: {clean}")

    def _lane_marker(self, lane: _Lane, source: str, name: str) -> None:
        """Apply an in-band stage marker to its lane and the rest of its source."""
        label = f"{lane.execution_role}: {name}" if lane.execution_role else name
        self._lane_stage_changed(lane, label, None)
        self._source_stages[source] = lane.stage

    def _lane_line(self, lane: _Lane, snapshot: _Snapshot, source: str, clean: str) -> None:
        """Attribute one output line to the lane and label it had when produced."""
        target = snapshot[0]
        stage = self._source_stages.get(source, snapshot[1])
        lane.last_output = self.last_output = self.clock()
        lane.tail.append(clean)
        if self.tool_output:
            self._record(f"{source}: {clean}", target=target, stage=stage, lane=lane)
            return
        # Redirected output summarizes a lane's lines when its label changes;
        # a TTY prints them once at the final drain, like serial output.
        if lane.pending and lane.pending[-1][:2] != (target, stage) and not self._terminal()[2]:
            self._flush_lane(lane)
        lane.pending.append((target, stage, clean))

    def _flush_lane(self, lane: _Lane) -> None:
        for target, stage, line in lane.pending:
            self._record(f"output: {line}", target=target, stage=stage, lane=lane)
        lane.pending.clear()

    def _finish_lanes(self) -> None:
        """Print (or, with live tool output, drop) every lane's unflushed lines."""
        for lane in self._lanes:
            if self.tool_output:
                lane.pending.clear()
            else:
                self._flush_lane(lane)
        self._prune_lanes()

    def _open_lanes(self) -> list[_Lane]:
        return [lane for lane in self._lanes if not lane.closed]

    def _prune_lanes(self) -> None:
        self._lanes = [lane for lane in self._lanes if not (lane.closed and not lane.pending)]

    def _open_lane(self, label: str) -> _Lane:
        with self.lock:
            lane = _Lane(
                label="_".join(sanitize(label).split()), stage="preparing", target=self.target
            )
            self._lanes.append(lane)
            return lane

    def _close_lane(self, lane: _Lane) -> None:
        with self.lock:
            lane.closed = True

    def _stage_label(self, stage: str) -> str:
        return f"{self._execution_role}: {stage}" if self._execution_role else stage

    def _vivado_stage(self, stage: str) -> None:
        """Serial-only: Vivado stage detection runs for FPGA stage files, never in a lane."""
        self._serial_stage_changed(self._stage_label(stage), None)

    def _terminal(self) -> tuple[int, int, bool]:
        """Return ``(width, height, interactive)`` for the human stream."""
        try:
            width, height = self.dimensions()
        except Exception:
            logging.getLogger(__name__).debug("Terminal dimensions unavailable", exc_info=True)
            width, height = 80, 0
        tty = self.stream.isatty() and os.environ.get("TERM") != "dumb" and height >= 8
        return width, height, tty

    def tick(self) -> None:
        """Render from monotonic time, even when the command emits no output."""
        now = self.clock()
        width, height, tty = self._terminal()
        open_lanes = self._open_lanes()
        if tty and not self.tool_output:
            if now - self.last_render < 0.25:
                return
            self._clear()
            age = "none" if self.last_output is None else format_age(0, now=now - self.last_output)
            stage = f"lanes={len(open_lanes)}" if open_lanes else self.stage
            status = (
                f"{self.flow} elapsed={format_duration(now - self.started)} command={format_duration(now - self.command_started)} "
                f"Target={self.target[:20]} {stage[:24]} last output={age}"
            )
            rows = [status, *self.tail, *self._lane_rows(open_lanes, now)][: max(1, height - 1)]
            self._write(
                "\n".join(fit_width(sanitize(row), max(1, width - 1)) for row in rows) + "\n"
            )
            self.rows = len(rows)
            self.last_render = now
        elif now - self.last_record >= 5:
            self._record(
                "heartbeat (elapsed time; process health unknown)",
                stage=f"lanes={len(open_lanes)}" if open_lanes else None,
            )
            if not self.tool_output:
                for line in self.new_lines:
                    self._record(f"output: {line}")
                for lane in self._lanes:
                    self._flush_lane(lane)
            self.new_lines.clear()
            self.last_record = now
        self._prune_lanes()

    def _lane_rows(self, lanes: list[_Lane], now: float) -> list[str]:
        """One header row per open lane, followed by its share of recent output."""
        share = max(1, 6 // len(lanes)) if lanes else 0
        rows: list[str] = []
        for lane in lanes:
            age = "none" if lane.last_output is None else format_age(0, now=now - lane.last_output)
            rows.append(f"lane={lane.label} stage={lane.stage} last output={age}")
            rows.extend(list(lane.tail)[-share:])
        return rows

    def _clear(self) -> None:
        if self.rows:
            self._write(f"\x1b[{self.rows}A\x1b[J")
            self.rows = 0

    def _poll(self) -> None:
        if self.abandoned:
            return
        with self.lock:
            for _ in range(256):
                if self.abandoned:
                    return
                try:
                    source, chunk, lane, snapshot = self.samples.get_nowait()
                except queue.Empty:
                    break
                lines = self.pipe_lines.setdefault(source, _Lines())
                if lane is not None and snapshot is not None:
                    self._source_lanes[source] = (lane, snapshot)
                self._consume(source, chunk, lines, lane=lane, snapshot=snapshot)
            for source in self.files:
                if self.abandoned:
                    return
                chunk = source.read()
                if chunk:
                    self._consume(str(source.path), chunk, source.lines)
            self.tick()

    def _run(self) -> None:
        try:
            while not self.stop.wait(0.1):
                self._poll()
            if not self.abandoned:
                self._drain_end()
        except Exception:
            logging.getLogger(__name__).debug("Human observation failed", exc_info=True)
            self._log_failure()
        finally:
            if self.abandoned:
                self._release_transcript()

    def end_command(self) -> None:
        """Bound writer shutdown independently of the subprocess pipe readers."""
        with self._command_lock:
            self._active_commands = max(0, self._active_commands - 1)
            if self._active_commands:
                return
            self._stop_worker()

    def _stop_worker(self) -> None:
        self.stop.set()
        if self.worker is None:
            self._drain_end()
            return
        self.worker.join(timeout=2)
        if self.worker.is_alive():
            self.abandoned = True
            self.files.clear()
            self._log_failure()
            return
        self.worker = None

    def _drain_end(self) -> None:
        # Drain finite current-file backlogs while still inside the workspace lease.
        for _ in range(1 + (self.samples.qsize() + 255) // 256):
            self._poll()
        with self.lock:
            for source in self.files:
                signature = source._stat()
                remaining = max(0, signature[1] - source.offset) if signature else 0
                for _ in range((remaining + 65535) // 65536):
                    if self.abandoned:
                        return
                    chunk = source.read()
                    if chunk:
                        self._consume(str(source.path), chunk, source.lines)
                for line in source.lines.feed(b"", final=True):
                    self._line(line, str(source.path))
            for name, lines in self.pipe_lines.items():
                lane, snapshot = self._source_lanes.get(name, (None, None))
                for line in lines.feed(b"", final=True):
                    self._line(line, name, lane=lane, snapshot=snapshot)
            self.pipe_lines.clear()
            self._source_lanes.clear()
            self._source_stages.clear()
            self.files.clear()
            self._clear()
            if not self.tool_output:
                for line in self.new_lines:
                    self._record(f"output: {line}")
                self.new_lines.clear()
            self._finish_lanes()

    def close(self) -> None:
        if self.closed:
            return
        self.stop.set()
        if self.worker is not None:
            self.worker.join(timeout=2)
            if self.worker.is_alive():
                self.abandoned = True
                self.files.clear()
                self._log_failure()
                self.closed = True
                return
        with self.lock:
            self._clear()
            self._release_transcript()
            self.closed = True

    def _release_transcript(self) -> None:
        try:
            if self.log is not None:
                self.log.close()
            if self.directory is not None:
                (self.directory / "active").unlink(missing_ok=True)
        except OSError:
            self._log_failure()

    def footer(self) -> None:
        """Print final log paths. Serial-only: runs after the invocation, with no lane open."""
        if self.log_path is not None:
            self._record(f"transcript={self.log_path}")
        for path, registration in self.paths.items():
            if not registration.temporary and path.is_file():
                self._record(f"log={path}", target=registration.target, stage=registration.stage)


def announce_unit(stage: str, *, target: str | None = None) -> None:
    """Offer work-unit identity to the optional human observer."""
    observer = current_progress()
    if observer is not None:
        observer.stage_changed(stage, target=target)


@contextmanager
def progress_lane(label: str) -> Iterator[None]:
    """Scope progress to one concurrently running unit of work.

    Inside the block, stage changes, command starts and observed output belong
    to a lane named ``label`` instead of the shared invocation state, so
    parallel units cannot relabel, evict or clear each other's progress. With
    no observer installed this does nothing. Outside any lane, behavior and
    output are exactly the serial format.
    """
    observer = current_progress()
    if observer is None:
        yield
        return
    lane = observer._open_lane(label)
    token = _LANE.set(lane)
    try:
        yield
    finally:
        _LANE.reset(token)
        observer._close_lane(lane)


def observe_logs(paths: list[Path], *, target: str | None = None, temporary: bool = False) -> None:
    """Register a bounded known set before dispatch, within the build lease."""
    observer = current_progress()
    if observer is not None:
        observer.watch(paths, target=target, temporary=temporary)

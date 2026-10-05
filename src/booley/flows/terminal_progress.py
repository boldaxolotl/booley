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
from pathlib import Path
from typing import BinaryIO, TextIO

from booley.runtime.project_dir import runtime_dir
from booley.runtime.timefmt import utc_now_rfc3339

_CURRENT: contextvars.ContextVar[TerminalProgress | None] = contextvars.ContextVar(
    "flow_terminal_progress", default=None
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
        self.paths: dict[Path, bool] = {}
        self.files: list[_FileSource] = []
        self.pipe_lines: dict[str, _Lines] = {}
        # Captures already retain full output in memory. Buffer observations too,
        # so a fast tool cannot discard transcript bytes while the writer catches up.
        self.samples: queue.SimpleQueue[tuple[str, bytes]] = queue.SimpleQueue()
        self._command_lock = threading.Lock()
        self._active_commands = 0
        self._execution_role: str | None = None
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

    def _record(self, text: str) -> None:
        self._clear()
        self._write(
            f"[booley-progress] {utc_now_rfc3339()} {self.flow} Target={sanitize(self.target)} "
            f"stage={sanitize(self.stage)} elapsed={int(self.clock() - self.started)}s "
            f"{sanitize(text)}\n"
        )

    def stage_changed(self, stage: str, *, target: str | None = None) -> None:
        if self.abandoned:
            return
        with self.lock:
            new_stage = sanitize(stage)
            if new_stage == self.stage and (target is None or target == self.target):
                return
            if target is not None:
                self.target = target
            self._execution_role = next(
                (role for role in ("candidate", "baseline") if new_stage.startswith(role + ": ")),
                None,
            )
            self.stage = new_stage
            self._record("")

    def log_path_known(self, path: Path, *, temporary: bool = False) -> None:
        if self.abandoned:
            return
        with self.lock:
            self.paths[path] = temporary
            self._record(
                f"log={path}" + (" (temporary; copied to transcript)" if temporary else "")
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
            self.command_started = self.clock()
            self.tail.clear()
            self.last_output = None
            if stage is not None:
                self.stage_changed(stage)
            self._record("command started")
            if self.worker is None:
                self.stop.clear()
                self.worker = threading.Thread(target=self._run, daemon=True)
                self.worker.start()

    def watch(self, paths: list[Path], *, temporary: bool = False) -> None:
        if self.abandoned:
            return
        with self.lock:
            self.files = [_FileSource(path) for path in paths]
            for path in paths:
                self.log_path_known(path, temporary=temporary)

    def observe(self, source: str, chunk: bytes) -> None:
        """Never block a pipe reader on terminal or disk I/O."""
        if self.abandoned:
            return
        self.samples.put((source, chunk))

    def _consume(self, source: str, chunk: bytes, lines: _Lines) -> None:
        if self.log is not None and not self.log_failed:
            try:
                label = (
                    f"\n[{self.flow} Target={self.target} stage={self.stage} source={source}]\n"
                )
                self.log.write(label.encode("utf-8") + chunk)
            except OSError:
                self._log_failure()
        if not self.abandoned:
            for line in lines.feed(chunk, tail_only=not self.tool_output):
                self._line(line, source)

    def _line(self, line: str, source: str) -> None:
        clean = sanitize(line)
        marker = _STAGE.fullmatch(clean)
        if marker:
            self.stage_changed(self._stage_label(marker[1]))
            return
        if _ENVELOPE.match(clean) or _OWNED_RECORD.match(clean):
            return
        # Synth make prints failure-tail copies of the authoritative stage logs.
        if (
            self.flow in {"synth", "fpga"}
            and self.files
            and source.split(":", 1)[0] in {"stdout", "stderr"}
        ):
            return
        if self.flow == "fpga" and source.endswith("runme.log"):
            if "/synth_1/" in source:
                self._vivado_stage("synthesis")
            elif "/impl_1/" in source:
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

    def _stage_label(self, stage: str) -> str:
        return f"{self._execution_role}: {stage}" if self._execution_role else stage

    def _vivado_stage(self, stage: str) -> None:
        self.stage_changed(self._stage_label(stage))

    def tick(self) -> None:
        """Render from monotonic time, even when the command emits no output."""
        now = self.clock()
        try:
            width, height = self.dimensions()
        except Exception:
            logging.getLogger(__name__).debug("Terminal dimensions unavailable", exc_info=True)
            width, height = 80, 0
        tty = self.stream.isatty() and os.environ.get("TERM") != "dumb" and height >= 8
        if tty and not self.tool_output:
            if now - self.last_render < 0.25:
                return
            self._clear()
            age = "none" if self.last_output is None else f"{int(now - self.last_output)}s ago"
            status = (
                f"{self.flow} elapsed={int(now - self.started)}s command={int(now - self.command_started)}s "
                f"Target={self.target[:20]} {self.stage[:24]} last output={age}"
            )
            rows = [status, *self.tail]
            self._write(
                "\n".join(fit_width(sanitize(row), max(1, width - 1)) for row in rows) + "\n"
            )
            self.rows = len(rows)
            self.last_render = now
        elif now - self.last_record >= 5:
            self._record("heartbeat (elapsed time; process health unknown)")
            if not self.tool_output:
                for line in self.new_lines:
                    self._record(f"output: {line}")
            self.new_lines.clear()
            self.last_record = now

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
                    source, chunk = self.samples.get_nowait()
                except queue.Empty:
                    break
                lines = self.pipe_lines.setdefault(source, _Lines())
                self._consume(source, chunk, lines)
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
                for line in lines.feed(b"", final=True):
                    self._line(line, name)
            self.pipe_lines.clear()
            self.files.clear()
            self._clear()
            if not self.tool_output:
                for line in self.new_lines:
                    self._record(f"output: {line}")
                self.new_lines.clear()

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
        if self.log_path is not None:
            self._record(f"transcript={self.log_path}")
        for path, temporary in self.paths.items():
            if not temporary and path.is_file():
                self._record(f"log={path}")


def announce_unit(stage: str, *, target: str | None = None) -> None:
    """Offer work-unit identity to the optional human observer."""
    observer = current_progress()
    if observer is not None:
        observer.stage_changed(stage, target=target)


def observe_logs(paths: list[Path], *, temporary: bool = False) -> None:
    """Register a bounded known set before dispatch, within the build lease."""
    observer = current_progress()
    if observer is not None:
        observer.watch(paths, temporary=temporary)

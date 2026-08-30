"""Execute host-side Docker builds with bounded, visible progress."""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field
from queue import Empty, Full, Queue
from typing import TextIO

HEARTBEAT_INTERVAL_S = 60.0
_QUEUE_SIZE = 256
_EOF = object()
_SALIENT_WORDS = ("error", "failed", "fatal")


@dataclass(frozen=True)
class _CaptureFailure:
    error: OSError | ValueError


@dataclass(frozen=True)
class DockerBuildResult:
    """Observable outcome of one Docker build command."""

    returncode: int | None
    timed_out: bool = False
    diagnostics: tuple[str, ...] = ()


def _emit(output: TextIO, text: str) -> bool:
    try:
        output.write(text.rstrip("\n") + "\n")
        output.flush()
    except (OSError, UnicodeError, ValueError):
        return False
    return True


def _put_record(records: Queue[object], record: object, stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            records.put(record, timeout=0.1)
            return
        except Full:
            continue


def _read_output(stream: TextIO, records: Queue[object], stop: threading.Event) -> None:
    try:
        for line in stream:
            _put_record(records, line, stop)
            if stop.is_set():
                return
    except (OSError, ValueError) as exc:
        _put_record(records, _CaptureFailure(exc), stop)
    finally:
        _put_record(records, _EOF, stop)


def _terminate(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _present_progress(output: TextIO, line: str, *, verbose: bool, last: str) -> tuple[str, bool]:
    if verbose:
        return line, _emit(output, line)
    if ">>>" not in line:
        return last, False
    progress = line[line.index(">>>") :].strip().split('"', 1)[0].rstrip(" \\")
    if progress and progress != last:
        return progress, _emit(output, progress)
    return last, False


def _diagnostics(tail: deque[tuple[int, str]], salient: deque[tuple[int, str]]) -> tuple[str, ...]:
    retained = dict((*salient, *tail))
    return tuple(
        retained[sequence] for sequence in sorted(retained) if ">>>" not in retained[sequence]
    )


@dataclass
class _ProgressState:
    image: str
    verbose: bool
    output: TextIO
    started: float
    deadline: float
    last_visible: float
    last_progress: str = ""
    sequence: int = 0
    tail: deque[tuple[int, str]] = field(default_factory=lambda: deque(maxlen=100))
    salient: deque[tuple[int, str]] = field(default_factory=lambda: deque(maxlen=20))

    def accept(self, record: str) -> None:
        line = record.rstrip("\n")
        self.tail.append((self.sequence, line))
        if any(word in line.lower() for word in _SALIENT_WORDS):
            self.salient.append((self.sequence, line))
        self.sequence += 1
        self.last_progress, emitted = _present_progress(
            self.output, record, verbose=self.verbose, last=self.last_progress
        )
        if emitted:
            self.last_visible = time.monotonic()

    def heartbeat(self, now: float) -> None:
        if now - self.last_visible < HEARTBEAT_INTERVAL_S:
            return
        _emit(self.output, f"  * [{self.image} build] elapsed: {now - self.started:.1f}s")
        self.last_visible = now


def _pump(process: subprocess.Popen[str], records: Queue[object], state: _ProgressState) -> bool:
    while True:
        now = time.monotonic()
        if now >= state.deadline:
            return process.poll() is None
        try:
            record = records.get(timeout=min(0.1, max(state.deadline - now, 0.001)))
        except Empty:
            record = None
        if record is _EOF:
            return False
        if isinstance(record, _CaptureFailure):
            raise OSError(
                f"{state.image} Docker build output capture failed: {record.error}"
            ) from record.error
        if isinstance(record, str):
            state.accept(record)
        state.heartbeat(time.monotonic())


def _await_exit(process: subprocess.Popen[str], deadline: float, timed_out: bool) -> bool:
    if timed_out:
        _terminate(process)
        return True
    try:
        process.wait(timeout=max(deadline - time.monotonic(), 0.001))
    except subprocess.TimeoutExpired:
        _terminate(process)
        return True
    return False


def _close_capture(
    process: subprocess.Popen[str], stop: threading.Event, reader: threading.Thread
) -> None:
    stop.set()
    if process.poll() is None:
        _terminate(process)
    assert process.stdout is not None
    process.stdout.close()
    reader.join(timeout=1)


def _run_captured(
    command: Sequence[str], *, image: str, verbose: bool, timeout: float, output: TextIO
) -> DockerBuildResult:
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if process.stdout is None:
        raise OSError("Docker build output pipe was not created")
    records: Queue[object] = Queue(maxsize=_QUEUE_SIZE)
    stop = threading.Event()
    reader = threading.Thread(
        target=_read_output, args=(process.stdout, records, stop), daemon=True
    )
    reader.start()
    started = time.monotonic()
    state = _ProgressState(image, verbose, output, started, started + timeout, started)
    try:
        timed_out = _pump(process, records, state)
        timed_out = _await_exit(process, state.deadline, timed_out)
    finally:
        _close_capture(process, stop, reader)
    diagnostics = (
        _diagnostics(state.tail, state.salient)
        if (timed_out or process.returncode) and not verbose
        else ()
    )
    return DockerBuildResult(None if timed_out else process.returncode, timed_out, diagnostics)


def run_docker_build(
    command: Sequence[str],
    *,
    image: str,
    verbose: bool,
    timeout: float,
    output: TextIO | None = None,
) -> DockerBuildResult:
    """Run one Docker build while keeping useful progress observable."""
    sink = sys.stdout if output is None else output
    if verbose and sink.isatty():
        try:
            completed = subprocess.run(command, timeout=timeout, check=False)
        except subprocess.TimeoutExpired:
            return DockerBuildResult(None, timed_out=True)
        return DockerBuildResult(completed.returncode)
    return _run_captured(command, image=image, verbose=verbose, timeout=timeout, output=sink)

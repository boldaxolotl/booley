"""Stream declared ``[flows.sim].trace_files`` VCDs through named pipes.

On POSIX with the bwave binary, a literal relative ``.vcd`` entry inside the run
directory becomes a named pipe before the simulator starts. A ``bwave build``
process reads it while the test runs, so the raw VCD never lands on disk. Any
entry that cannot stream keeps the post-run conversion path untouched.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

from booley.bwave import waveform_store
from booley.bwave.waveform_store import StreamingConversion, start_streaming_conversion
from booley.flows.sim.trace_session import TraceSession

_GLOB_MAGIC = "*?["
_STREAM_DIR = "trace_streams"


@dataclass(frozen=True)
class _Stream:
    """One declared entry being converted while the simulator writes it."""

    pattern: str
    sidecar: Path
    conversion: StreamingConversion


def _eligible_path(pattern: str, run_root: Path) -> Path | None:
    """The pipe path for a literal relative ``.vcd`` entry inside *run_root*, else ``None``."""
    entry = Path(pattern)
    if any(char in pattern for char in _GLOB_MAGIC) or entry.is_absolute():
        return None
    if entry.suffix.lower() != ".vcd":
        return None
    path = run_root / entry
    return path if path.resolve().is_relative_to(run_root) else None


def _sidecar_path(pipe: Path) -> Path:
    """The ownership record that sits beside a pipe Booley created."""
    return pipe.with_name(f".{pipe.name}.booley-pipe")


def _is_booley_stale_pipe(pipe: Path, status: os.stat_result) -> bool:
    """True when *pipe* is a FIFO whose sidecar records exactly its identity."""
    if not stat.S_ISFIFO(status.st_mode):
        return False
    try:
        record: object = json.loads(_sidecar_path(pipe).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return record == {"dev": status.st_dev, "ino": status.st_ino}


class DeclaredTraceStreams:
    """The pipes of one run's declared VCD entries and what became of each."""

    def __init__(self, session: TraceSession) -> None:
        self._session = session
        self._streams: list[_Stream] = []
        # Declared entry -> queryable store a finished stream produced.
        self.stores: dict[str, Path] = {}
        # Declared entry -> why its stream failed.
        self.failures: dict[str, str] = {}

    @classmethod
    def start(
        cls, session: TraceSession, patterns: tuple[str, ...], run_cwd: Path
    ) -> DeclaredTraceStreams:
        """Create a pipe and converter for every entry that may stream."""
        streams = cls(session)
        if not waveform_store.can_stream_waveform():
            return streams
        run_root = run_cwd.resolve()
        out_dir = session.work_dir / _STREAM_DIR
        shutil.rmtree(out_dir, ignore_errors=True)
        claimed: set[Path] = set()
        for index, pattern in enumerate(patterns):
            path = _eligible_path(pattern, run_root)
            if path is None or path.resolve() in claimed:
                continue
            claimed.add(path.resolve())
            stem = f"{index}-{Path(pattern).stem}"
            streams._open(pattern, path, out_dir / f"{stem}.fst", out_dir / f"{stem}.stderr")
        return streams

    def _open(self, pattern: str, path: Path, store: Path, stderr: Path) -> None:
        """Claim *path* as a pipe and start converting it, or leave it to post-run conversion."""
        session = self._session
        if not _clear_stale_pipe(path):
            session.record_attempt(
                "declared_stream", "skipped", detail=f"{pattern}: something already exists there"
            )
            return
        conversion = start_streaming_conversion(
            path,
            store,
            scope=session.trace_scope,
            stderr_path=stderr,
            replace_existing=False,
        )
        if conversion is None:
            session.record_attempt(
                "declared_stream", "skipped", detail=f"{pattern}: pipe could not be created"
            )
            return
        status = path.lstat()
        sidecar = _sidecar_path(path)
        with contextlib.suppress(OSError):
            sidecar.write_text(
                json.dumps({"dev": status.st_dev, "ino": status.st_ino}), encoding="utf-8"
            )
        self._streams.append(_Stream(pattern, sidecar, conversion))
        session.record_attempt("declared_stream", "started", detail=pattern)
        print(f"[bwave] streaming {pattern} through a pipe")

    def watch(self, sim_proc: subprocess.Popen[str]) -> None:
        """Monitor every converter for stalls without ever killing *sim_proc*."""
        for stream in self._streams:
            self._session.start_monitor(stream.conversion, sim_proc, spare_simulator=True)

    def close_inputs(self) -> None:
        """Give every converter EOF and remove the pipes and sidecars Booley created."""
        for stream in self._streams:
            stream.conversion.close_input()
            stream.sidecar.unlink(missing_ok=True)

    def finish(self) -> None:
        """Wait for each converter within the conversion budget and record its outcome."""
        session = self._session
        for stream in self._streams:
            result = stream.conversion.finish(session.budget_remaining_s())
            last_line = result.detail.splitlines()[-1:] or ["no usable waveform was written"]
            if result.success:
                self.stores[stream.pattern] = stream.conversion.store_path
            else:
                self.failures[stream.pattern] = (
                    f"streaming {stream.pattern} failed: {last_line[0]}"
                )
            return_code = result.attempts[-1].return_code
            session.record_attempt(
                "declared_stream",
                "success" if result.success else "failed",
                detail=stream.pattern,
                return_code=return_code,
            )


def _clear_stale_pipe(path: Path) -> bool:
    """True when *path* is free; a pipe Booley provably left behind is removed first."""
    try:
        status = path.lstat()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    if not _is_booley_stale_pipe(path, status):
        return False
    path.unlink()
    _sidecar_path(path).unlink(missing_ok=True)
    return True

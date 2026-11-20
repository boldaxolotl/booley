"""Stream declared ``[flows.sim].trace_files`` VCDs through named pipes.

On POSIX with the bwave binary, a literal relative ``.vcd`` entry inside the run
directory becomes a named pipe before the simulator starts. A ``bwave build``
process reads it while the test runs, so the raw VCD never lands on disk. Any
entry that cannot stream keeps the post-run conversion path untouched. The
Booley Directory's ownership record names each pipe, so a sweep removes what a
killed run left behind.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

from booley.bwave import waveform_store
from booley.bwave.waveform_store import StreamingConversion, start_streaming_conversion
from booley.flows.sim.staged_input_ledger import StagedInputLedger, sweep_cancelled_run_inputs
from booley.flows.sim.trace_session import TraceSession

_GLOB_MAGIC = "*?["
_STREAM_DIR = "trace_streams"
_TESTBENCH_FIX_HINT = (
    " (if the testbench seeks in, reads back, or reopens this dump, make it write the dump "
    "once, front to back, or dump FST instead)"
)


@dataclass(frozen=True)
class _Stream:
    """One declared entry being converted while the simulator writes it."""

    pattern: str
    ledger: StagedInputLedger
    conversion: StreamingConversion


def _eligible_path(pattern: str, run_root: Path) -> Path | None:
    """The pipe path for a literal relative ``.vcd`` entry inside *run_root*, else ``None``."""
    entry = Path(pattern)
    if any(char in pattern for char in _GLOB_MAGIC) or entry.is_absolute():
        return None
    if entry.suffix.lower() != ".vcd":
        return None
    path = run_root / entry
    if not path.resolve().is_relative_to(run_root):
        return None
    # Real directories only: the ownership record's sweep requires them.
    return path.parent.resolve() / path.name


class DeclaredTraceStreams:
    """The pipes of one run's declared VCD entries and what became of each."""

    def __init__(self, session: TraceSession, booley_dir: Path, run_root: Path) -> None:
        self._session = session
        self._booley_dir = booley_dir
        self._run_root = run_root
        self._streams: list[_Stream] = []
        # Declared entry -> queryable store a finished stream produced.
        self.stores: dict[str, Path] = {}
        # Declared entry -> why its stream failed.
        self.failures: dict[str, str] = {}

    @classmethod
    def start(
        cls, session: TraceSession, patterns: tuple[str, ...], run_cwd: Path, booley_dir: Path
    ) -> DeclaredTraceStreams:
        """Create a pipe and converter for every entry that may stream."""
        run_root = run_cwd.resolve()
        streams = cls(session, booley_dir, run_root)
        if not waveform_store.can_stream_waveform():
            return streams
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
        # The ledger's sweep runs first, so a dead run's pipe is gone before the path is examined.
        ledger = StagedInputLedger(self._booley_dir, self._run_root)
        taken = os.path.lexists(path)
        conversion = None if taken else self._create_pipe(ledger, path, store, stderr)
        if conversion is None:
            why = "something already exists there" if taken else "pipe could not be created"
            session.record_attempt("declared_stream", "skipped", detail=f"{pattern}: {why}")
            return
        self._streams.append(_Stream(pattern, ledger, conversion))
        session.record_attempt("declared_stream", "started", detail=pattern)
        print(f"[bwave] streaming {pattern} through a pipe")

    def _create_pipe(
        self, ledger: StagedInputLedger, path: Path, store: Path, stderr: Path
    ) -> StreamingConversion | None:
        """Make the pipe under a recorded random name, then link it to *path*; ``None`` on failure."""
        relative = path.relative_to(self._run_root)
        staged = path.with_name(f".{path.name}.{uuid.uuid4().hex}.booley-pipe")
        conversion = None
        try:
            ledger.will_create(relative, kind="pipe", identity=staged.name)
            conversion = start_streaming_conversion(
                staged,
                store,
                scope=self._session.trace_scope,
                stderr_path=stderr,
                replace_existing=False,
            )
            if conversion is not None:
                ledger.created(relative, staged.lstat())
                # Fails instead of replacing whatever appeared at *path* meanwhile.
                conversion.link_pipe_to(path)
                return conversion
        except OSError:
            if conversion is not None:
                conversion.finish(0)
        ledger.discard()
        return None

    def watch(self, sim_proc: subprocess.Popen[str]) -> None:
        """Monitor every converter for stalls without ever killing *sim_proc*."""
        for stream in self._streams:
            self._session.start_monitor(stream.conversion, sim_proc, spare_simulator=True)

    def stop_watch(self) -> None:
        """End the stall monitors :meth:`watch` started and wait for their threads."""
        self._session.stop_monitor()

    def close_inputs(self) -> None:
        """Give every converter EOF and remove the pipes Booley created and their record."""
        for stream in self._streams:
            stream.conversion.close_input()
            stream.ledger.discard()

    def finish(self) -> None:
        """Wait for each converter within the conversion budget and record its outcome."""
        session = self._session
        for stream in self._streams:
            result = stream.conversion.finish(session.budget_remaining_s())
            session.note_conversion(result)
            last_line = result.detail.splitlines()[-1:] or ["no usable waveform was written"]
            if result.success:
                self.stores[stream.pattern] = stream.conversion.store_path
            else:
                hint = "" if result.failure_kind == "timed_out" else _TESTBENCH_FIX_HINT
                self.failures[stream.pattern] = (
                    f"streaming {stream.pattern} failed: {last_line[0]}{hint}"
                )
            return_code = result.attempts[-1].return_code
            session.record_attempt(
                "declared_stream",
                "success" if result.success else "failed",
                detail=stream.pattern,
                return_code=return_code,
            )


def clear_stale_pipes(booley_dir: str) -> None:
    """Remove the pipes a killed run's ownership record names, so every launch starts clean."""
    if booley_dir:
        sweep_cancelled_run_inputs(Path(booley_dir))

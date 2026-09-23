"""Simulation-owned trace attempt, freshness, evidence, and publication policy."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from booley.bwave.waveform_store import (
    ConversionResult,
    ExistingStorePolicy,
    StreamingConversion,
    conversion_failure_messages,
    convert_vcd,
    discover_waveform,
    inspect_store,
    start_streaming_conversion,
    waveform_cache_dir,
)
from booley.runtime.timefmt import utc_now_rfc3339

TRACE_STATUS_SCHEMA_VERSION = 1
TRACE_METADATA_PREFIX = "TRACE_METADATA: "


@dataclass(frozen=True)
class TraceArtifact:
    """A waveform proven queryable by the same B-Wave reader users invoke."""

    path: Path
    size_bytes: int
    top_scope: str
    signal_count: int
    total_ticks: int

    def metadata_line(self) -> str:
        """Stable stdout marker consumed by the parent Simulation Flow."""
        payload = {
            "path": str(self.path),
            "size_bytes": self.size_bytes,
            "top_scope": self.top_scope,
            "signal_count": self.signal_count,
            "total_ticks": self.total_ticks,
        }
        return TRACE_METADATA_PREFIX + json.dumps(payload, separators=(",", ":"))


@dataclass(frozen=True)
class TraceInspection:
    """Result of validating one retained waveform at the trace seam."""

    artifact: TraceArtifact | None
    failure_reason: str = ""

    @property
    def usable(self) -> bool:
        return self.artifact is not None


def _utc_now() -> str:
    return utc_now_rfc3339()


def trace_cache_key(
    source_files: list[Path],
    scope: str | None = None,
    simulator: str = "",
) -> str:
    """Content-addressed cache key for trace files.

    Hash file contents directly — correctness over speed.
    Cache hit = trace is valid by construction; no mtime races.
    """
    h = hashlib.sha256()
    for f in sorted(source_files):
        h.update(f.name.encode())
        h.update(f.read_bytes())
    if scope:
        h.update(scope.encode())
    if simulator:
        h.update(simulator.encode())
    return h.hexdigest()[:16]


class TraceSession:
    """Own Simulation trace freshness, attempt evidence, and publication.

    When `cache_key` is provided, the cache directory uses content-addressed
    hashing instead of `work_dir.name` — a key change means the old cache
    is a miss by construction, eliminating manual invalidation.
    """

    def __init__(
        self,
        work_dir: Path,
        trace_scope: str | None = None,
        cache_key: str | None = None,
        backend: str = "",
        target: str = "",
        test: str = "",
    ) -> None:
        self._work_dir = work_dir
        self._trace_scope = trace_scope
        self._cache_key = cache_key
        # Stall-kill state — set by start_monitor when it escalates to a kill.
        self._stall_killed = False
        self._stall_message: str | None = None
        now = _utc_now()
        self._status: dict[str, object] = {
            "schema_version": TRACE_STATUS_SCHEMA_VERSION,
            "backend": backend,
            "target": target,
            "test": test,
            "trace_scope": trace_scope or "",
            "created_at": now,
            "updated_at": now,
            "paths": {},
            "attempts": [],
            "events": [],
            "current_status": "initialized",
            "failure_reason": "",
            "return_codes": {},
        }

    @property
    def stall_killed(self) -> bool:
        """True if the monitor killed sim/bwave due to a trace pipeline stall."""
        return self._stall_killed

    @property
    def stall_message(self) -> str | None:
        """Human-readable description of the stall kill, if one occurred."""
        return self._stall_message

    @property
    def cache_dir(self) -> Path:
        """B-Wave-owned collision-resistant cache location."""
        return waveform_cache_dir(self._work_dir, self._cache_key)

    @property
    def bwave_path(self) -> Path:
        return self.cache_dir / "trace.fst"

    @property
    def work_bwave_path(self) -> Path:
        return self._work_dir / "trace.fst"

    @property
    def fifo_path(self) -> Path:
        return self._work_dir / "trace.fifo"

    @property
    def incident_path(self) -> Path:
        return self._work_dir / "trace_incident.txt"

    @property
    def persisted_stderr_path(self) -> Path:
        return self._work_dir / "trace.fst.stderr"

    @property
    def manifest_path(self) -> Path:
        return self._work_dir / "trace_status.json"

    def _manifest_paths(self) -> dict[str, str]:
        return {
            "work_dir": str(self._work_dir),
            "cache_dir": str(self.cache_dir),
            "bwave": str(self.bwave_path),
            "work_bwave": str(self.work_bwave_path),
            "fifo": str(self.fifo_path),
            "vcd": str(self._work_dir / "trace.vcd"),
            "incident": str(self.incident_path),
            "stderr": str(self.persisted_stderr_path),
        }

    def _write_status(self) -> None:
        self._work_dir.mkdir(parents=True, exist_ok=True)
        self._status["updated_at"] = _utc_now()
        self._status["paths"] = self._manifest_paths()
        tmp = self.manifest_path.with_name(
            f".trace_status.{os.getpid()}.{time.monotonic_ns()}.tmp",
        )
        tmp.write_text(
            json.dumps(self._status, indent=2, sort_keys=False) + "\n",
            encoding="utf-8",
        )
        tmp.replace(self.manifest_path)

    def record_attempt(
        self,
        kind: str,
        status: str,
        *,
        detail: str = "",
        return_code: int | None = None,
    ) -> None:
        """Append a trace attempt record to the canonical manifest."""
        attempt: dict[str, object] = {
            "kind": kind,
            "status": status,
            "timestamp": _utc_now(),
        }
        if detail:
            attempt["detail"] = detail
        if return_code is not None:
            attempt["return_code"] = return_code
            return_codes = self._status.setdefault("return_codes", {})
            assert isinstance(return_codes, dict)
            return_codes[kind] = return_code
        attempts = self._status.setdefault("attempts", [])
        assert isinstance(attempts, list)
        attempts.append(attempt)
        self._write_status()

    def record_event(self, kind: str, detail: str = "") -> None:
        """Append a trace lifecycle event to the canonical manifest."""
        event: dict[str, object] = {
            "kind": kind,
            "timestamp": _utc_now(),
        }
        if detail:
            event["detail"] = detail
        events = self._status.setdefault("events", [])
        assert isinstance(events, list)
        events.append(event)
        self._write_status()

    def _path_state(self, path: Path) -> str:
        try:
            st = path.lstat()
        except OSError as exc:
            return f"{path}: missing ({exc})"
        mode = st.st_mode
        if stat.S_ISFIFO(mode):
            kind = "fifo"
        elif stat.S_ISDIR(mode):
            kind = "dir"
        elif stat.S_ISREG(mode):
            kind = "file"
        else:
            kind = "other"
        return f"{path}: {kind}, size={st.st_size}, mtime={st.st_mtime:.0f}"

    def _process_snapshot(
        self,
        sim_proc: subprocess.Popen | None,
        conversion: StreamingConversion | subprocess.Popen | None,
    ) -> str:
        pids = [p.pid for p in (sim_proc, conversion) if p is not None]
        if not pids or os.name != "posix":
            return "process snapshot unavailable"
        try:
            result = subprocess.run(
                ["ps", "-eo", "pid,ppid,stat,etime,cmd"],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return f"process snapshot failed: {exc}"

        rows = result.stdout.splitlines()
        if not rows:
            return "process snapshot empty"
        selected: list[str] = [rows[0]]
        wanted = {str(pid) for pid in pids}
        changed = True
        while changed:
            changed = False
            for row in rows[1:]:
                parts = row.split(None, 4)
                if len(parts) < 2:
                    continue
                pid, ppid = parts[0], parts[1]
                if pid in wanted or ppid in wanted:
                    if pid not in wanted:
                        wanted.add(pid)
                        changed = True
                    if row not in selected:
                        selected.append(row)
        return "\n".join(selected[:80])

    def write_incident(
        self,
        reason: str,
        *,
        sim_proc: subprocess.Popen | None = None,
        bwave_proc: StreamingConversion | subprocess.Popen | None = None,
    ) -> Path:
        """Persist trace failure context next to the sim artifacts."""
        self._work_dir.mkdir(parents=True, exist_ok=True)
        tmp_stderr = self.bwave_path.with_name(self.bwave_path.name + ".stderr")
        stderr_text = ""
        for stderr_path in (self.persisted_stderr_path, tmp_stderr):
            try:
                if stderr_path.exists():
                    stderr_text = stderr_path.read_text(errors="replace").strip()
                    break
            except OSError:
                pass

        lines = [
            f"reason: {reason}",
            f"work_dir: {self._work_dir}",
            f"cache_dir: {self.cache_dir}",
            f"trace_scope: {self._trace_scope or ''}",
            self._path_state(self.bwave_path),
            self._path_state(self.work_bwave_path),
            self._path_state(self.fifo_path),
            self._path_state(self._work_dir / "trace.vcd"),
            self._path_state(self.persisted_stderr_path),
            self._path_state(tmp_stderr),
            "",
            "processes:",
            self._process_snapshot(sim_proc, bwave_proc),
        ]
        if stderr_text:
            lines.extend(["", "bwave stderr:", stderr_text[-4000:]])
        self.incident_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self._status["current_status"] = "failed"
        self._status["failure_reason"] = reason
        return_codes = self._status.setdefault("return_codes", {})
        assert isinstance(return_codes, dict)
        if sim_proc is not None:
            return_codes["sim"] = sim_proc.poll()
        if bwave_proc is not None:
            return_codes["bwave"] = bwave_proc.poll()
        self.record_event("incident", reason)
        print(f"[trace] incident written: {self.incident_path}")
        return self.incident_path

    def find(self) -> Path | None:
        """Adapt B-Wave discovery into Simulation-owned attempt evidence."""
        result = discover_waveform(self._work_dir, cache_dir=self.cache_dir)
        if result.failure_kind == "ambiguous":
            raise SystemExit(result.detail)
        selected = result.selected
        if result.conversion is not None:
            self._record_conversion("vcd_convert", result.conversion)
            if result.conversion.success:
                selected = self._publish_bwave(result.selected) if result.selected else None
        return selected or result.selected

    def inspect(self, path: Path | None = None) -> TraceInspection:
        """Prove that a retained store has a hierarchy and at least one signal.

        The structural FST scan used by :meth:`find` is deliberately only a
        prefilter. This probe crosses the real consumer seam with the cheap
        ``bwave list`` hierarchy query, so malformed and zero-signal stores
        cannot earn ``TRACE_OK`` merely because they contain a value block.
        """
        candidate = path or self.find()
        if candidate is None:
            return TraceInspection(None, "no retained trace artifact was found")
        if candidate.suffix.lower() != ".fst":
            return TraceInspection(
                None,
                f"retained trace is raw {candidate.suffix or 'data'}, not a queryable FST store",
            )
        store_inspection = inspect_store(candidate, expected_scope=self._trace_scope)
        if not store_inspection.queryable:
            return TraceInspection(None, store_inspection.detail)
        metadata = store_inspection.metadata
        assert metadata is not None
        artifact = TraceArtifact(
            path=candidate,
            size_bytes=candidate.stat().st_size,
            top_scope=metadata.display_scope,
            signal_count=metadata.signal_count,
            total_ticks=metadata.total_ticks,
        )
        self._status["current_status"] = "usable"
        self._status["trace_metadata"] = {
            "path": str(artifact.path),
            "size_bytes": artifact.size_bytes,
            "top_scope": artifact.top_scope,
            "signal_count": artifact.signal_count,
            "total_ticks": artifact.total_ticks,
        }
        self.record_event(
            "validated",
            f"{artifact.signal_count} signals under {artifact.top_scope}",
        )
        return TraceInspection(artifact)

    def reset_for_run(self, raw_trace_paths: tuple[Path, ...] = ()) -> None:
        """Remove generated stores that could masquerade as this run's trace.

        A new simulation attempt must prove that it produced a waveform.  If
        the simulator or converter exits before replacing an earlier valid
        store, :meth:`find` cannot distinguish that survivor by integrity or
        mtime alone.  Clear Booley-owned outputs before the simulator starts;
        callers include simulator-owned raw VCD paths that live outside the
        session work directory.
        """
        paths = {
            self.bwave_path,
            self.work_bwave_path,
            self.fifo_path,
            self.incident_path,
            self.persisted_stderr_path,
            self.manifest_path,
            self._work_dir / "trace.vcd",
            *raw_trace_paths,
        }
        for path in paths:
            path.unlink(missing_ok=True)

    def invalidate(self) -> None:
        """Delete cached stores from tmpdir so next find() picks up fresh data."""
        import logging

        logger = logging.getLogger(__name__)
        cache = self.cache_dir
        if cache.is_dir():
            for f in cache.glob("*.fst"):
                try:
                    f.unlink()
                    logger.info("Invalidated stale cached trace: %s", f)
                except OSError:
                    pass

    def start_fifo(self) -> StreamingConversion | None:
        """Start the B-Wave-owned FIFO converter when available."""
        conversion = start_streaming_conversion(
            self.fifo_path,
            self.bwave_path,
            scope=self._trace_scope,
            stderr_path=self.persisted_stderr_path,
        )
        self.record_attempt(
            "fifo_stream",
            "started" if conversion else "disabled",
            detail=(
                "bwave FIFO streamer active"
                if conversion
                else "FIFO unavailable; use VCD fallback"
            ),
        )
        if conversion:
            print(f"[bwave] streaming VCD through FIFO → {self.bwave_path}")
        return conversion

    def cleanup_fifo(self, conversion: StreamingConversion | None) -> None:
        """Finish converter ownership and record Simulation evidence."""
        result = conversion.finish() if conversion is not None else None
        if result:
            for event in result.events:
                print(f"[bwave] {event}")
            if result.detail:
                print(f"[bwave] stderr: {result.detail}")
        status = "success" if result and result.success else "no_artifact"
        if status == "success":
            self._publish_bwave(self.bwave_path)
        return_code = result.attempts[-1].return_code if result and result.attempts else None
        self.record_attempt("fifo_cleanup", status, return_code=return_code)

    def start_monitor(
        self,
        bwave_proc: StreamingConversion,
        sim_proc: subprocess.Popen,
        stall_timeout: float = 30.0,
        poll_interval: float = 2.0,
        kill_after_stalls: int = 3,
    ) -> None:
        """Start a daemon thread that watches trace-store growth and kills on stall.

        Logs a warning each `stall_timeout` window where the store file size
        does not change.  After `kill_after_stalls` consecutive stall windows
        (default 3 x 30s = 90s of zero growth), forcibly terminates both
        `sim_proc` (tree-kill, since sim may have spawned grandchildren) and
        `bwave_proc`, and records the event on the TraceSession so callers
        can surface a clear "trace pipeline stalled" error instead of riding
        the outer job timeout out.
        """
        import threading

        t = threading.Thread(
            target=self._monitor_stalls,
            args=(bwave_proc, sim_proc, stall_timeout, poll_interval, kill_after_stalls),
            daemon=True,
        )
        t.start()

    def _monitor_stalls(
        self,
        bwave_proc: StreamingConversion,
        sim_proc: subprocess.Popen,
        stall_timeout: float,
        poll_interval: float,
        kill_after_stalls: int,
    ) -> None:
        import time

        last_size = -1
        stall_start: float | None = None
        stall_count = 0
        while sim_proc.poll() is None and bwave_proc.poll() is None:
            time.sleep(poll_interval)
            size = bwave_proc.progress_size()
            if size != last_size:
                last_size, stall_start, stall_count = size, None, 0
                continue
            if stall_start is None:
                stall_start = time.monotonic()
                continue
            if time.monotonic() - stall_start < stall_timeout:
                continue
            stall_count += 1
            stalled_for = stall_timeout * stall_count
            self._log_stall_window(
                self.bwave_path,
                size,
                stalled_for,
                stall_count,
                kill_after_stalls,
                sim_proc,
                bwave_proc,
            )
            if stall_count >= kill_after_stalls:
                self._kill_stalled_pipeline(size, stalled_for, sim_proc, bwave_proc)
                return
            stall_start = None

    def _log_stall_window(
        self,
        bpath,
        sz,
        stalled_for,
        stall_count,
        kill_after_stalls,
        sim_proc,
        bwave_proc,
    ) -> None:
        """Warn about one stall window and echo any bwave stderr tail."""
        import logging

        log = logging.getLogger(__name__)
        log.warning(
            "[bwave monitor] trace store stalled at %d bytes for %.0fs "
            "(window %d/%d). sim_proc pid=%d alive=%s, "
            "bwave_proc pid=%d alive=%s",
            sz,
            stalled_for,
            stall_count,
            kill_after_stalls,
            sim_proc.pid,
            sim_proc.poll() is None,
            bwave_proc.pid,
            bwave_proc.poll() is None,
        )
        txt = bwave_proc.stderr_tail(500)
        if txt:
            log.warning("[bwave monitor] stderr: %s", txt)

    def _kill_stalled_pipeline(
        self,
        sz,
        stalled_for,
        sim_proc,
        bwave_proc,
    ) -> None:
        """Record the stall incident and tree-kill sim + bwave processes."""
        import logging

        from booley.runtime.platform_paths import kill_process_tree

        log = logging.getLogger(__name__)
        self._stall_killed = True
        self._stall_message = (
            f"bwave trace pipeline stalled (no growth for {stalled_for:.0f}s at {sz} bytes)"
        )
        self.write_incident(
            self._stall_message,
            sim_proc=sim_proc,
            bwave_proc=bwave_proc,
        )
        kill_msg = f"[bwave monitor] killing sim+bwave: {self._stall_message}"
        log.error(kill_msg)
        # Print to stdout so it lands in the simulator's captured
        # output and gets surfaced by upstream parsers.
        print(kill_msg, flush=True)
        kill_process_tree(sim_proc)
        bwave_proc.kill()

    def postprocess(self, vcd_path: Path) -> None:
        """Windows/fallback: convert VCD file → .fst after sim completes."""
        detail = f"{vcd_path} (exists={vcd_path.exists()}; scope={self._trace_scope or '<all>'})"
        self.record_attempt("vcd_postprocess", "started", detail=detail)
        success = False
        try:
            result = convert_vcd(
                vcd_path,
                self.bwave_path,
                scope=self._trace_scope,
                allow_unscoped_fallback=True,
                existing_store=ExistingStorePolicy.REPLACE,
            )
            self._render_conversion(result)
            self._record_conversion("vcd_postprocess_build", result)
            success = result.success
            if success:
                self._publish_bwave(self.bwave_path)
            elif result.detail:
                (vcd_path.parent / "trace.fst.stderr").write_text(
                    result.detail + "\n", encoding="utf-8"
                )
        finally:
            self._settle_postprocess_artifacts(vcd_path, success=success)
        status = "success" if success else "vcd_only"
        self.record_attempt("vcd_postprocess", status, detail=detail)

    def _settle_postprocess_artifacts(self, vcd_path: Path, *, success: bool) -> None:
        """Keep raw fallback artifacts inside the managed simulation directory."""
        self._work_dir.mkdir(parents=True, exist_ok=True)
        source_stderr = vcd_path.parent / "trace.fst.stderr"

        if success:
            vcd_path.unlink(missing_ok=True)
            if source_stderr != self.persisted_stderr_path:
                source_stderr.unlink(missing_ok=True)
            return

        managed_vcd = self._work_dir / "trace.vcd"
        if vcd_path.exists() and vcd_path != managed_vcd:
            managed_vcd.unlink(missing_ok=True)
            shutil.move(vcd_path, managed_vcd)
        if source_stderr.exists() and source_stderr != self.persisted_stderr_path:
            self.persisted_stderr_path.unlink(missing_ok=True)
            shutil.move(source_stderr, self.persisted_stderr_path)

    def _publish_bwave(self, trace_path: Path) -> Path | None:
        """Copy a valid cached store beside sim artifacts for later lookup."""
        if (
            trace_path.suffix != ".fst"
            or not inspect_store(trace_path, probe_reader=False).structurally_usable
        ):
            return None
        dest = self.work_bwave_path
        try:
            if trace_path.resolve() == dest.resolve():
                return dest
        except OSError:
            pass
        try:
            self._work_dir.mkdir(parents=True, exist_ok=True)
            if dest.exists() and dest.stat().st_mtime >= trace_path.stat().st_mtime:
                return dest
            shutil.copy2(trace_path, dest)
            self.record_event("bwave_published", str(dest))
            return dest
        except OSError as exc:
            self.record_event("bwave_publish_failed", str(exc))
            return None

    def _record_conversion(self, kind: str, result: ConversionResult) -> None:
        """Persist B-Wave conversion attempts in the Simulation manifest."""
        if not result.attempts:
            status = "disabled" if result.failure_kind == "missing_binary" else "failed"
            self.record_attempt(kind, status, detail=result.detail)
            return
        for attempt in result.attempts:
            detail = attempt.stderr or f"scope={attempt.scope or '<all>'}"
            self.record_attempt(
                kind,
                "success" if result.success and attempt is result.attempts[-1] else "failed",
                detail=detail,
                return_code=attempt.return_code,
            )

    @staticmethod
    def _render_conversion(result: ConversionResult) -> None:
        for event in result.events:
            print(f"[bwave] {event}")
        for message in conversion_failure_messages(result, warning="post-process failed"):
            print(message)

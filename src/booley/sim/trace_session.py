"""Single owner for simulation trace lifecycle.

Consolidates trace path derivation, cache management, FIFO streaming,
and invalidation into one object — eliminating the multi-module path
mismatch bugs that plagued the previous scattered implementation.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from booley.runtime.timefmt import utc_now_rfc3339


def _bwave_cache_root() -> Path:
    """Platform-safe temp directory for bwave cache files."""
    import tempfile

    return Path(tempfile.gettempdir()) / "bwave"


# FST files are a sequence of blocks, each [1-byte type][big-endian u64
# section length, counted from the length field itself][payload]. The first is
# always the header block: type 0, fixed length 329.
_FST_HEADER_BLOCK = 0
_FST_HEADER_LENGTH = 329
_BWAVE_MIN_SIZE = 16
# Block types that actually carry value-change data. A trace with a valid
# header and none of these is a syntactically perfect file describing nothing
# — which is exactly what a simulator produces when it was asked to trace via
# a CLI convention its main() does not implement.
_FST_VCDATA_BLOCKS = frozenset({1, 5, 8})
# Guard against walking a corrupt/adversarial block chain forever.
_FST_MAX_BLOCK_SCAN = 4096
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


def _looks_like_vcd(path: Path) -> bool:
    """Return True when *path* is a regular file containing VCD text."""
    try:
        st = path.stat()
        if stat.S_ISFIFO(st.st_mode) or st.st_size == 0:
            return False
        with path.open("rb") as f:
            head = f.read(4096)
    except OSError:
        return False
    return b"$date" in head and (b"$scope" in head or b"$timescale" in head)


def _materialize_fifo_vcd(fifo_path: Path, vcd_path: Path) -> Path | None:
    """Copy a regular-file trace.fifo fallback to trace.vcd for discovery.

    On POSIX, the iverilog trace injector targets ``trace.fifo``.  If the
    bwave FIFO builder is unavailable, the simulator opens that path as a
    normal file and writes VCD text there.  The rest of Booley's trace tooling
    discovers ``trace.vcd``, so normalize the fallback file in place.
    """
    if not _looks_like_vcd(fifo_path):
        return None
    try:
        if vcd_path.exists() and vcd_path.stat().st_mtime >= fifo_path.stat().st_mtime:
            return vcd_path
        shutil.copy2(fifo_path, vcd_path)
    except OSError:
        return fifo_path
    return vcd_path


def _bwave_valid(path: Path) -> bool:
    """Integrity check for a waveform store: FST header *and* signal data.

    Checking only the header is a false-pass generator. A simulator whose
    ``main()`` uses a trace CLI Booley did not speak still opens the dump file
    and writes the header, then records nothing; the run passes, and a
    443-byte "waveform" is accepted as proof of a traced simulation. Requiring
    at least one value-change block makes the check assert the thing the
    caller actually cares about.
    """
    try:
        size = path.stat().st_size
        if size < _BWAVE_MIN_SIZE:
            return False
        with path.open("rb") as f:
            head = f.read(9)
            if (
                len(head) != 9
                or head[0] != _FST_HEADER_BLOCK
                or int.from_bytes(head[1:9], "big") != _FST_HEADER_LENGTH
            ):
                return False
            return _has_value_change_block(f, size)
    except OSError:
        return False


def _has_value_change_block(f: IO[bytes], size: int) -> bool:
    """Walk the FST block chain from the header, looking for signal data.

    Each block is a 1-byte type followed by a big-endian u64 section length
    that counts itself, so the next block starts at ``offset + 1 + length``.
    The scan is bounded: a corrupt or hostile chain must terminate rather than
    spin on a self-referencing offset.
    """
    offset = 1 + _FST_HEADER_LENGTH
    for _ in range(_FST_MAX_BLOCK_SCAN):
        if offset >= size:
            return False
        f.seek(offset)
        block = f.read(9)
        if len(block) != 9:
            return False
        if block[0] in _FST_VCDATA_BLOCKS:
            return True
        length = int.from_bytes(block[1:9], "big")
        if length <= 0:
            return False
        offset += 1 + length
    return False


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
    """Owns the full lifecycle of a simulation trace file.

    Callers provide `work_dir` (the sim output directory) as pre-computed
    input; TraceSession handles everything from there: cache location,
    FIFO management, invalidation, and discovery.

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
        """Per-work-dir cache bucket under the bwave temp root.

        The fallback key used to be the bare ``work_dir.name``, which is not
        unique: every ticket's sim output directory is called ``sim``, so all
        of them shared ``/tmp/bwave/sim`` inside one container. ``find()``
        checks that cache first, so registering a trace could bind a
        *different design's* waveform — observed in the field, where a query
        about an AXI bridge returned signals from an unrelated serial-comm
        testbench with nothing flagging the mismatch. Appending a digest of
        the absolute work_dir path keeps the readable prefix and makes the
        bucket collision-free.
        """
        if self._cache_key:
            name = self._cache_key
        else:
            resolved = str(self._work_dir.resolve())
            digest = hashlib.sha256(resolved.encode("utf-8", "replace")).hexdigest()[:12]
            name = f"{self._work_dir.name}-{digest}"
        d = _bwave_cache_root() / name
        d.mkdir(parents=True, exist_ok=True)
        return d

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
        bwave_proc: subprocess.Popen | None,
    ) -> str:
        pids = [p.pid for p in (sim_proc, bwave_proc) if p is not None]
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
        bwave_proc: subprocess.Popen | None = None,
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
        """Find existing trace: tmpdir cache first, then work_dir, then VCD fallback.

        Auto-converts VCD→.fst if only VCD exists. Exits on ambiguity.
        Respects cache_key when set (content-addressed lookup).
        """

        def _stores_in(directory: Path) -> list[Path]:
            found = [f for f in directory.glob("*.fst") if _bwave_valid(f)]
            if len(found) > 1:
                sys.exit(
                    f"ERROR: Multiple *.fst files in {directory}:\n"
                    + "\n".join(f"  {f.name}" for f in found)
                )
            return found

        # Tmpdir fast cache (uses cache_key if set) vs. the store published
        # beside the sim artifacts. Prefer whichever is newer: a cached store
        # left by an earlier sim of the same work dir is stale the moment a
        # re-run publishes a fresh trace, and silently answering from the old
        # one is the same class of bug as the cross-ticket collision.
        cached = _stores_in(self.cache_dir) if self.cache_dir.is_dir() else []
        published = _stores_in(self._work_dir)
        if cached and published:
            # Publishing uses copy2(), so the cache and project copy normally
            # have identical mtimes. Prefer the host-visible project artifact
            # on that tie; list/max's former cache-first tie break leaked a
            # container-private /tmp path into reports (Taxi F-42).
            cached_mtime = cached[0].stat().st_mtime
            published_mtime = published[0].stat().st_mtime
            return published[0] if published_mtime >= cached_mtime else cached[0]
        if cached:
            return cached[0]
        if published:
            return published[0]

        # VCD fallback with auto-conversion
        vcd = self._work_dir / "trace.vcd"
        if not vcd.exists():
            vcd = _materialize_fifo_vcd(self.fifo_path, vcd) or vcd
        if not vcd.exists():
            return None
        converted = self._convert_vcd(vcd)
        return converted if converted else vcd

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

        data, failure_reason = self._read_bwave_metadata(candidate)
        if data is None:
            return TraceInspection(None, failure_reason)
        top_scope = str(data["scope_prefix"])
        if not top_scope:
            top_scope = ", ".join(str(scope) for scope in data["root_scopes"])
        signal_count = int(data["signal_count"])
        total_ticks = int(data["total_ticks"])
        if signal_count <= 0:
            return TraceInspection(None, "retained trace has no signals")
        if not top_scope:
            top_scope = "<top-level signals>"

        artifact = TraceArtifact(
            path=candidate,
            size_bytes=candidate.stat().st_size,
            top_scope=top_scope,
            signal_count=signal_count,
            total_ticks=total_ticks,
        )
        self._status["current_status"] = "usable"
        self._status["trace_metadata"] = {
            "path": str(artifact.path),
            "size_bytes": artifact.size_bytes,
            "top_scope": artifact.top_scope,
            "signal_count": artifact.signal_count,
            "total_ticks": artifact.total_ticks,
        }
        self.record_event("validated", f"{signal_count} signals under {top_scope}")
        return TraceInspection(artifact)

    @staticmethod
    def _read_bwave_metadata(candidate: Path) -> tuple[dict[str, object] | None, str]:
        """Run the bounded hierarchy probe and decode its JSON data object."""
        from booley.sim.bwave_fifo import _find_bwave_bin

        bwave_bin = _find_bwave_bin()
        if not bwave_bin:
            return None, "native B-Wave binary is unavailable for trace validation"
        try:
            result = subprocess.run(
                [bwave_bin, "list", str(candidate), "--format", "json", "--limit", "1"],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return None, f"B-Wave hierarchy probe failed: {exc}"
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip().splitlines()
            suffix = f": {detail[-1]}" if detail else ""
            return (
                None,
                f"B-Wave could not read the retained trace (rc={result.returncode}){suffix}",
            )
        try:
            payload = json.loads(result.stdout)
            raw_data = payload["data"]
            if not isinstance(raw_data, dict):
                raise TypeError("data is not an object")
            root_scopes = [
                str(scope).strip()
                for scope in raw_data.get("root_scopes", [])
                if str(scope).strip()
            ]
            if not root_scopes:
                root_scopes = sorted(
                    {
                        str(signal.get("name", "")).split(".", 1)[0]
                        for signal in raw_data.get("signals", [])
                        if isinstance(signal, dict)
                        and "." in str(signal.get("name", ""))
                    }
                )
            data = {
                "scope_prefix": str(raw_data.get("scope_prefix") or "").strip(),
                "root_scopes": root_scopes,
                "signal_count": int(
                    raw_data.get("signal_count", len(raw_data.get("signals", []))) or 0
                ),
                "total_ticks": int(raw_data.get("total_ticks", 0) or 0),
            }
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            return None, f"B-Wave returned malformed trace metadata: {exc}"
        return data, ""

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

    def start_fifo(self) -> tuple[subprocess.Popen | None, bool, int | None]:
        """Start FIFO streaming pipeline (POSIX only).

        Returns (bwave_proc, use_fifo, keepalive_fd). Caller MUST close
        keepalive_fd after simulator exits, then call cleanup_fifo().
        """
        from booley.sim.bwave_fifo import setup_bwave_paths

        # Hand the streamer *our* store path: it must write exactly the file
        # cleanup_fifo/find()/write_incident later look for (see F-24).
        _fifo_path, _bwave_path, bwave_proc, use_fifo, keepalive_fd = setup_bwave_paths(
            self._work_dir, True, self._trace_scope, self.bwave_path
        )
        self.record_attempt(
            "fifo_stream",
            "started" if use_fifo else "disabled",
            detail=(
                "bwave FIFO streamer active" if use_fifo else "FIFO unavailable; use VCD fallback"
            ),
        )
        return bwave_proc, use_fifo, keepalive_fd

    def cleanup_fifo(
        self,
        bwave_proc: subprocess.Popen | None,
        keepalive_fd: int | None,
    ) -> None:
        """Close keepalive fd and wait for bwave with kill escalation."""
        if keepalive_fd is not None:
            os.close(keepalive_fd)
        tmp_stderr = self.bwave_path.with_name(self.bwave_path.name + ".stderr")
        if tmp_stderr.exists():
            with contextlib.suppress(OSError):
                shutil.copy2(tmp_stderr, self.persisted_stderr_path)
        from booley.sim.bwave_fifo import cleanup_bwave

        cleanup_bwave(bwave_proc, self.bwave_path, self.fifo_path)
        rc = bwave_proc.poll() if bwave_proc is not None else None
        status = "success" if _bwave_valid(self.bwave_path) else "no_artifact"
        if status == "success":
            self._publish_bwave(self.bwave_path)
        self.record_attempt("fifo_cleanup", status, return_code=rc)

    def start_monitor(
        self,
        bwave_proc: subprocess.Popen,
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

        def _monitor():
            import time

            bpath = self.bwave_path
            # bwave appends to <bwave>.progress every ~5s during VCD
            # parsing; the .fst itself is only written at EOF.  Watching both
            # avoids false-positive stalls during long randomized TBs.
            progress_path = bpath.with_name(bpath.name + ".progress")
            last_size = -1
            stall_start: float | None = None
            stall_count = 0

            def _liveness_size() -> int:
                total = 0
                for p in (bpath, progress_path):
                    try:
                        if p.exists():
                            total += p.stat().st_size
                    except OSError:
                        pass
                return total

            while sim_proc.poll() is None and bwave_proc.poll() is None:
                time.sleep(poll_interval)
                sz = _liveness_size()

                if sz != last_size:
                    last_size = sz
                    stall_start = None
                    stall_count = 0
                    continue

                if stall_start is None:
                    stall_start = time.monotonic()
                    continue
                if time.monotonic() - stall_start < stall_timeout:
                    continue

                stall_count += 1
                stalled_for = stall_timeout * stall_count
                self._log_stall_window(
                    bpath,
                    sz,
                    stalled_for,
                    stall_count,
                    kill_after_stalls,
                    sim_proc,
                    bwave_proc,
                )

                if stall_count >= kill_after_stalls:
                    self._kill_stalled_pipeline(
                        sz,
                        stalled_for,
                        sim_proc,
                        bwave_proc,
                    )
                    return

                stall_start = None  # re-arm for the next window

        t = threading.Thread(target=_monitor, daemon=True)
        t.start()

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
        stderr_file = bpath.with_name(bpath.name + ".stderr")
        try:
            txt = stderr_file.read_text(errors="replace").strip()
            if txt:
                log.warning("[bwave monitor] stderr: %s", txt[:500])
        except OSError:
            pass

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
        with contextlib.suppress(OSError):
            bwave_proc.kill()

    def postprocess(self, vcd_path: Path) -> None:
        """Windows/fallback: convert VCD file → .fst after sim completes."""
        from booley.sim.bwave_fifo import postprocess_vcd_to_bwave

        detail = f"{vcd_path} (exists={vcd_path.exists()}; scope={self._trace_scope or '<all>'})"
        self.record_attempt("vcd_postprocess", "started", detail=detail)
        postprocess_vcd_to_bwave(vcd_path, self.bwave_path, self._trace_scope)
        status = "success" if _bwave_valid(self.bwave_path) else "vcd_only"
        if status == "success":
            self._publish_bwave(self.bwave_path)
        self.record_attempt("vcd_postprocess", status, detail=detail)

    def _convert_vcd(self, vcd_path: Path) -> Path | None:
        """Convert VCD → .fst, caching in tmpdir."""
        bwave_path = self.bwave_path
        if bwave_path.exists() and bwave_path.stat().st_mtime >= vcd_path.stat().st_mtime:
            return bwave_path

        from booley.sim.bwave_fifo import _find_bwave_bin

        bwave_bin = _find_bwave_bin()
        if not bwave_bin:
            self.record_attempt(
                "vcd_convert",
                "disabled",
                detail="bwave binary not found",
            )
            print("[bwave] bwave not found -- cannot convert VCD", file=sys.stderr)
            return None

        vcd_mb = vcd_path.stat().st_size / (1024 * 1024)
        print(f"[bwave] converting VCD -> .fst ({vcd_mb:.0f} MB VCD) ...")
        self.record_attempt("vcd_convert", "started", detail=str(vcd_path))
        with vcd_path.open("rb") as f:
            # v0.2 subcommand form: `bwave build -o PATH` reading VCD from stdin
            result = subprocess.run(
                [bwave_bin, "build", "-o", str(bwave_path)],
                stdin=f,
                capture_output=True,
                check=False,
            )
        if result.returncode == 0 and bwave_path.exists() and _bwave_valid(bwave_path):
            sz_mb = bwave_path.stat().st_size / (1024 * 1024)
            print(f"[bwave] wrote {bwave_path.name} ({sz_mb:.1f} MB)")
            self.record_attempt(
                "vcd_convert",
                "success",
                detail=str(vcd_path),
                return_code=result.returncode,
            )
            self._publish_bwave(bwave_path)
            return bwave_path

        stderr_out = result.stderr.decode(errors="replace").strip()
        if stderr_out:
            print(stderr_out, file=sys.stderr)
        print(f"[bwave] WARNING: VCD conversion failed (rc={result.returncode})", file=sys.stderr)
        self.record_attempt(
            "vcd_convert",
            "failed",
            detail=stderr_out[-1000:] if stderr_out else str(vcd_path),
            return_code=result.returncode,
        )
        return None

    def _publish_bwave(self, trace_path: Path) -> Path | None:
        """Copy a valid cached store beside sim artifacts for later lookup."""
        if trace_path.suffix != ".fst" or not _bwave_valid(trace_path):
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

"""Shared bwave FIFO streaming development.

Provides FIFO-based VCD→.fst streaming for both iverilog and Verilator
sim runners, plus a fallback post-process path for Windows.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

_BWAVE_EXIT_TIMEOUT_SECONDS = 15
_BWAVE_KILL_TIMEOUT_SECONDS = 5


def _find_bwave_bin() -> str | None:
    """Locate the native bwave binary without auto-building.

    FIFO streaming needs the *native* binary: the Python wrapper (the only
    ``bwave`` on PATH) exits before reading the FIFO, which would leave the
    simulator blocked on trace output.  booley.paths.native_bwave_binary()
    resolves the binary off PATH and rejects a wrapper hit for us.
    """
    from booley.paths import native_bwave_binary

    found = native_bwave_binary()
    return str(found) if found else None


def can_stream_bwave_fifo() -> bool:
    """Return True when POSIX FIFO streaming can be used for this run."""
    return os.name == "posix" and _find_bwave_bin() is not None


def setup_bwave_paths(
    work_dir: Path,
    vcd: bool,
    trace_scope: str | None,
    bwave_path: Path | None = None,
) -> tuple[Path, Path, subprocess.Popen | None, bool, int | None]:
    """Prepare bwave FIFO/file paths and optionally start the FIFO streamer.

    Returns (fifo_path, bwave_path, bwave_proc, use_fifo, keepalive_fd).

    ``bwave_path`` is the destination store; callers that own a TraceSession
    pass ``session.bwave_path`` so writer and reader address the *same* file.
    This used to be re-derived here as ``_bwave_cache_root() / work_dir.name``
    — the pre-digest bucket — while TraceSession.cache_dir appends a digest of
    the resolved work_dir. The streamer then wrote ``/tmp/bwave/sim/trace.fst``
    and every reader (cleanup validation, incident report, find()) probed
    ``/tmp/bwave/sim-<digest>/trace.fst: missing``, so a perfectly healthy FIFO
    run looked artifact-less. TraceSession stays the single source of truth;
    the fallback below exists only for callers without a session in hand.
    """
    fifo_path = work_dir / "trace.fifo"
    if bwave_path is None:
        from booley.sim.trace_session import TraceSession

        bwave_path = TraceSession(work_dir, trace_scope).bwave_path
    bwave_path.parent.mkdir(parents=True, exist_ok=True)
    bwave_proc = None
    keepalive_fd: int | None = None
    use_fifo = vcd and can_stream_bwave_fifo()
    if use_fifo:
        bwave_proc, use_fifo, keepalive_fd = start_bwave_fifo(work_dir, bwave_path, trace_scope)
    return fifo_path, bwave_path, bwave_proc, use_fifo, keepalive_fd


def start_bwave_fifo(
    work_dir: Path,
    bwave_path: Path,
    trace_scope: str | None,
) -> tuple[subprocess.Popen | None, bool, int | None]:
    """Start bwave FIFO streaming on POSIX.

    Opens the FIFO with O_RDWR (keepalive) + O_RDONLY (bwave stdin).
    bwave receives VCD data on stdin (no --input flag — that path is
    for blocking-open FIFOs in the offline post-process case).

    Returns (bwave_proc, use_fifo, keepalive_fd).  The caller MUST close
    keepalive_fd after the simulator exits so bwave sees EOF.
    """
    import fcntl

    fifo_path = work_dir / "trace.fifo"
    bwave_bin = _find_bwave_bin()
    if not bwave_bin:
        print("[bwave] bwave not found, falling back to VCD file")
        return None, False, None

    if fifo_path.exists():
        fifo_path.unlink()
    os.mkfifo(str(fifo_path))

    # O_RDWR keeps both reader and writer references alive so the
    # simulator's open(O_WRONLY) on the FIFO succeeds without blocking.
    # bwave gets a *separate* O_RDONLY fd so it sees EOF once every
    # writer closes — otherwise the O_RDWR fd counts as a writer and
    # bwave hangs forever after the simulator exits.
    rw_fd = os.open(str(fifo_path), os.O_RDWR)
    rd_fd = os.open(str(fifo_path), os.O_RDONLY | os.O_NONBLOCK)
    fcntl.fcntl(rd_fd, fcntl.F_SETFL, fcntl.fcntl(rd_fd, fcntl.F_GETFL) & ~os.O_NONBLOCK)

    # v0.2 subcommand form: `bwave build -o PATH [--scope SCOPE]` (stdin input)
    bwave_cmd = [bwave_bin, "build", "-o", str(bwave_path)]
    if trace_scope:
        bwave_cmd.extend(["--scope", trace_scope])

    # stdout → DEVNULL (high-volume progress output can deadlock the
    # 64 KB pipe buffer); stderr → file so crashes aren't silent.
    stderr_path = work_dir / "trace.fst.stderr"
    stderr_fh = stderr_path.open("w")
    bwave_proc = subprocess.Popen(
        bwave_cmd,
        stdin=rd_fd,
        stdout=subprocess.DEVNULL,
        stderr=stderr_fh,
    )
    stderr_fh.close()
    os.close(rd_fd)

    print(f"[bwave] streaming VCD through FIFO → {bwave_path}")
    return bwave_proc, True, rw_fd


def cleanup_bwave(
    bwave_proc: subprocess.Popen | None,
    bwave_path: Path,
    fifo_path: Path,
) -> None:
    """Wait for bwave builder (with kill escalation) and clean up FIFO."""
    if bwave_proc is not None:
        try:
            bwave_rc = bwave_proc.wait(timeout=_BWAVE_EXIT_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            bwave_proc.kill()
            bwave_rc = bwave_proc.wait(timeout=_BWAVE_KILL_TIMEOUT_SECONDS)
            print("[bwave] WARNING: bwave killed (FIFO hung)")
        stderr_file = fifo_path.parent / "trace.fst.stderr"
        if not stderr_file.exists():
            stderr_file = bwave_path.with_name(bwave_path.name + ".stderr")
        if stderr_file.exists():
            stderr_text = stderr_file.read_text().strip()
            if stderr_text:
                print(f"[bwave] stderr: {stderr_text}")
        if bwave_rc != 0:
            print(f"[bwave] WARNING: bwave exited with rc={bwave_rc}")
        elif bwave_path.exists():
            sz_mb = bwave_path.stat().st_size / (1024 * 1024)
            print(f"[bwave] wrote {bwave_path.name} ({sz_mb:.1f} MB)")
    if fifo_path.exists():
        fifo_path.unlink(missing_ok=True)
    # Liveness heartbeat from bwave: normally removed on clean exit,
    # but a killed/crashed process leaves it behind.
    progress_path = bwave_path.with_name(bwave_path.name + ".progress")
    progress_path.unlink(missing_ok=True)


def _run_bwave_build_attempt(
    bwave_bin: str,
    vcd_path: Path,
    bwave_path: Path,
    label: str,
    scope: str | None,
) -> subprocess.CompletedProcess:
    """Run one `bwave build` attempt, clearing any stale/invalid prior output first."""
    from booley.sim.trace_session import _bwave_valid

    if bwave_path.exists() and not _bwave_valid(bwave_path):
        bwave_path.unlink(missing_ok=True)
    if label == "unscoped-fallback":
        print("[bwave] scoped post-process produced no queryable store; retrying whole trace")

    # v0.2 subcommand form: `bwave build -o PATH [--scope SCOPE]` (stdin input)
    bwave_cmd = [bwave_bin, "build", "-o", str(bwave_path)]
    if scope:
        bwave_cmd.extend(["--scope", scope])
    with vcd_path.open("rb") as f:
        return subprocess.run(
            bwave_cmd,
            stdin=f,
            capture_output=True,
            check=False,
        )


def _write_bwave_failure_diagnostic(
    vcd_path: Path,
    bwave_path: Path,
    trace_scope: str | None,
    stderr_chunks: list[str],
    last_rc: int | None,
) -> None:
    """Persist a diagnostic sidecar and warn when post-processing never succeeded."""
    stderr_path = vcd_path.parent / "trace.fst.stderr"
    diagnostic = [
        f"vcd_path: {vcd_path}",
        f"vcd_exists: {vcd_path.exists()}",
        f"requested_scope: {trace_scope or '<all>'}",
        f"bwave_path: {bwave_path}",
        f"bwave_exists: {bwave_path.exists()}",
    ]
    if stderr_chunks:
        diagnostic.extend(["", *stderr_chunks])
        print("\n".join(stderr_chunks))
    stderr_path.write_text("\n".join(diagnostic) + "\n", encoding="utf-8")
    print(f"[bwave] WARNING: post-process failed (rc={last_rc})")


def postprocess_vcd_to_bwave(
    vcd_path: Path,
    bwave_path: Path,
    trace_scope: str | None,
) -> bool:
    """Windows/fallback: post-process VCD file → .fst store."""
    if not vcd_path.exists():
        return False
    bwave_bin = _find_bwave_bin()
    if not bwave_bin:
        return False
    from booley.sim.trace_session import _bwave_valid

    print("[bwave] post-processing VCD -> .fst ...")
    attempts: list[tuple[str, str | None]] = [("scoped", trace_scope)]
    if trace_scope:
        attempts.append(("unscoped-fallback", None))

    stderr_chunks: list[str] = []
    last_rc: int | None = None
    for label, scope in attempts:
        result = _run_bwave_build_attempt(
            bwave_bin,
            vcd_path,
            bwave_path,
            label,
            scope,
        )
        last_rc = result.returncode
        stderr_out = result.stderr.decode(errors="replace").strip()
        if stderr_out:
            stderr_chunks.append(
                f"[{label}; scope={scope or '<all>'}; rc={result.returncode}]\n{stderr_out}"
            )
        if result.returncode == 0 and _bwave_valid(bwave_path):
            sz_mb = bwave_path.stat().st_size / (1024 * 1024)
            print(f"[bwave] wrote {bwave_path.name} ({sz_mb:.1f} MB)")
            return True

    _write_bwave_failure_diagnostic(
        vcd_path,
        bwave_path,
        trace_scope,
        stderr_chunks,
        last_rc,
    )
    return False

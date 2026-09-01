"""Flow-neutral durable run-log persistence and freshness protocol."""

from __future__ import annotations

import os
import time
from pathlib import Path

from booley.runtime.timefmt import utc_now_rfc3339

RUN_LOG_NAME = "run.log"
RUN_LOG_HEADER_PREFIX = "[BOOLEY RUN_LOG]"
RUN_LOG_IN_PROGRESS_PREFIX = "(run in progress"
RUN_LOG_PENDING = f"{RUN_LOG_IN_PROGRESS_PREFIX} — the full output lands here when it finishes)"
RUN_LOG_PROGRESS_MAX_BYTES = 200_000
_PROCESS_RUN_TOKEN = f"pid{os.getpid()}-{int(time.time())}"


def current_run_token() -> str:
    """Return the Flow invocation identity that owns its run logs."""
    return os.environ.get("BOOLEY_RUN_ID", "") or _PROCESS_RUN_TOKEN


def begin_run_log(
    work_dir: str | Path,
    *,
    flow: str,
    target: str,
    run: str | None = None,
) -> Path:
    """Open a fresh run log with ownership and in-progress markers."""
    path = Path(work_dir, RUN_LOG_NAME)
    header = (
        f"{RUN_LOG_HEADER_PREFIX} run={run or current_run_token()} "
        f"flow={flow} target={target} started={utc_now_rfc3339()}\n"
    )
    path.write_text(f"{header}{RUN_LOG_PENDING}\n", encoding="utf-8")
    return path


def _read_run_log_head(path: Path) -> tuple[bytes, bytes]:
    """Read at most the first two bounded lines of a run log."""
    try:
        with path.open("rb") as stream:
            return stream.readline(4096), stream.readline(4096)
    except OSError:
        return b"", b""


def read_run_log_header(work_dir: str | Path) -> dict[str, str] | None:
    """Parse a run log's ownership header into its ``key=value`` fields."""
    first, _ = _read_run_log_head(Path(work_dir, RUN_LOG_NAME))
    line = first.decode("utf-8", errors="replace")
    if not line.startswith(RUN_LOG_HEADER_PREFIX) or not line.endswith("\n"):
        return None
    fields: dict[str, str] = {}
    for token in line[len(RUN_LOG_HEADER_PREFIX) :].split():
        key, separator, value = token.partition("=")
        if separator and value:
            fields[key] = value
    return fields


def run_log_is_current(work_dir: str | Path, run: str | None = None) -> bool:
    """Return whether the log holds finished output from the requested run."""
    header = read_run_log_header(work_dir)
    if header is None or header.get("run") != (run or current_run_token()):
        return False
    _, second = _read_run_log_head(Path(work_dir, RUN_LOG_NAME))
    body = second.decode("utf-8", errors="replace").rstrip("\n")
    return not body.startswith(RUN_LOG_IN_PROGRESS_PREFIX)


def cap_log_bytes(data: bytes, max_bytes: int) -> bytes:
    """Clamp bytes to a maximum size while retaining the actionable tail."""
    if len(data) <= max_bytes:
        return data
    marker = (
        f"[RUN_LOG TRUNCATED] original size {len(data)} bytes > cap {max_bytes}; keeping tail\n"
    ).encode()
    keep = max(0, max_bytes - len(marker))
    tail = data[-keep:] if keep else b""
    cut = tail.find(b"\n")
    if 0 <= cut < len(tail) - 1:
        tail = tail[cut + 1 :]
    return marker + tail


def _atomic_write(path: Path, data: bytes) -> None:
    """Atomically replace a log so concurrent readers never see torn output."""
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_bytes(data)
        temporary.replace(path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def write_run_log_progress(
    work_dir: str | Path,
    tail_text: str,
    *,
    elapsed_s: float,
    line_count: int,
    idle_s: float | None = None,
    max_bytes: int = RUN_LOG_PROGRESS_MAX_BYTES,
) -> Path:
    """Refresh a still-running Flow's log with bounded progress and live output."""
    path = Path(work_dir, RUN_LOG_NAME)
    first, _ = _read_run_log_head(path)
    is_header = first.startswith(RUN_LOG_HEADER_PREFIX.encode()) and first.endswith(b"\n")
    header = first if is_header else b""
    idle = "" if idle_s is None else f", last output {idle_s:.0f}s ago"
    status = (
        f"{RUN_LOG_IN_PROGRESS_PREFIX} — {elapsed_s:.0f}s elapsed, "
        f"{line_count} output line(s){idle}; live tail below, replaced by the "
        "full output when the run finishes)\n"
    ).encode()
    body = tail_text.encode("utf-8", errors="replace")
    data = header + status + cap_log_bytes(body, max(0, max_bytes - len(status)))
    _atomic_write(path, data)
    return path


def write_run_log(
    work_dir: str | Path,
    text: str,
    max_bytes: int | None = 10_000_000,
) -> Path:
    """Atomically persist Flow output, preserving an existing ownership header."""
    path = Path(work_dir, RUN_LOG_NAME)
    first, _ = _read_run_log_head(path)
    is_header = first.startswith(RUN_LOG_HEADER_PREFIX.encode()) and first.endswith(b"\n")
    header = first if is_header else b""
    body = text.encode("utf-8", errors="replace")
    data = (
        header + body
        if max_bytes is None
        else header + cap_log_bytes(body, max(0, max_bytes - len(header)))
    )
    _atomic_write(path, data)
    return path


__all__ = [
    "RUN_LOG_HEADER_PREFIX",
    "RUN_LOG_IN_PROGRESS_PREFIX",
    "RUN_LOG_NAME",
    "RUN_LOG_PENDING",
    "RUN_LOG_PROGRESS_MAX_BYTES",
    "begin_run_log",
    "cap_log_bytes",
    "current_run_token",
    "read_run_log_header",
    "run_log_is_current",
    "write_run_log",
    "write_run_log_progress",
]

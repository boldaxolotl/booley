"""Flow-neutral durable run-log persistence and freshness protocol."""

from __future__ import annotations

import os
import stat
import time
from dataclasses import dataclass
from pathlib import Path

from booley.core.booley_dir_links import is_booley_dir_entry
from booley.runtime.regular_file import open_regular_nofollow
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
    owner: Path | None = None,
    booley_dir: Path | None = None,
) -> Path:
    """Open a fresh run log with ownership and in-progress markers."""
    path = Path(work_dir, RUN_LOG_NAME)
    absolute = Path(os.path.abspath(path))  # noqa: PTH100 — preserve links
    for ancestor in (absolute, *absolute.parents):
        # The exact Booley Directory entry may be a link (#2191); any other stays refused.
        if ancestor.is_symlink() and not is_booley_dir_entry(ancestor, booley_dir):
            raise OSError("run log path contains a symbolic link")
        if owner is None or ancestor == owner:
            break
    flags = os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    with os.fdopen(os.open(path, flags, 0o600), "w", encoding="utf-8") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise OSError("run log must be a singly linked regular file")
        stream.truncate(0)
        header = format_run_log_header(flow=flow, target=target, run=run)
        stream.write(f"{header}{RUN_LOG_PENDING}\n")
    return path


@dataclass
class RunLogIdentity:
    """Writer-owned identity, refreshed only when the log's stat identity changes."""

    path: Path
    target: str
    run: str
    attempt: str
    fingerprint: tuple[int, ...]
    booley_dir: Path | None = None

    @classmethod
    def create(
        cls,
        root: Path,
        target: str,
        attempt: str,
        allowed: tuple[Path, ...],
        *,
        booley_dir: Path | None = None,
    ) -> RunLogIdentity:
        path = Path(os.path.abspath(root / RUN_LOG_NAME))  # noqa: PTH100 — preserve links
        owner = next((parent for parent in allowed if path.is_relative_to(parent)), None)
        if owner is None:
            raise OSError("run log lies outside the owned evidence roots")
        run = current_run_token()
        path = begin_run_log(
            root, flow="sim", target=target, run=run, owner=owner, booley_dir=booley_dir
        )
        return cls(path, target, run, attempt, cls._fingerprint(path), booley_dir)

    @staticmethod
    def _fingerprint(path: Path) -> tuple[int, ...]:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise OSError("run log must be a singly linked regular file")
        return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)

    def pointer(self, root: Path, token: str, project: Path) -> dict[str, object] | None:
        if root.absolute() != self.path.parent or (token and token != self.attempt):
            return None
        try:
            fingerprint = self._fingerprint(self.path)
            if fingerprint != self.fingerprint:
                with os.fdopen(
                    open_regular_nofollow(self.path, booley_dir=self.booley_dir),
                    "r",
                    encoding="utf-8",
                ) as stream:
                    fields = parse_run_log_header(stream.readline(4096))
                if fields is None or any(
                    fields.get(key) != value
                    for key, value in (("run", self.run), ("flow", "sim"), ("target", self.target))
                ):
                    return None
                self.fingerprint = fingerprint
            representation = (
                self.path.relative_to(project) if self.path.is_relative_to(project) else self.path
            )
            return {"path": str(representation), "live": True, "complete": False}
        except (OSError, ValueError):
            return None


def format_run_log_header(*, flow: str, target: str, run: str | None = None) -> str:
    """Render the ownership protocol independently of filesystem opening policy."""
    return (
        f"{RUN_LOG_HEADER_PREFIX} run={run or current_run_token()} "
        f"flow={flow} target={target} started={utc_now_rfc3339()}\n"
    )


def parse_run_log_header(line: str) -> dict[str, str] | None:
    """Parse one complete ownership line, rejecting partial headers."""
    if not line.startswith(RUN_LOG_HEADER_PREFIX) or not line.endswith("\n"):
        return None
    fields: dict[str, str] = {}
    for token in line[len(RUN_LOG_HEADER_PREFIX) :].split():
        key, separator, value = token.partition("=")
        if separator and value:
            fields[key] = value
    return fields


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
    return parse_run_log_header(first.decode("utf-8", errors="replace"))


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
    "format_run_log_header",
    "parse_run_log_header",
    "read_run_log_header",
    "run_log_is_current",
    "write_run_log",
    "write_run_log_progress",
]

"""Durable records for asynchronous endpoint dispatch (ADR 0027).

A long-running MCP tool call (``simulate``, ``fpga_impl``, ``asic_synthesize``)
can outlive the MCP *client's* ~60-90s call cap while the *server's* budget runs
into the minutes.  Rather than hold the MCP call open for the whole run — which
makes the client give up, orphans a live subprocess, and BLOCKs the next call —
the server spawns the endpoint subprocess as a background job, records it here, and
hands the caller a ``run_id`` to poll (see ``mcp_server._JobManager`` and the
``booley_poll`` synthetic MCP tool).

These records are the *durable* half of that contract.  The in-memory asyncio
task registry gives a clean same-session result, but it dies with the server;
the on-disk record lets a *fresh* server (after an idle-exit or restart)
re-derive a job's status by ``run_id`` — so the agent's poll always gets a
definite answer instead of losing the in-flight wait with its shell.

Pure + stdlib-only: no asyncio, no MCP types.  PID liveness (and the /proc argv
lookup) are injected so the status derivation stays unit-testable.  The
concurrency guarantee (only one heavy endpoint at a time) is NOT this module's job —
it lives in ``mcp_server._JobManager``'s single-flight admission, with the
detached child's own run-lock as belt-and-braces where that lock is active.
Async here means "don't block the MCP call", never "run endpoints concurrently".
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

from booley.runtime.timefmt import parse_timestamp
from booley.ticket_board.paths import ticket_runtime_dir

logger = logging.getLogger(__name__)

# Job lifecycle states. "running" is the only non-terminal one.
STATUS_RUNNING = "running"
STATUS_DONE = "done"  # subprocess exited 0
STATUS_FAILED = "failed"  # non-zero exit, timeout kill, or crash-without-record
STATUS_CANCELLED = "cancelled"  # explicitly withdrawn by booley_cancel

# Grace period past a job's watchdog budget before a still-"running" record is
# declared dead. The _run_subprocess watchdog SIGKILLs a runaway at timeout_s
# and the terminal record lands moments later, so a record still non-terminal
# well past its budget means the server died (no watchdog survives it) or the
# PID was recycled — either way nothing will ever finish this job.
DEADLINE_SLACK_SECONDS = 120.0

# A freshly submitted record is written just before its child PID is known.
# Treat that short pid-less interval as active so a finalization fence cannot
# slip between record creation and process spawn.
SPAWN_GRACE_SECONDS = 30.0


@dataclass
class JobRecord:
    """On-disk record of one detached endpoint run.

    ``pid`` is the child subprocess PID — the same PID the child stamps into its
    ``endpoint_start`` display event, so it is authoritative for liveness and lets a
    restarted server tell "still running" from "crashed" without the in-memory
    task.  It is ``None`` only in the sliver between record creation and process
    spawn (the record is written twice: once at submit, once the PID is known).
    """

    run_id: str
    endpoint: str
    started_at: str
    timeout_s: int
    argv: list[str] = field(default_factory=list)
    pid: int | None = None
    status: str = STATUS_RUNNING
    exit_code: int | None = None
    # When the child was first observed actually RUNNING (holding its slot),
    # stamped by the supervising server. ``started_at`` is submit time; with
    # queued admission (ADR 0028) the two can differ by the whole queue wait,
    # and every deadline must anchor here when available. None while queued,
    # or forever if the server died before the job left the queue.
    run_started_at: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> JobRecord:
        # Tolerate unknown keys from a future writer — read only what we model.
        known = {f: d.get(f) for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in known.items() if v is not None})


def terminal_status(exit_code: int, timed_out: bool) -> str:
    """Map a finished subprocess outcome to a terminal job status."""
    return STATUS_DONE if (exit_code == 0 and not timed_out) else STATUS_FAILED


def make_run_id(endpoint: str, started_compact: str, counter: int) -> str:
    """Build a session-unique run-id.

    ``started_compact`` is a filesystem-safe timestamp (e.g. ``20260704T131502Z``)
    and ``counter`` disambiguates same-second submits from the same server.
    """
    return f"{endpoint}-{started_compact}-{counter}"


def jobs_dir() -> Path | None:
    """Return the ``jobs`` dir for this run, or None when no runtime is set.

    Mirrors ``mcp_server._endpoint_reports_dir`` runtime resolution so job records
    land beside ``flow-reports/`` under the same bind-mounted runtime tree,
    readable across the host/sandbox boundary.
    """
    logs_dir = os.environ.get("BOOLEY_LOGS_DIR", "")
    if not logs_dir:
        return None
    runtime_env = os.environ.get("BOOLEY_RUNTIME_DIR", "")
    runtime_dir = Path(runtime_env) if runtime_env else ticket_runtime_dir(logs_dir)
    return runtime_dir / "jobs"


def _record_path(run_id: str) -> Path | None:
    root = jobs_dir()
    return (root / f"{run_id}.json") if root is not None else None


def write_record(rec: JobRecord) -> None:
    """Persist (or overwrite) a job record. Best-effort — never raises.

    Atomic (tmp + rename): the reader may be a *different* process — a
    restarted server resolving a poll across the host/sandbox boundary — and a
    half-written record would make it report "Unknown run_id" for a job that is
    fine. The tmp name carries our PID so two processes writing the same
    run_id cannot collide on the tmp file itself.
    """
    path = _record_path(rec.run_id)
    if path is None:
        return
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(rec.to_dict()) + "\n", encoding="utf-8")
        tmp.replace(path)
    except OSError:
        logger.debug("Failed to write job record %s", rec.run_id, exc_info=True)
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)  # don't leave a stray tmp behind


def read_record(run_id: str, root: Path | None = None) -> JobRecord | None:
    """Load a job record by run-id, or None if absent/unreadable."""
    path = (root / f"{run_id}.json") if root is not None else _record_path(run_id)
    if path is None or not path.is_file():
        return None
    try:
        return JobRecord.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        logger.warning("Malformed job record at %s", path, exc_info=True)
        return None


def list_records(root: Path | None = None) -> list[JobRecord]:
    """Return every job record on disk (unordered). Empty when none."""
    records_root = root if root is not None else jobs_dir()
    if records_root is None or not records_root.is_dir():
        return []
    out: list[JobRecord] = []
    for path in records_root.glob("*.json"):
        rec = read_record(path.stem, records_root)
        if rec is not None:
            out.append(rec)
    return out


def parse_stamp(stamp: object) -> float | None:
    """Epoch seconds for a ``%Y-%m-%dT%H:%M:%SZ`` stamp, or None if unparseable.

    Both job records (``started_at``) and endpoint reports (``timestamp``) use this
    exact format; tolerating garbage keeps status derivation best-effort for
    hand-edited or legacy records.
    """
    if not isinstance(stamp, str):
        return None
    try:
        return parse_timestamp(stamp).timestamp()
    except ValueError:
        return None


def _proc_cmdline(pid: int) -> list[str] | None:
    """argv of a live process from /proc, or None when unavailable.

    None (non-Linux, permission, or a raced exit) means "cannot judge" — the
    caller must NOT treat it as a mismatch, or a live job would be declared
    dead on platforms without /proc.
    """
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return None
    parts = [p for p in raw.decode("utf-8", errors="replace").split("\x00") if p]
    return parts or None


def derive_status(
    rec: JobRecord,
    is_pid_alive: Callable[[int], bool],
    *,
    now: float | None = None,
    read_cmdline: Callable[[int], list[str] | None] = _proc_cmdline,
) -> str:
    """Best-effort status for a record whose in-memory task is gone.

    A terminal record is authoritative — the background task recorded the
    outcome before the server died.  Otherwise fall back to PID liveness: a
    still-live child means the run genuinely survived (e.g. its own session
    outlived the server), a dead one with no recorded result means it was
    killed or crashed before recording, which is a failure from the caller's
    point of view.  A record with no PID yet (spawn hadn't happened) is treated
    as failed for the same reason — nothing is running to wait on.

    A bare "PID is alive" is not proof the *job* is alive, and a false RUNNING
    here is the worst outcome — the caller polls a ghost forever, the exact
    burned-attempts cascade ADR 0027 exists to kill.  Two guards close it:

    * **Deadline**: a record still non-terminal past ``started_at + timeout_s +
      DEADLINE_SLACK_SECONDS`` is failed regardless of the PID.  The watchdog
      guarantees no supervised job outlives its budget, and an *unsupervised*
      orphan (server SIGKILLed, watchdog gone) can hang forever — nothing will
      ever record success for it.
    * **Identity**: if the live PID's /proc argv no longer matches the recorded
      ``argv``, the PID was recycled by an unrelated process — container PID
      namespaces are small and dense, so across a restart this is a realistic
      collision, not paranoia.

    ``now`` (epoch seconds) and ``read_cmdline`` are injectable for tests.
    """
    if rec.status in (STATUS_DONE, STATUS_FAILED, STATUS_CANCELLED):
        return rec.status
    if rec.pid is None or not is_pid_alive(rec.pid):
        return STATUS_FAILED

    # Deadline anchors at the observed run start when the supervisor stamped
    # one — queue wait (ADR 0028) must not count against the run budget. A
    # record with no run_started_at (still queued, or its server died first)
    # conservatively falls back to submit time: strictly no worse than the
    # pre-stamp behavior, and the PID/argv guards still apply.
    started = parse_stamp(rec.run_started_at) or parse_stamp(rec.started_at)
    if started is not None and isinstance(rec.timeout_s, (int, float)):
        current = time.time() if now is None else now
        if current > started + rec.timeout_s + DEADLINE_SLACK_SECONDS:
            return STATUS_FAILED

    if rec.argv:
        cmdline = read_cmdline(rec.pid)
        if cmdline is not None and cmdline != rec.argv:
            return STATUS_FAILED  # PID recycled by an unrelated process

    return STATUS_RUNNING


def is_active(
    rec: JobRecord,
    is_pid_alive: Callable[[int], bool],
    *,
    now: float | None = None,
) -> bool:
    """Return whether a record still represents running or spawning work."""
    if rec.status != STATUS_RUNNING:
        return False
    if derive_status(rec, is_pid_alive, now=now) == STATUS_RUNNING:
        return True
    started = parse_stamp(rec.started_at)
    current = time.time() if now is None else now
    return rec.pid is None and started is not None and current - started < SPAWN_GRACE_SECONDS

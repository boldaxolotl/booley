"""Shared on-disk slot store for Job admission (ADR 0028).

Container-only Booley runs *everything* — the interactive session, every
concurrent ticket's Developer Agent, and each Flow/MCP-tool subprocess — inside the one
Session Runtime, so admission ("may this Job run now, or must it wait?") can
no longer live in any single process the way ADR 0027's in-process
single-flight did.  This module is the replacement: a filesystem-coordinated store of
claim files under ``<runtime>/jobs/slots/`` that every process shares.
Container-only guarantees one PID namespace, so PID liveness and ``/proc``
argv identity are trustworthy here — the exact rationale that made a shared
store unsound in the former split-runtime world no longer applies.

Every admission request is one **entry file** in the requesting Job's class
directory.  The scheme is bakery-style — create your own uniquely-named
entry, then rank all live entries; no entry ever needs to modify another
except to reap a provably-stale one:

* Entry names sort in scheduling order: ``h-`` holders (running) before
  ``w-`` waiters, then ``(priority, seq, pid, n)``.  Priority 0 is
  Interactive, 1 is Ticket — a later interactive request overtakes queued
  ticket work, but *never* a holder: promotion renames only one's **own**
  ``w-`` entry to ``h-``, and only when its rank is within the class cap, so
  running work cannot be preempted (ADR 0028 Decision 6).
* Because the ordering is total and each process promotes only itself under a
  short per-class promotion gate, two processes cannot both promote into the
  last free slot.
* Entries appear atomically (content written to a tmp, then hard-linked
  into place), so a reader never sees a partial claim.
* Stale entries — dead PID, recycled PID (``/proc`` argv mismatch), or a
  holder past its deadline — are reaped by whoever notices.  Unique names
  make reaping race-free: the unlink can only ever hit the exact entry that
  was judged stale, never a fresh claim reusing the name.

Pure + stdlib-only, like ``runtime.job_records`` (whose deadline/identity
guards it reuses): PID liveness, ``/proc`` reads, the clock, and sleep are
all injectable, so scheduling behavior is unit-testable without processes.
This module is mechanism only — *enforcement* (claim on entry, release in
``finally``) lives at the endpoint-process entry point, ``McpTool.main``,
with the Runner claiming TICKET slots at Developer Agent launch.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

from booley.runtime.execution_records import (
    RUNTIME_EXECUTION_ENV,
    execution_paths,
    read_json,
    request_cancellation,
)
from booley.runtime.job_records import (
    DEADLINE_SLACK_SECONDS,
    _proc_cmdline,
    parse_stamp,
)
from booley.runtime.pid import (
    DEAD,
    REUSED,
    ZOMBIE,
    ProcessIdentity,
    ProcessObservation,
    capture_process_identity,
    is_pid_alive,
    observe_process,
)
from booley.runtime.timefmt import rfc3339_from_epoch

logger = logging.getLogger(__name__)

# Job classes (ADR 0028 Decision 5), resolved by workload type.
CLASS_HEAVY = "heavy"  # in-container EDA subprocesses (sim/synth/elaborate)
CLASS_LIGHT = "light"  # Specialists — model-API-bound, ~no local footprint
CLASS_TICKET = "ticket"  # Developer Agent processes, claimed by the Runner
JOB_CLASSES = (CLASS_HEAVY, CLASS_LIGHT, CLASS_TICKET)

# Requester roles → queue priority (lower sorts first). Interactive work
# jumps ahead of queued ticket work; holders are never displaced.
ROLE_INTERACTIVE = "interactive"
ROLE_TICKET = "ticket"
_ROLE_PRIORITY = {ROLE_INTERACTIVE: 0, ROLE_TICKET: 1}

# Entry-name state prefixes. 'h' < 'w' lexicographically, so holders always
# rank ahead of every waiter regardless of priority — that ordering is what
# makes "no preemption" fall out of a plain sorted() call.
_HOLDER = "h"
_WAITER = "w"

# An entry file that cannot be parsed is foreign junk or corruption, never a
# half-written claim (creation is atomic). It is invisible to ranking and
# reaped once comfortably older than any plausible in-flight creation.
_UNREADABLE_REAP_AGE_SECONDS = 60.0

# How often a still-queued claim repeats its "waiting for slot" narration.
# Position-change-only narration went silent for the entire holder's run, which
# reads as a hang; a reminder every half minute is cheap and un-spammy (F-27).
NARRATE_INTERVAL_SECONDS = 30.0

# Windows may briefly deny rename/unlink while another claimant is reading the
# same entry. Retrying keeps the shared store from producing false losses or
# permanent phantom-holder deadlocks.
_ENTRY_IO_ATTEMPTS = 20
_ENTRY_IO_RETRY_SECONDS = 0.01

# Ranking and waiter->holder promotion must be one serialized decision. Without
# a gate, two processes can each observe themselves in the last free rank before
# either rename becomes visible, then both promote. O_EXCL creation is atomic on
# the local filesystems supported by the Session Runtime and Windows CI.
_PROMOTION_GATE_NAME = ".promotion.lock"

# Versioned holder identity + renewable recovery lease. Work budgets remain a
# separate field: a Developer Agent may legitimately have no work timeout, but
# every new holder must still be recoverable after its owner stops renewing.
SLOT_SCHEMA_VERSION = 2
LEASE_ACTIVE = "active"
LEASE_CANCELLING = "cancelling"
LEASE_DURATION_SECONDS = 30.0
LEASE_RENEW_INTERVAL_SECONDS = 10.0
_EXECUTION_ID_RE = re.compile(r"[0-9a-f]{32}")


class QueueFullError(RuntimeError):
    """The class queue is at ``queue_max`` — the submit must be refused.

    This is the only condition that still surfaces as BLOCKED to the agent
    (ADR 0028 Decision 8); anything under the cap waits in queue instead.
    """


class ClaimLostError(RuntimeError):
    """The waiting entry vanished — the queued job was cancelled.

    A live waiter's entry can only disappear through a deliberate unlink
    (``booley_cancel`` claiming the cancellation atomically); the ghost
    guards never reap a live, argv-matching waiter. Raised by ``acquire`` so
    a cancelled child exits instead of silently re-joining the queue —
    re-submitting would undo the cancellation.
    """


class ClaimAbortedError(RuntimeError):
    """The caller's ``should_abort`` hook fired while queued.

    Raised by ``acquire`` after withdrawing its own entry, so a shutdown
    request (e.g. the Runner's Ctrl+C event) can end a queue wait instead of
    idling until a slot frees.
    """


@dataclass
class SlotCaps:
    """Per-class concurrency caps + global queue bound ([jobs] in booley.toml).

    The defaults preserve the pre-ADR-0028 semantics exactly: one heavy EDA
    run at a time, a small pool for API-bound Specialists, and two concurrent
    tickets.
    """

    max_heavy: int = 1
    max_light: int = 3
    max_tickets: int = 2
    queue_max: int = 8

    def cap_for(self, job_class: str) -> int:
        caps = {
            CLASS_HEAVY: self.max_heavy,
            CLASS_LIGHT: self.max_light,
            CLASS_TICKET: self.max_tickets,
        }
        if job_class not in caps:
            raise ValueError(f"Unknown job class: {job_class!r}")
        # A zero/negative cap would deadlock every claimant; clamp loudly.
        cap = caps[job_class]
        if cap < 1:
            logger.warning("Cap for %s is %r; clamping to 1", job_class, cap)
            return 1
        return cap


# Token states returned by SlotStore.refresh().
HOLDING = "holding"  # the token owns a slot — run
QUEUED = "queued"  # waiting; position is meaningful
LOST = "lost"  # the entry vanished (reaped or cancelled) — do not run


@dataclass
class TokenState:
    state: str
    position: int | None = None  # 0-based queue position when QUEUED


@dataclass
class SlotToken:
    """Handle for one admission request — one entry file, owned by one PID.

    ``path`` tracks the entry through its waiter→holder rename. Treat as
    opaque outside this module; only the creating process should promote or
    release it (reaping stale entries is the one sanctioned exception).
    """

    job_class: str
    role: str
    priority: int
    seq: int
    pid: int
    n: int
    argv: list[str]
    created_at: str
    timeout_s: float | None
    path: Path
    # Stamped into the entry payload when the owner promotes itself to holder
    # (None while waiting, or for a holder that crashed between the rename and
    # the payload rewrite). The holder deadline anchors here, NOT at
    # created_at: queue wait must never count against the run budget.
    promoted_at: str | None = None
    schema_version: int = 1
    lease_id: str = ""
    execution_id: str | None = None
    lease_state: str = LEASE_ACTIVE
    lease_generation: int = 0
    lease_expires_at: str | None = None
    owner_identity: ProcessIdentity | None = None
    owner_kind: str = "process"

    @property
    def is_holder(self) -> bool:
        return self.path.name.startswith(f"{_HOLDER}-")


def slots_dir() -> Path | None:
    """Root of the shared slot store, or None when no project is resolvable.

    PROJECT-scoped (``.booley_project/runtime/jobs/slots/``), deliberately
    NOT the per-ticket ``jobs/`` tree the JobRecords live in: admission must
    arbitrate across every workload in the one container — the interactive
    session and all concurrent tickets — so their claims must land in one
    directory. ``BOOLEY_SLOTS_DIR`` overrides for tests and doctor probes.
    """
    env = os.environ.get("BOOLEY_SLOTS_DIR", "")
    if env:
        return Path(env)
    try:
        from booley.runtime.project_dir import resolve_project_dir

        project = resolve_project_dir()
    except Exception:  # noqa: BLE001 — no project ⇒ no admission (bare runs)
        return None
    return project / "runtime" / "jobs" / "slots"


def _default_execution_is_terminal(execution_id: str) -> bool:
    payload = read_json(execution_paths(execution_id).record)
    return bool(
        payload is not None
        and payload.get("state") == "terminal"
        and payload.get("tree_terminal") is True
    )


def _default_cancel_execution(execution_id: str) -> None:
    request_cancellation(execution_paths(execution_id))


def _entry_name(state: str, priority: int, seq: int, pid: int, n: int) -> str:
    # Fixed-width fields so lexicographic filename order == scheduling order.
    return f"{state}-p{priority}-s{seq:010d}-{pid:010d}-{n:04d}.json"


def _parse_entry_name(name: str) -> tuple[str, int, int, int, int] | None:
    """(state, priority, seq, pid, n) from an entry filename, or None."""
    stem = name.removesuffix(".json")
    parts = stem.split("-")
    if len(parts) != 5 or parts[0] not in (_HOLDER, _WAITER):
        return None
    try:
        return (
            parts[0],
            int(parts[1].removeprefix("p")),
            int(parts[2].removeprefix("s")),
            int(parts[3]),
            int(parts[4]),
        )
    except ValueError:
        return None


def _utc_stamp(epoch: float) -> str:
    return rfc3339_from_epoch(epoch)


def _argv_label(argv: list[str]) -> str:
    """Best-effort "what is this process" label from a recorded argv.

    Entries store argv, not an endpoint name, so recover the Booley Flow name
    from ``booley flow <name>`` / ``python -m booley.flows.<name>`` and fall
    back to the executable's basename.
    """
    for i, part in enumerate(argv):
        if part == "flow" and i + 1 < len(argv):
            return argv[i + 1]
        if part.startswith("booley.flows."):
            return part.rsplit(".", 1)[-1]
    return Path(argv[0]).name if argv else "unknown"


def _describe_holder(tok: SlotToken, now: float) -> str:
    """Render one holder as ``pid 123 (sim), held 10m12s``."""
    label = _argv_label(tok.argv) or tok.role
    started = parse_stamp(tok.promoted_at) or parse_stamp(tok.created_at)
    if started is None:
        return f"pid {tok.pid} ({label})"
    held = max(0, int(now - started))
    return f"pid {tok.pid} ({label}), held {held // 60}m{held % 60:02d}s"


_default_pid_alive = is_pid_alive


def parse_caps(data: dict) -> SlotCaps:
    """Parse [jobs] concurrency caps from a loaded booley.toml dict.

    Defaults preserve the pre-slot-store semantics for local workloads.
    Invalid values warn and keep the default —
    a typo in a cap must not change admission behavior silently. Lives here
    (not in harness config) so endpoint subprocesses can resolve caps without
    importing the harness.
    """
    section = data.get("jobs", {})
    caps = SlotCaps()
    if not isinstance(section, dict):
        logger.warning("[jobs] is not a table; using defaults")
        return caps
    known = ("max_heavy", "max_light", "max_tickets", "queue_max")
    # Resource reservation is consumed by Doctor's admission invariant, not
    # by the integer slot-cap parser.  It is still a recognized [jobs] key.
    non_cap_keys = {"heavy_memory"}
    for key in known:
        if key not in section:
            continue
        val = section[key]
        floor = 0 if key == "queue_max" else 1
        if isinstance(val, int) and not isinstance(val, bool) and val >= floor:
            setattr(caps, key, val)
        else:
            logger.warning(
                "[jobs] %s = %r is invalid (int >= %d); using %d",
                key,
                val,
                floor,
                getattr(caps, key),
            )
    for key in section:
        if key not in known and key not in non_cap_keys:
            logger.warning("[jobs] has unknown key %r (ignored)", key)
    return caps


class SlotStore:
    """Claim/queue/release over one slots directory. Safe across processes.

    All clock/PID/proc access is injected so tests can simulate multi-process
    interleavings deterministically; production callers use the defaults.
    """

    def __init__(
        self,
        root: Path,
        caps: SlotCaps | None = None,
        *,
        is_pid_alive: Callable[[int], bool] = _default_pid_alive,
        read_cmdline: Callable[[int], list[str] | None] = _proc_cmdline,
        now: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        capture_identity: Callable[[int], ProcessIdentity | None] = capture_process_identity,
        observe_identity: Callable[[ProcessIdentity], ProcessObservation] = observe_process,
        execution_is_terminal: Callable[[str], bool] = _default_execution_is_terminal,
        cancel_execution: Callable[[str], None] = _default_cancel_execution,
    ) -> None:
        self.root = root
        self.caps = caps or SlotCaps()
        self._is_pid_alive = is_pid_alive
        self._read_cmdline = read_cmdline
        self._now = now
        self._sleep = sleep
        self._capture_identity = capture_identity
        self._observe_identity = observe_identity
        self._execution_is_terminal = execution_is_terminal
        self._cancel_execution = cancel_execution
        self._n = 0  # per-store counter: distinct entries from one process
        self._auto_renew = now is time.time and sleep is time.sleep
        self._renewals: dict[str, tuple[threading.Event, threading.Thread]] = {}

    # ---------------------------------------------------------------- submit

    def submit(
        self,
        job_class: str,
        *,
        pid: int,
        argv: list[str] | None = None,
        role: str = ROLE_INTERACTIVE,
        timeout_s: float | None = None,
        execution_id: str | None = None,
    ) -> SlotToken:
        """Join the admission order for *job_class*.

        Returns a token that may already be HOLDING (check with
        :meth:`refresh`). Raises :class:`QueueFullError` when the class queue
        is already at ``queue_max`` — the caller surfaces that as BLOCKED.
        """
        cap = self.caps.cap_for(job_class)
        cls_dir = self.root / job_class
        cls_dir.mkdir(parents=True, exist_ok=True)
        self.reap(job_class)

        # Refuse before creating: a full queue must not grow further. The
        # bound is total live entries (slots + queue) — an entry that has not
        # promoted yet still occupies capacity, not queue. The count can race
        # a concurrent submit; the post-create rank check catches whatever
        # slips through.
        if len(self._live_entry_names(job_class)) >= cap + self.caps.queue_max:
            raise QueueFullError(f"{job_class} queue is full ({self.caps.queue_max} waiting)")

        priority = _ROLE_PRIORITY.get(role, _ROLE_PRIORITY[ROLE_TICKET])
        created = self._now()
        linked_execution = execution_id or os.environ.get(RUNTIME_EXECUTION_ENV) or None
        if linked_execution is not None and _EXECUTION_ID_RE.fullmatch(linked_execution) is None:
            raise ValueError("execution_id must be 32 lowercase hexadecimal characters")
        token = self._create_entry(
            cls_dir,
            priority,
            pid,
            argv or [],
            role,
            timeout_s,
            created,
            linked_execution,
        )

        # Post-create backstop: if a racing submit pushed us past the bound,
        # withdraw our own entry and refuse — the queue must stay bounded.
        if self._rank(token) >= cap + self.caps.queue_max:
            self.release(token)
            raise QueueFullError(f"{job_class} queue is full ({self.caps.queue_max} waiting)")
        return token

    def _create_entry(
        self,
        cls_dir: Path,
        priority: int,
        pid: int,
        argv: list[str],
        role: str,
        timeout_s: float | None,
        created: float,
        execution_id: str | None,
    ) -> SlotToken:
        """Create a uniquely-named waiter entry; atomic full-content appearance."""
        owner_identity = self._capture_identity(pid)
        lease_id = uuid.uuid4().hex
        payload_base = {
            "schema_version": SLOT_SCHEMA_VERSION,
            "role": role,
            "argv": argv,
            "created_at": _utc_stamp(created),
            "timeout_s": timeout_s,
            "lease_id": lease_id,
            "execution_id": execution_id,
            "owner_kind": "execution" if execution_id is not None else "process",
            "lease_state": LEASE_ACTIVE,
            "lease_generation": 0,
            "lease_expires_at": None,
            "owner_identity": self._identity_payload(owner_identity),
        }
        while True:
            seq = self._next_seq(cls_dir)
            self._n += 1
            name = _entry_name(_WAITER, priority, seq, pid, self._n)
            path = cls_dir / name
            payload = dict(payload_base, seq=seq)
            tmp = cls_dir / f".{name}.{pid}.tmp"
            try:
                tmp.write_text(json.dumps(payload) + "\n", encoding="utf-8")
                os.link(tmp, path)  # atomic; EEXIST on the ~impossible collision
            except FileExistsError:
                continue  # same pid+n+seq raced us — pick a fresh seq
            finally:
                with contextlib.suppress(OSError):
                    tmp.unlink(missing_ok=True)
            return SlotToken(
                job_class=cls_dir.name,
                role=role,
                priority=priority,
                seq=seq,
                pid=pid,
                n=self._n,
                argv=list(argv),
                created_at=payload["created_at"],
                timeout_s=timeout_s,
                path=path,
                schema_version=SLOT_SCHEMA_VERSION,
                lease_id=lease_id,
                execution_id=execution_id,
                owner_identity=owner_identity,
                owner_kind="execution" if execution_id is not None else "process",
            )

    @staticmethod
    def _identity_payload(identity: ProcessIdentity | None) -> dict | None:
        if identity is None:
            return None
        return {
            "pid": identity.pid,
            "pid_namespace": identity.pid_namespace,
            "start_ticks": identity.start_ticks,
        }

    def _next_seq(self, cls_dir: Path) -> int:
        """Max seq among live entries + 1. Ties across processes are fine —
        the (seq, pid, n) name suffix keeps the order total."""
        top = 0
        for path in cls_dir.glob("*.json"):
            parsed = _parse_entry_name(path.name)
            if parsed is not None:
                top = max(top, parsed[2])
        return top + 1

    # ------------------------------------------------------- refresh/promote

    def refresh(self, token: SlotToken) -> TokenState:
        """Re-derive *token*'s state, promoting it to holder when its turn comes.

        Call repeatedly while QUEUED. Only the token's owner may call this —
        promotion renames the entry file, and that is an owner-only act.
        """
        self.reap(token.job_class)
        if not token.path.exists():
            return TokenState(LOST)
        if token.is_holder:
            return TokenState(HOLDING)
        return self._refresh_waiter(token)

    def _refresh_waiter(self, token: SlotToken) -> TokenState:
        """Recheck and promote one waiter without an observation race."""
        cap = self.caps.cap_for(token.job_class)
        rank = self._rank(token)
        if rank >= cap:
            return TokenState(QUEUED, position=rank - cap)

        with self._promotion_gate(token) as acquired:
            if not acquired:
                return TokenState(QUEUED, position=0)

            # Submissions do not take the gate, so repeat both cleanup and
            # ranking inside it. A newly-created earlier waiter must be
            # visible before this process commits its promotion.
            self.reap(token.job_class)
            if not token.path.exists():
                return TokenState(LOST)
            rank = self._rank(token)
            if rank >= cap:
                return TokenState(QUEUED, position=rank - cap)

            return self._promote(token)

    def _promote(self, token: SlotToken) -> TokenState:
        """Rename one eligible waiter while its class promotion gate is held."""
        if not self._stamp_promoted(token):
            return TokenState(QUEUED, position=0)
        holder_path = token.path.with_name(
            _entry_name(_HOLDER, token.priority, token.seq, token.pid, token.n)
        )
        if not self._rename_entry(token.path, holder_path):
            # A Windows sharing violation is not cancellation. If the
            # waiter still exists, leave it queued and retry next poll.
            return TokenState(QUEUED, position=0) if token.path.exists() else TokenState(LOST)
        token.path = holder_path
        return TokenState(HOLDING)

    @contextlib.contextmanager
    def _promotion_gate(self, token: SlotToken) -> Iterator[bool]:
        """Best-effort cross-process gate around the promotion decision."""
        gate = token.path.parent / _PROMOTION_GATE_NAME
        fd: int | None = None
        for attempt in range(_ENTRY_IO_ATTEMPTS):
            try:
                fd = os.open(gate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                if self._promotion_gate_is_stale(gate):
                    self._unlink_entry(gate)
                elif attempt + 1 < _ENTRY_IO_ATTEMPTS:
                    self._sleep(_ENTRY_IO_RETRY_SECONDS)
            except OSError:
                break
            else:
                try:
                    os.write(fd, f"{token.pid}\n".encode())
                except OSError:
                    os.close(fd)
                    fd = None
                    self._unlink_entry(gate)
                break

        acquired = fd is not None
        try:
            yield acquired
        finally:
            if fd is not None:
                os.close(fd)
                self._unlink_entry(gate)

    def _promotion_gate_is_stale(self, gate: Path) -> bool:
        """Return True only for an old gate whose owner is provably dead."""
        try:
            if self._now() - gate.stat().st_mtime <= _UNREADABLE_REAP_AGE_SECONDS:
                return False
        except OSError:
            return False
        try:
            pid = int(gate.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return True
        return not self._is_pid_alive(pid)

    def _rename_entry(self, source: Path, target: Path) -> bool:
        """Promote an entry through transient Windows sharing violations."""
        for attempt in range(_ENTRY_IO_ATTEMPTS):
            try:
                source.rename(target)
                return True
            except PermissionError:
                if attempt + 1 < _ENTRY_IO_ATTEMPTS:
                    self._sleep(_ENTRY_IO_RETRY_SECONDS)
            except OSError:
                return False
        return False

    def _token_payload(self, token: SlotToken) -> dict:
        return {
            "schema_version": token.schema_version,
            "role": token.role,
            "argv": token.argv,
            "created_at": token.created_at,
            "timeout_s": token.timeout_s,
            "seq": token.seq,
            "promoted_at": token.promoted_at,
            "lease_id": token.lease_id,
            "execution_id": token.execution_id,
            "owner_kind": token.owner_kind,
            "lease_state": token.lease_state,
            "lease_generation": token.lease_generation,
            "lease_expires_at": token.lease_expires_at,
            "owner_identity": self._identity_payload(token.owner_identity),
        }

    def _rewrite_token(self, token: SlotToken) -> bool:
        tmp = token.path.with_name(f".{token.path.name}.{os.getpid()}.rewrite.tmp")
        try:
            tmp.write_text(json.dumps(self._token_payload(token)) + "\n", encoding="utf-8")
            tmp.replace(token.path)
        except OSError:
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)
            return False
        return True

    def _stamp_promoted(self, token: SlotToken) -> bool:
        """Stamp complete holder metadata before the waiter→holder rename.

        The holder deadline (``_is_stale``) anchors at promotion, not entry
        creation — an entry can legitimately queue for longer than its own run
        budget, and charging that wait against the deadline would reap a
        *live* holder. Writing before rename makes a crash leave either a
        waiter or a fully described holder, never an immortal half-promotion.
        """
        promoted = self._now()
        token.promoted_at = _utc_stamp(promoted)
        token.lease_expires_at = _utc_stamp(promoted + LEASE_DURATION_SECONDS)
        return self._rewrite_token(token)

    def acquire(
        self,
        job_class: str,
        *,
        pid: int,
        argv: list[str] | None = None,
        role: str = ROLE_INTERACTIVE,
        timeout_s: float | None = None,
        poll_interval: float = 1.0,
        on_queued: Callable[[int | None], None] | None = None,
        should_abort: Callable[[], bool] | None = None,
        narrate_interval_s: float = NARRATE_INTERVAL_SECONDS,
    ) -> SlotToken:
        """Blocking convenience: submit, then poll until HOLDING.

        ``on_queued(position)`` fires whenever the position changes, and again
        every ``narrate_interval_s`` while the wait continues — the Console's
        "waiting for slot (position N)" narration hook. The periodic repeat
        exists because a position that never moves used to narrate exactly once
        and then go silent for as long as the holder ran (F-27). Raises
        :class:`ClaimLostError` when the entry vanishes while waiting: a
        live waiter is only ever unlinked by a deliberate cancellation, so
        re-submitting would undo it. ``should_abort`` is checked once per
        poll; when it returns True the entry is withdrawn and
        :class:`ClaimAbortedError` raised — the shutdown hook for callers
        whose signal handlers set an event instead of raising.

        Any exception escaping the wait (including KeyboardInterrupt)
        withdraws our own entry first: a claim without a caller-held token is
        unreleasable, and — with this process alive and its argv matching —
        unreapable, i.e. a permanent slot leak.
        """
        token = self.submit(job_class, pid=pid, argv=argv, role=role, timeout_s=timeout_s)
        last_pos: int | None = None
        last_narrated: float | None = None
        try:
            while True:
                if should_abort is not None and should_abort():
                    raise ClaimAbortedError(
                        f"aborted while waiting for a {job_class} slot (shutdown requested)"
                    )
                state = self.refresh(token)
                if state.state == HOLDING:
                    self._start_renewal(token)
                    return token
                if state.state == LOST:
                    raise ClaimLostError(
                        f"{job_class} queue entry vanished while waiting — the job was cancelled"
                    )
                now = self._now()
                due = last_narrated is None or (now - last_narrated) >= narrate_interval_s
                if on_queued is not None and (state.position != last_pos or due):
                    last_pos = state.position
                    last_narrated = now
                    on_queued(state.position)
                self._sleep(poll_interval)
        except BaseException:
            self.release(token)  # idempotent; no-op on the LOST path
            raise

    # ------------------------------------------------------- release/inspect

    def release(self, token: SlotToken) -> None:
        """Release a slot or withdraw a queued entry. Idempotent, never raises."""
        renewal = self._renewals.pop(token.lease_id, None)
        if renewal is not None:
            stop, thread = renewal
            stop.set()
            if thread is not threading.current_thread():
                thread.join(timeout=1)
        with self._lease_gate(token) as acquired:
            if not acquired:
                return
            current = self._load_token(token.path)
            if current is None:
                return
            if current.lease_id and current.lease_id != token.lease_id:
                return
            if (
                current.lease_state == LEASE_CANCELLING
                and current.execution_id is not None
                and not self._execution_is_terminal(current.execution_id)
            ):
                return
            self._unlink_entry(token.path)

    def renew(self, token: SlotToken) -> bool:
        """Extend one active holder lease; refuse after recovery has claimed it."""
        with self._lease_gate(token) as acquired:
            if not acquired:
                return False
            current = self._load_token(token.path)
            if (
                current is None
                or not current.is_holder
                or current.lease_id != token.lease_id
                or current.lease_state != LEASE_ACTIVE
            ):
                return False
            current.lease_generation += 1
            current.lease_expires_at = _utc_stamp(self._now() + LEASE_DURATION_SECONDS)
            if not self._rewrite_token(current):
                return False
            token.lease_generation = current.lease_generation
            token.lease_expires_at = current.lease_expires_at
            return True

    def _start_renewal(self, token: SlotToken) -> None:
        if not self._auto_renew or not token.lease_id or token.lease_id in self._renewals:
            return
        stop = threading.Event()

        def maintain() -> None:
            while not stop.wait(LEASE_RENEW_INTERVAL_SECONDS):
                if not self.renew(token):
                    return

        thread = threading.Thread(target=maintain, name=f"booley-lease-{token.lease_id[:8]}")
        thread.daemon = True
        self._renewals[token.lease_id] = (stop, thread)
        thread.start()

    @contextlib.contextmanager
    def _lease_gate(self, token: SlotToken) -> Iterator[bool]:
        if not token.lease_id:
            yield True  # legacy entries never race renewal
            return
        gate = token.path.parent / f".{token.lease_id}.lease.lock"
        acquired = False
        for attempt in range(_ENTRY_IO_ATTEMPTS):
            try:
                gate.mkdir()
            except FileExistsError:
                if self._is_old_junk(gate):
                    with contextlib.suppress(OSError):
                        gate.rmdir()
                elif attempt + 1 < _ENTRY_IO_ATTEMPTS:
                    self._sleep(_ENTRY_IO_RETRY_SECONDS)
            except OSError:
                break
            else:
                acquired = True
                break
        try:
            yield acquired
        finally:
            if acquired:
                with contextlib.suppress(OSError):
                    gate.rmdir()

    def _unlink_entry(self, path: Path) -> bool:
        """Remove an entry, retrying transient Windows sharing violations."""
        for attempt in range(_ENTRY_IO_ATTEMPTS):
            try:
                path.unlink(missing_ok=True)
                return True
            except PermissionError:
                if attempt + 1 < _ENTRY_IO_ATTEMPTS:
                    self._sleep(_ENTRY_IO_RETRY_SECONDS)
            except OSError:
                return False
        logger.warning("Could not remove slot entry after retries: %s", path.name)
        return False

    def _rank(self, token: SlotToken) -> int:
        """Position of *token* in the total scheduling order of live entries."""
        names = self._live_entry_names(token.job_class)
        own = token.path.name
        if own not in names:
            names.append(own)  # raced a reap; rank as if still present
        return sorted(names).index(own)

    def snapshot(self, job_class: str) -> tuple[list[SlotToken], list[SlotToken]]:
        """(holders, waiters) in scheduling order — for narration and doctor.

        Reaps first, so the view reflects only live claimants.
        """
        self.reap(job_class)
        holders: list[SlotToken] = []
        waiters: list[SlotToken] = []
        cls_dir = self.root / job_class
        if not cls_dir.is_dir():
            return ([], [])
        for name in sorted(self._live_entry_names(job_class)):
            tok = self._load_token(cls_dir / name)
            if tok is None:
                continue
            (holders if tok.is_holder else waiters).append(tok)
        return (holders, waiters)

    def describe_holders(self, job_class: str) -> str:
        """Who is occupying *job_class* right now, as one human-readable line.

        Feeds the "waiting for slot" narration (F-27): a queue wait that names
        nobody is indistinguishable from a hang, and the observed failure mode
        was an asic job sitting ~10 minutes behind a wedged sim in silence.
        """
        holders, _waiters = self.snapshot(job_class)
        if not holders:
            return "holder unknown (it released while we looked)"
        return "; ".join(_describe_holder(tok, self._now()) for tok in holders)

    def _load_token(self, path: Path) -> SlotToken | None:
        parsed = _parse_entry_name(path.name)
        if parsed is None:
            return None
        _state, priority, seq, pid, n = parsed
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            return None
        owner_payload = payload.get("owner_identity")
        owner_identity: ProcessIdentity | None = None
        if isinstance(owner_payload, dict):
            owner_pid = owner_payload.get("pid")
            namespace = owner_payload.get("pid_namespace")
            start_ticks = owner_payload.get("start_ticks")
            if isinstance(owner_pid, int) and isinstance(namespace, str) and isinstance(
                start_ticks, int
            ):
                owner_identity = ProcessIdentity(owner_pid, namespace, start_ticks)
        return SlotToken(
            job_class=path.parent.name,
            role=payload.get("role", ROLE_INTERACTIVE),
            priority=priority,
            seq=seq,
            pid=pid,
            n=n,
            argv=list(payload.get("argv") or []),
            created_at=payload.get("created_at", ""),
            timeout_s=payload.get("timeout_s"),
            path=path,
            promoted_at=payload.get("promoted_at"),
            schema_version=payload.get("schema_version", 1),
            lease_id=payload.get("lease_id", ""),
            execution_id=payload.get("execution_id"),
            lease_state=payload.get("lease_state", LEASE_ACTIVE),
            lease_generation=payload.get("lease_generation", 0),
            lease_expires_at=payload.get("lease_expires_at"),
            owner_identity=owner_identity,
            owner_kind=payload.get("owner_kind", "legacy"),
        )

    def cancel_waiter(self, pid: int) -> bool:
        """Atomically cancel the QUEUED entry owned by *pid*. True on success.

        The unlink is the cancellation claim: it can only succeed while the
        entry still has its waiter name — promotion renames the file, so a
        child that became a holder in the meantime makes this fail (False)
        and the cancel must be refused. The cancelled child's ``acquire``
        then sees LOST and raises instead of re-queueing.
        """
        for job_class in JOB_CLASSES:
            _holders, waiters = self.snapshot(job_class)
            for tok in waiters:
                if tok.pid != pid:
                    continue
                try:
                    tok.path.unlink()
                except OSError:
                    return False  # promoted (or vanished) under us — refuse
                return True
        return False

    def state_for_pid(self, pid: int) -> TokenState | None:
        """Admission state of the entry owned by *pid*, or None when absent.

        The submit/poll dispatch layer narrates a detached child's phase from
        this: a holder is genuinely RUNNING, a waiter is QUEUED at a position.
        Scans every class — the caller knows the PID (from the JobRecord),
        not the class.
        """
        for job_class in JOB_CLASSES:
            holders, waiters = self.snapshot(job_class)
            for tok in holders:
                if tok.pid == pid:
                    return TokenState(HOLDING)
            for position, tok in enumerate(waiters):
                if tok.pid == pid:
                    return TokenState(QUEUED, position=position)
        return None

    # ----------------------------------------------------------------- reap

    def _live_entry_names(self, job_class: str) -> list[str]:
        cls_dir = self.root / job_class
        if not cls_dir.is_dir():
            return []
        return [p.name for p in cls_dir.glob("*.json") if _parse_entry_name(p.name) is not None]

    def reap(self, job_class: str) -> list[str]:
        """Remove provably-stale entries; return the reaped filenames.

        Reuses the ADR 0027 ghost guards: a dead PID is stale; a live PID
        whose /proc argv no longer matches the recorded argv is a recycled
        PID (one shared PID namespace makes this judgment sound); a *holder*
        still present past ``created_at + timeout_s + slack`` is an
        unsupervised orphan nothing will ever release. Waiters get no
        deadline — queueing arbitrarily long is legitimate. Unreadable
        non-entry junk is removed once old enough to rule out in-flight
        creation.
        """
        cls_dir = self.root / job_class
        if not cls_dir.is_dir():
            return []
        reaped: list[str] = []
        for path in list(cls_dir.glob("*.json")):
            if self._is_stale(path) and self._unlink_entry(path):
                reaped.append(path.name)
                logger.info("Reaped stale slot entry %s", path.name)
        return reaped

    def _is_stale(self, path: Path) -> bool:
        parsed = _parse_entry_name(path.name)
        if parsed is None:
            return self._is_old_junk(path)
        state, _priority, _seq, _pid, _n = parsed

        tok = self._load_token(path)
        if tok is None:
            # Named like an entry but unreadable — corrupt; same junk rule.
            return self._is_old_junk(path)

        # Unknown future schemas fail closed: this process cannot safely infer
        # ownership or recovery rules from fields it does not understand.
        if not isinstance(tok.schema_version, int) or isinstance(tok.schema_version, bool):
            return False
        if tok.schema_version > SLOT_SCHEMA_VERSION:
            return False

        if tok.execution_id is not None:
            return self._linked_execution_is_stale(tok, state)
        return self._process_owner_is_stale(tok, state)

    def _linked_execution_is_stale(self, tok: SlotToken, state: str) -> bool:
        if self._execution_is_terminal(tok.execution_id or ""):
            return self._begin_execution_recovery(tok, request_cancel=False)
        owner_stale = self._owner_is_stale(tok)
        deadline_reached = state == _HOLDER and (
            self._lease_expired(tok) or self._work_deadline_expired(tok)
        )
        if owner_stale or deadline_reached:
            self._begin_execution_recovery(tok, request_cancel=True)
        return False

    def _process_owner_is_stale(self, tok: SlotToken, state: str) -> bool:
        if self._owner_is_stale(tok):
            return True
        return state == _HOLDER and self._work_deadline_expired(tok)

    def _owner_is_stale(self, tok: SlotToken) -> bool:
        if tok.owner_identity is not None:
            return self._observe_identity(tok.owner_identity).state in {DEAD, REUSED, ZOMBIE}
        return not self._is_pid_alive(tok.pid) or self._argv_was_reused(tok)

    def _argv_was_reused(self, tok: SlotToken) -> bool:
        if not tok.argv:
            return False
        cmdline = self._read_cmdline(tok.pid)
        return cmdline is not None and cmdline != tok.argv

    def _lease_expired(self, tok: SlotToken) -> bool:
        deadline = parse_stamp(tok.lease_expires_at)
        if deadline is None:
            anchor = parse_stamp(tok.promoted_at) or parse_stamp(tok.created_at)
            deadline = anchor + LEASE_DURATION_SECONDS if anchor is not None else None
        return deadline is not None and self._now() > deadline

    def _work_deadline_expired(self, tok: SlotToken) -> bool:
        if tok.timeout_s is None:
            return False
        started = parse_stamp(tok.promoted_at)
        return bool(
            started is not None
            and self._now() > started + tok.timeout_s + DEADLINE_SLACK_SECONDS
        )

    def _begin_execution_recovery(self, tok: SlotToken, *, request_cancel: bool) -> bool:
        if tok.execution_id is None:
            return False
        if tok.lease_state == LEASE_CANCELLING:
            return True
        claimed = False
        with self._lease_gate(tok) as acquired:
            current = self._load_token(tok.path) if acquired else None
            if current is not None and current.lease_state == LEASE_CANCELLING:
                claimed = True
            elif current is not None and current.lease_state == LEASE_ACTIVE:
                current.lease_state = LEASE_CANCELLING
                current.lease_generation += 1
                claimed = self._rewrite_token(current)
                if claimed and request_cancel:
                    self._cancel_execution(current.execution_id or tok.execution_id)
        return claimed

    def _is_old_junk(self, path: Path) -> bool:
        try:
            age = self._now() - path.stat().st_mtime
        except OSError:
            return False  # vanished under us — nothing to reap
        return age > _UNREADABLE_REAP_AGE_SECONDS

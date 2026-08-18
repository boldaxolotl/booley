"""Shared on-disk slot store for Job admission (ADR 0028).

Container-only Booley runs *everything* — the interactive session, every
concurrent ticket's Developer Agent, and each Flow/MCP-tool subprocess — inside the one
Session Runtime, so admission ("may this Job run now, or must it wait?") can
no longer live in any single process the way ADR 0027's in-process
single-flight did.  This module is the replacement: a lock-free store of
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
* Because the ordering is total and each process promotes only itself,
  at most one waiter can ever observe itself at the head — two processes
  cannot both promote into the last free slot.
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
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from booley.runtime.job_records import (
    DEADLINE_SLACK_SECONDS,
    _proc_cmdline,
    parse_stamp,
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
# same entry. Retrying keeps the lock-free store from producing false losses or
# permanent phantom-holder deadlocks.
_ENTRY_IO_ATTEMPTS = 20
_ENTRY_IO_RETRY_SECONDS = 0.01


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


def _windows_pid_alive(pid: int) -> bool:
    # os.kill(pid, 0) is not a liveness probe on Windows: signal 0 is
    # CTRL_C_EVENT, which GenerateConsoleCtrlEvent sprays across the
    # whole console — interrupting the very session that spawned us.
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    ERROR_INVALID_PARAMETER = 87
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        # Access denial and transient API failures are not proof of death. A
        # false negative here lets another claimant reap a live process.
        return ctypes.get_last_error() != ERROR_INVALID_PARAMETER
    try:
        # OpenProcess can succeed for recently-exited processes;
        # verify the process hasn't terminated via its exit code.
        exit_code = wintypes.DWORD()
        STILL_ACTIVE = 259
        if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return exit_code.value == STILL_ACTIVE
        return True  # API call failed — assume alive to avoid false negatives
    finally:
        kernel32.CloseHandle(handle)


def _default_pid_alive(pid: int) -> bool:
    if sys.platform == "win32":
        return _windows_pid_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    except OSError:
        return False
    return True


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
    ) -> None:
        self.root = root
        self.caps = caps or SlotCaps()
        self._is_pid_alive = is_pid_alive
        self._read_cmdline = read_cmdline
        self._now = now
        self._sleep = sleep
        self._n = 0  # per-store counter: distinct entries from one process

    # ---------------------------------------------------------------- submit

    def submit(
        self,
        job_class: str,
        *,
        pid: int,
        argv: list[str] | None = None,
        role: str = ROLE_INTERACTIVE,
        timeout_s: float | None = None,
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
        token = self._create_entry(cls_dir, priority, pid, argv or [], role, timeout_s, created)

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
    ) -> SlotToken:
        """Create a uniquely-named waiter entry; atomic full-content appearance."""
        payload_base = {
            "role": role,
            "argv": argv,
            "created_at": _utc_stamp(created),
            "timeout_s": timeout_s,
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
            )

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

        rank = self._rank(token)
        if rank < self.caps.cap_for(token.job_class):
            holder_path = token.path.with_name(
                _entry_name(_HOLDER, token.priority, token.seq, token.pid, token.n)
            )
            if not self._rename_entry(token.path, holder_path):
                # A Windows sharing violation is not cancellation. If the
                # waiter still exists, leave it queued and retry next poll.
                return TokenState(QUEUED, position=0) if token.path.exists() else TokenState(LOST)
            token.path = holder_path
            self._stamp_promoted(token)
            return TokenState(HOLDING)
        return TokenState(QUEUED, position=rank - self.caps.cap_for(token.job_class))

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

    def _stamp_promoted(self, token: SlotToken) -> None:
        """Rewrite the freshly-promoted entry with a ``promoted_at`` stamp.

        The holder deadline (``_is_stale``) anchors at promotion, not entry
        creation — an entry can legitimately queue for longer than its own run
        budget, and charging that wait against the deadline would reap a
        *live* holder (freeing a slot that is still occupied → overcommit).
        Atomic tmp + replace so a concurrent reader sees old or new payload,
        never a partial one. Best-effort: on failure the entry simply keeps no
        deadline, exactly the pre-stamp behavior.
        """
        token.promoted_at = _utc_stamp(self._now())
        payload = {
            "role": token.role,
            "argv": token.argv,
            "created_at": token.created_at,
            "timeout_s": token.timeout_s,
            "seq": token.seq,
            "promoted_at": token.promoted_at,
        }
        tmp = token.path.with_name(f".{token.path.name}.{token.pid}.promote.tmp")
        try:
            tmp.write_text(json.dumps(payload) + "\n", encoding="utf-8")
            tmp.replace(token.path)
        except OSError:
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)

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
        self._unlink_entry(token.path)

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
        state, _priority, _seq, pid, _n = parsed

        tok = self._load_token(path)
        if tok is None:
            # Named like an entry but unreadable — corrupt; same junk rule.
            return self._is_old_junk(path)

        if not self._is_pid_alive(pid):
            return True
        if tok.argv:
            cmdline = self._read_cmdline(pid)
            if cmdline is not None and cmdline != tok.argv:
                return True  # PID recycled by an unrelated process

        if state == _HOLDER and tok.timeout_s is not None:
            # Anchor at promotion, never at entry creation: queue wait must
            # not count against the run budget. A holder without promoted_at
            # (crashed between rename and payload rewrite, or a pre-stamp
            # writer) gets no deadline — the PID guards still cover it.
            started = parse_stamp(tok.promoted_at)
            if (
                started is not None
                and self._now() > started + tok.timeout_s + DEADLINE_SLACK_SECONDS
            ):
                return True  # orphaned holder past any possible budget
        return False

    def _is_old_junk(self, path: Path) -> bool:
        try:
            age = self._now() - path.stat().st_mtime
        except OSError:
            return False  # vanished under us — nothing to reap
        return age > _UNREADABLE_REAP_AGE_SECONDS

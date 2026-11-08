"""Whether anyone still renders a running Dashboard, and how to retire one nobody does.

A browser reload cannot always deliver terminal disposal to the remote
terminal host, so a Dashboard process can outlive every view of it. The one
signal that means "someone renders this terminal" is a terminal round trip:
an emulator attached to the pty answers a cursor-position request, a pty
whose renderer is gone does not. The Dashboard sends that request on its
refresh heartbeat and records when it was last answered in a `ViewState`.
`serve` publishes the age of that answer on the Dashboard's Sandbox socket, so
a newcomer or the attach policy can call the owner *unseen* once the age
passes `UNSEEN_AFTER`, and `retire` it before taking over.

Nothing here is a guess. An owner that cannot prove it is unseen (an older
Booley that never listens, a process of another user, a malformed reply)
counts as seen, so a live view is never taken away without evidence.
"""

from __future__ import annotations

import math
import os
import signal
import socket
import struct
import threading
import time
from dataclasses import dataclass
from pathlib import Path

# Age of the last terminal answer, in seconds, up to which the view is seen.
SEEN_WITHIN = 6.0
# Age from which the view is unseen; between the two the probe waits and asks once more.
UNSEEN_AFTER = 15.0
# Longest wait for an owner to connect and answer before it counts as wedged.
REPLY_SECONDS = 2.0
# Grace the owner gets after SIGTERM, and after SIGKILL, before retire gives up.
TERM_GRACE_SECONDS = 3.0
KILL_GRACE_SECONDS = 2.0
# Added to the re-probe wait so a rounded age never lands just under the threshold.
_REPROBE_MARGIN = 0.1
_POLL_SECONDS = 0.05
_ACCEPT_POLL_SECONDS = 0.2
_MAX_REPLY_BYTES = 64
# Gone states in /proc/<pid>/stat: zombie (not yet reaped) and dead.
_GONE_STATES = frozenset("ZXx")
_PEERCRED = struct.Struct("3i")  # struct ucred: pid, uid, gid


def socket_name(scope: str) -> str:
    """Abstract-namespace address of the Dashboard owning *scope*, as bound."""
    return "\0booley-dashboard:" + scope


class ViewState:
    """When the Dashboard started and when its terminal last answered (thread-safe)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._started = time.monotonic()
        self._answered: float | None = None

    def answered(self) -> None:
        """Record a terminal answer now."""
        with self._lock:
            self._answered = time.monotonic()

    def age(self) -> float:
        """Seconds since the last answer; a Dashboard that has not asked yet counts from start."""
        with self._lock:
            reference = self._started if self._answered is None else self._answered
            return time.monotonic() - reference


def serve(listener: socket.socket, state: ViewState, stop: threading.Event) -> threading.Thread:
    """Answer every connection with the view age on its own thread, until *stop* or a closed socket.

    The thread never waits on the Dashboard's event loop, so a starting or busy
    Dashboard still replies and a hung one simply stops refreshing its age.
    """
    listener.settimeout(_ACCEPT_POLL_SECONDS)

    def loop() -> None:
        while not stop.is_set():
            try:
                connection, _ = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                if stop.is_set() or listener.fileno() == -1:
                    return
                # A transient failure (no free descriptor) must not silence the
                # reply thread for good: a silent owner is taken for wedged.
                stop.wait(_ACCEPT_POLL_SECONDS)
                continue
            with connection:
                connection.settimeout(REPLY_SECONDS)
                try:
                    connection.sendall(f"{state.age():.3f}\n".encode("ascii"))
                except OSError:
                    continue

    thread = threading.Thread(target=loop, name="dashboard-view-probe", daemon=True)
    thread.start()
    return thread


@dataclass(frozen=True)
class ViewOwner:
    """A Dashboard process, identified by pid and start time so a reused pid is never signalled."""

    pid: int
    start_time: str


@dataclass(frozen=True)
class _Reading:
    owner: ViewOwner
    age: float | None  # None: connected but no reply in time


def _process_stat(pid: int) -> tuple[str, str] | None:
    """State and start time of *pid* from /proc, or None when it is gone or unreadable."""
    try:
        text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return None
    # The command name is parenthesised and may hold spaces and parentheses.
    fields = text[text.rfind(")") + 2 :].split()
    if len(fields) < 20:
        return None
    return fields[0], fields[19]


def _read_owner(scope: str) -> _Reading | None:
    """Connect to the Dashboard of *scope*; None when no provable owner answers the socket."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
        probe.settimeout(REPLY_SECONDS)
        try:
            probe.connect(socket_name(scope))
            pid, uid, _ = _PEERCRED.unpack(
                probe.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, _PEERCRED.size)
            )
        except OSError:
            # Refused (older Booley, bind-to-listen gap), gone, or too busy to accept.
            return None
        stat = _process_stat(pid)
        if uid != os.getuid() or stat is None:
            return None
        owner = ViewOwner(pid, stat[1])
        try:
            age = float(probe.recv(_MAX_REPLY_BYTES).decode("ascii").strip())
        except TimeoutError:
            return _Reading(owner, None)  # accepted the connection, never answered: wedged
        except (OSError, ValueError):
            return None  # closed without a reply or a malformed one: not provable
        return _Reading(owner, age) if math.isfinite(age) and age >= 0 else None


def _pause_before_second_look(age: float | None) -> float | None:
    """Seconds to wait before asking the owner once more, or None when one look decides.

    A silent owner is asked again at once: a single late reply (a starved reply
    thread, a loaded host) must not cost a watched Dashboard its view. An age
    between seen and unseen waits until a silent owner would be unseen.
    """
    if age is None:
        return 0.0
    if SEEN_WITHIN < age < UNSEEN_AFTER:
        return UNSEEN_AFTER - age + _REPROBE_MARGIN
    return None


def unseen_owner(scope: str) -> ViewOwner | None:
    """The Dashboard of *scope* when it is provably unseen, else None.

    Seen, still starting, and every case that cannot be proven return None.
    """
    reading = _read_owner(scope)
    if reading is None:
        return None
    pause = _pause_before_second_look(reading.age)
    if pause is not None:
        time.sleep(pause)
        second = _read_owner(scope)
        if second is None or second.owner != reading.owner:
            return None
        reading = second
    return reading.owner if reading.age is None or reading.age >= UNSEEN_AFTER else None


def _is_gone(owner: ViewOwner) -> bool:
    stat = _process_stat(owner.pid)
    return stat is None or stat[0] in _GONE_STATES or stat[1] != owner.start_time


def _wait_gone(owner: ViewOwner, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while not _is_gone(owner):
        if time.monotonic() >= deadline:
            return False
        time.sleep(_POLL_SECONDS)
    return True


def retire(owner: ViewOwner) -> bool:
    """Stop an unseen Dashboard (SIGTERM, then SIGKILL); True once it is gone.

    A process that no longer has the recorded start time is not the Dashboard
    any more, so it is never signalled.
    """
    for sig, grace in ((signal.SIGTERM, TERM_GRACE_SECONDS), (signal.SIGKILL, KILL_GRACE_SECONDS)):
        if _is_gone(owner):
            return True
        try:
            os.kill(owner.pid, sig)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        if _wait_gone(owner, grace):
            return True
    return False

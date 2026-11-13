"""Rank a memory-admitted Job's processes for the kernel's OOM killer (ADR 0087).

The kernel kills the process with the highest ``oom_badness``: its memory plus
``oom_score_adj`` x limit / 1000, so in shares of the limit the score is
``1000 x share + oom_score_adj``. While a Job holds a Memory Reservation its
processes get a value by admission order: the newest running Job's tools are
killed first, every Job's processes before the Developer Agent's (which keep
the container default, 0). The Job's coordinating process (the one holding the
claim) ranks just below its own tools, so an OOM kill takes a tool and leaves
the coordinator able to report the kill or requeue the Job.

A child inherits its parent's value at fork, so a coordinator ranked below its
tools cannot hand them a higher value by inheritance, and tools start from many
call sites. A daemon thread therefore re-applies the ranking to the
coordinator's process tree every ``RANK_INTERVAL_SECONDS``: a tool just
started runs at its coordinator's value (still above the agent) for at most
that long, and a re-rank after another Job starts or ends converges within one
interval. Where ``/proc`` is unavailable nothing changes.
"""

from __future__ import annotations

import contextlib
import logging
import threading
from collections.abc import Collection
from contextlib import AbstractContextManager
from pathlib import Path
from types import TracebackType

from booley.runtime import job_slots, process_tree

logger = logging.getLogger(__name__)

#: ``oom_score_adj`` of a Job's tool processes by the number of running memory
#: Jobs admitted after it (index clamped to the last entry). The newest running
#: Job is always 1000; the wide first gap lets it lose even against a much
#: larger older Job, since it is the Job most likely to have tipped the container.
TOOL_ADJ: tuple[int, ...] = (1000, 500, 450, 400, 350, 300)

#: The coordinator ranks this far below its Job's tools: half the smallest step
#: between levels, so it stays above the processes of an older Job that has a
#: lower level (every older Job until the levels bottom out at five newer Jobs).
COORDINATOR_GAP = 25

#: How often the ranking is re-applied to the Job's process tree.
RANK_INTERVAL_SECONDS = 0.5

#: A coordinator that already starts at or above its lowest level is left alone:
#: ranking must never lower a process below the value the agent's processes share.
LOWEST_COORDINATOR_ADJ = TOOL_ADJ[-1] - COORDINATOR_GAP

_JOIN_TIMEOUT_SECONDS = 5.0


def tool_adj(newer: int) -> int:
    """The tool level of a Job that *newer* running Jobs were admitted after."""
    return TOOL_ADJ[min(newer, len(TOOL_ADJ) - 1)]


class ProcOom:
    """The ``/proc`` accessor: the only place that reads or writes ``oom_score_adj``."""

    def read(self, pid: int) -> int | None:
        """*pid*'s ``oom_score_adj``, or ``None`` when ``/proc`` cannot say."""
        try:
            return int(Path(f"/proc/{pid}/oom_score_adj").read_text().strip())
        except (OSError, ValueError):
            return None

    def write(self, pid: int, value: int) -> bool:
        """Set *pid*'s ``oom_score_adj``; ``False`` when the kernel refuses (exited, setuid)."""
        try:
            Path(f"/proc/{pid}/oom_score_adj").write_text(str(value))
        except OSError as exc:
            logger.debug("oom_score_adj of %d not set to %d: %s", pid, value, exc)
            return False
        return True

    def descendants(self, pid: int, exclude: Collection[int]) -> list[int]:
        """*pid*'s descendants, skipping the subtrees rooted at *exclude*."""
        return process_tree.descendant_pids(pid, exclude=exclude)

    def nested_in(self, pid: int, others: Collection[int]) -> bool:
        """Whether *pid* descends from one of the *others* (a Job inside another Job)."""
        return any(process_tree.has_ancestor(pid, other) for other in others if other != pid)


PROC = ProcOom()


class JobOomRanking:
    """Keep one memory Job's process tree ranked while its claim is held."""

    def __init__(
        self, store: job_slots.SlotStore, token: job_slots.SlotToken, proc: ProcOom
    ) -> None:
        self._store = store
        self._token = token
        self._proc = proc
        self._previous: int | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> None:
        previous = self._proc.read(self._token.pid)
        if previous is None:
            return  # no /proc: nothing can be ranked
        try:
            if previous >= LOWEST_COORDINATOR_ADJ and not self._nested_in_another_job():
                return  # the process already ranks at least as high as we would
            self.apply()
        except OSError as exc:
            logger.debug("OOM ranking not started: %s", exc)
            return
        self._previous = previous
        try:
            thread = threading.Thread(target=self._loop, name="oom-priority", daemon=True)
            thread.start()
        except RuntimeError as exc:  # no thread to be had, for example under memory pressure
            logger.debug("OOM ranking thread not started: %s", exc)
            self._proc.write(self._token.pid, previous)
            self._previous = None
            return
        self._thread = thread

    def _nested_in_another_job(self) -> bool:
        """Whether a Custom Flow command runs this Job inside another Job's tree.

        Such a coordinator inherited its value from the outer Job's ranking
        (at or above the lowest level), and the outer Job leaves its subtree to
        it, so it must rank itself.
        """
        holders, _waiters = self._store.observe(job_slots.CLASS_MEMORY)
        return self._proc.nested_in(
            self._token.pid, {holder.pid for holder in holders if holder.pid != self._token.pid}
        )

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._thread is None or self._previous is None:
            return
        self._stop.set()
        self._thread.join(timeout=_JOIN_TIMEOUT_SECONDS)
        self._proc.write(self._token.pid, self._previous)

    def _loop(self) -> None:
        """Re-rank until stopped; a pass that fails is retried at the next interval."""
        while not self._stop.wait(RANK_INTERVAL_SECONDS):
            try:
                self.apply()
            except OSError as exc:
                logger.debug("OOM ranking pass failed: %s", exc)

    def apply(self) -> None:
        """Write the coordinator's and its descendants' values from the current holders.

        One PID is one Job, ordered by its earliest entry: a Campaign's work
        item claims share the Flow's PID, so they never count as newer Jobs
        against their own Flow and count once against another Job.
        """
        holders, _waiters = self._store.observe(job_slots.CLASS_MEMORY)
        if not any(holder.lease_id == self._token.lease_id for holder in holders):
            return  # the claim is gone; the next pass or the exit settles it
        order: dict[int, tuple[int, str]] = {}
        for holder in holders:
            key = (holder.admitted or 0, holder.path.name)
            order[holder.pid] = min(key, order.get(holder.pid, key))
        own_key = order.get(self._token.pid)
        if own_key is None:
            return
        others = {pid for pid in order if pid != self._token.pid}
        newer = sum(1 for pid in others if order[pid] > own_key)
        level = tool_adj(newer)
        self._set(self._token.pid, level - COORDINATOR_GAP)
        for pid in self._proc.descendants(self._token.pid, others):
            self._set(pid, level)

    def _set(self, pid: int, value: int) -> None:
        if self._stop.is_set():
            return  # the exit restored the coordinator; a late pass must not undo that
        if self._proc.read(pid) not in (None, value):
            self._proc.write(pid, value)


def job_oom_priority(
    store: job_slots.SlotStore | None, token: object | None
) -> AbstractContextManager[None]:
    """Rank the processes of the Job holding *token*; a no-op for any non-memory claim."""
    if (
        store is None
        or not isinstance(token, job_slots.SlotToken)
        or token.job_class != job_slots.CLASS_MEMORY
    ):
        return contextlib.nullcontext()
    return JobOomRanking(store, token, PROC)

"""Stop a running Job whose checkout or Booley Directory is removed.

The main thread is interrupted as by Ctrl-C, which the tool runners answer by
killing their tool trees. The tools are also signalled directly: the interrupt
does not wake a blocked ``communicate``, nor reach tools run by other threads.
"""

from __future__ import annotations

import _thread
import contextlib
import logging
import os
import signal
import threading
from collections.abc import Callable

from booley.runtime.process_group import foreign_group_of, signal_group
from booley.runtime.process_tree import descendant_pids

logger = logging.getLogger(__name__)

_INTERVAL_SECONDS = 2.0
_THREAD_NAME = "booley-run-root-watchdog"
# Windows has no SIGKILL, and no /proc, so no tool is signalled there.
_FORCE_SIGNAL: int = getattr(signal, "SIGKILL", signal.SIGTERM)


class RunRootWatchdog:
    """Watch the run roots from a thread for as long as the context is open.

    *check* names a removed root, else returns ``None``. :attr:`removed` holds
    that name once the watchdog has stopped the run.
    """

    def __init__(self, check: Callable[[], str | None]) -> None:
        self._check = check
        self.removed: str | None = None
        self._stop = threading.Event()
        # Remembered across ticks: a reparented child is no longer a descendant.
        # Tools in this process's own group (an agent CLI) are remembered by PID.
        self._pids: set[int] = set()
        self._groups: set[int] = set()
        # Children from before the run: neither they nor their subtrees are its tools.
        self._others: frozenset[int] = frozenset()
        self._interrupts = threading.current_thread() is threading.main_thread()
        self._thread = threading.Thread(target=self._watch, name=_THREAD_NAME, daemon=True)

    def __enter__(self) -> RunRootWatchdog:
        self._others = frozenset(descendant_pids(os.getpid()))
        self._thread.start()
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        """Join the thread; after a removal, kill any tool left. Safe to call again."""
        self._stop.set()
        self._thread.join()
        if self.removed is not None:
            self._signal_tools(_FORCE_SIGNAL)

    def _watch(self) -> None:
        while not self._stop.wait(_INTERVAL_SECONDS):
            if self.removed is not None:
                self._signal_tools(_FORCE_SIGNAL)
                continue
            removed = self._check()
            if removed is None:
                continue
            self.removed = removed
            logger.warning("Stopping the run: its %s is gone", removed)
            # Find the tools before the interrupt lets the run reap them.
            self._find_tools()
            if self._interrupts:
                _thread.interrupt_main()
            self._signal_tools(signal.SIGTERM)

    def _find_tools(self) -> None:
        """Remember every tool: its process group, or its PID when it shares this one."""
        for pid in descendant_pids(os.getpid(), exclude=self._others):
            with contextlib.suppress(ProcessLookupError):
                group = foreign_group_of(pid)
                if group is None:
                    self._pids.add(pid)
                else:
                    self._groups.add(group)

    def _signal_tools(self, signum: int) -> None:
        self._find_tools()
        # Windows remembers no tool, so neither signal is sent there.
        _signal_each(self._pids, lambda pid: os.kill(pid, signum))
        _signal_each(self._groups, lambda group: signal_group(group, signum))


def _signal_each(targets: set[int], send: Callable[[int], None]) -> None:
    """Signal each target; forget a gone one, so a reuse of its number is left alone."""
    for target in list(targets):
        with contextlib.suppress(PermissionError):
            try:
                send(target)
            except ProcessLookupError:
                targets.discard(target)

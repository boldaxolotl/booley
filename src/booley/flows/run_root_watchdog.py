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
        self._groups: set[int] = set()
        self._interrupts = threading.current_thread() is threading.main_thread()
        self._thread = threading.Thread(target=self._watch, name=_THREAD_NAME, daemon=True)

    def __enter__(self) -> RunRootWatchdog:
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
            self._find_tool_groups()
            if self._interrupts:
                _thread.interrupt_main()
            self._signal_tools(signal.SIGTERM)

    def _find_tool_groups(self) -> list[int]:
        """Remember the tools' process groups; return the tools in this process's own group."""
        ungrouped: list[int] = []
        for pid in descendant_pids(os.getpid()):
            with contextlib.suppress(ProcessLookupError):
                group = os.getpgid(pid)
                if group == os.getpgrp():
                    ungrouped.append(pid)
                else:
                    self._groups.add(group)
        return ungrouped

    def _signal_tools(self, signum: int) -> None:
        for pid in self._find_tool_groups():
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, signum)
        for group in list(self._groups):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(group, signum)

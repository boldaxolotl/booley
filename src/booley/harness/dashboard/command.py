"""Sandbox-only Dashboard command and one-view-per-Sandbox ownership."""

import errno
import os
import socket
import sys
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path

from booley.harness.dashboard import view_probe
from booley.harness.dashboard.app import DashboardApp
from booley.harness.dashboard.launch import Launcher
from booley.harness.dashboard.model import DashboardReader
from booley.harness.dashboard.targets import TargetsReader
from booley.mcp.session_registry import namespace
from booley.runtime.booley_dir import resolve_common_booley_dir

ALREADY_OPEN_REASON = (
    "The Dashboard is already open in another window or terminal of this Sandbox. "
    "Close it there (or press q in it), then run booley dashboard here."
)
_PLATFORM = sys.platform


def _name_dashboard_process() -> None:
    """An optional process adapter must leave unsupported launches silent."""
    try:
        from setproctitle import setproctitle
    except (ImportError, OSError):
        return
    with suppress(OSError, ValueError, RuntimeError):
        setproctitle("dashboard")


def _label_terminal() -> Callable[[], None]:
    """Label a Linux Dashboard, returning best-effort, once-only title cleanup."""
    stream = sys.stdout
    pushed = False
    restored = False

    def restore() -> None:
        nonlocal restored
        if restored:
            return
        restored = True
        if pushed:
            try:
                stream.write("\x1b[23;2t")
                stream.flush()
            except (AttributeError, OSError, ValueError, UnicodeError):
                pass

    if _PLATFORM != "linux":
        return restore
    _name_dashboard_process()
    if os.environ.get("NO_COLOR") or os.environ.get("TERM") == "dumb":
        return restore
    try:
        if stream.isatty():
            stream.write("\x1b[22;2t")
            pushed = True
            stream.write("\x1b]2;Booley Dashboard\x07")
            stream.flush()
    except (AttributeError, OSError, ValueError, UnicodeError):
        pass
    except BaseException:
        # Acquisition has no outer finally yet; unwind a completed push first.
        restore()
        raise
    return restore


def _bind(owner: socket.socket, address: str, patience: float) -> OSError | None:
    """Bind *address*, retrying while it stays in use for up to *patience* seconds."""
    deadline = time.monotonic() + patience
    while True:
        try:
            owner.bind(address)
            return None
        except OSError as exc:
            if exc.errno != errno.EADDRINUSE or time.monotonic() >= deadline:
                return exc
        time.sleep(view_probe.POLL_SECONDS)


def _bind_view(owner: socket.socket, scope: str) -> int | None:
    """Claim the Sandbox view; None when claimed, else the exit code of the refusal.

    A running Dashboard that provably nobody sees is retired once and the claim
    retried. Anything seen, starting, or unprovable keeps the view.
    """
    address = view_probe.socket_name(scope)
    failure = _bind(owner, address, 0.0)
    if failure is not None and failure.errno == errno.EADDRINUSE:
        stale = view_probe.unseen_owner(scope)
        if stale is not None:
            if not view_probe.retire(stale):
                print(
                    "Dashboard unavailable: an unresponsive Dashboard still holds the Sandbox view"
                )
                return 2
            # The address frees when the process's last thread exits, which can
            # be a moment after its main thread already shows as gone.
            failure = _bind(owner, address, view_probe.RELEASE_GRACE_SECONDS)
    if failure is None:
        return None
    if failure.errno != errno.EADDRINUSE:
        print(f"Dashboard unavailable: cannot reserve the Sandbox view: {failure}")
        return 2
    print(ALREADY_OPEN_REASON)
    return 0


def run_dashboard(root: Path) -> int:
    """A repeat launch keeps the existing view; an abstract socket leaves no filesystem state."""
    if not hasattr(socket, "AF_UNIX"):
        print("Dashboard unavailable: AF_UNIX sockets are unsupported")
        return 2
    scope = namespace()
    if not scope:
        print("Dashboard unavailable: Sandbox PID namespace cannot be observed")
        return 2
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as owner:
        refusal = _bind_view(owner, scope)
        if refusal is not None:
            return refusal
        _run_view(root, owner)
    return 0


def _run_view(root: Path, owner: socket.socket) -> None:
    """Show the Dashboard while answering view probes on the claimed socket."""
    state = view_probe.ViewState()
    stop = threading.Event()
    owner.listen()
    server = view_probe.serve(owner, state, stop)
    try:
        restore = _label_terminal()
        try:
            booley_dir = resolve_common_booley_dir(root)
            DashboardApp(
                DashboardReader(root, booley_dir).read,
                view_state=state,
                targets=TargetsReader(root),
                launcher=Launcher(booley_dir),
            ).run()
        finally:
            restore()
    finally:
        stop.set()
        server.join(timeout=1)

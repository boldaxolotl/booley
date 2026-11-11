"""Sandbox-only Dashboard command and one-view-per-Sandbox attachment policy."""

import errno
import json
import os
import socket
import sys
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from itertools import islice
from pathlib import Path

from booley.harness.dashboard import view_probe
from booley.harness.dashboard.app import DashboardApp
from booley.harness.dashboard.launch import Launcher
from booley.harness.dashboard.model import DashboardReader
from booley.harness.dashboard.targets import TargetsReader
from booley.mcp.session_registry import namespace
from booley.runtime.booley_dir import resolve_common_booley_dir
from booley.runtime.dashboard_tasks import attach_blocker

ALREADY_OPEN = 3
ATTACH_DISABLED = 4
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


def attach_policy(root: Path) -> dict[str, object]:
    """Read live Project policy without starting a terminal or changing any files."""
    reason = attach_blocker(root, resolve_common_booley_dir(root))
    # Computed even when a Project blocker applies, so the explicit open command
    # can tell an open Dashboard from a disabled automatic open.
    try:
        already_open = _dashboard_is_open()
    except RuntimeError:
        if not reason:
            raise
        already_open = False  # a Project opt-out was always reported without probing
    if already_open and not reason:
        reason = "The Dashboard is already open in this Sandbox."
    return {"protocol": 1, "enabled": not reason, "reason": reason, "already_open": already_open}


def _dashboard_is_open() -> bool:
    """A Dashboard holds the Sandbox view and something still renders it (or cannot be proven not to)."""
    scope = namespace()
    if not scope or not hasattr(socket, "AF_UNIX"):
        raise RuntimeError("Sandbox Dashboard ownership is unavailable")
    address = "@booley-dashboard:" + scope
    # A probe must not reserve the live view's socket, even momentarily. Linux
    # exposes abstract addresses here, in the caller's network namespace.
    with Path("/proc/net/unix").open(encoding="utf-8") as sockets:
        for index, row in enumerate(islice(sockets, 65537)):
            if index == 65536:
                raise RuntimeError("Sandbox socket inventory exceeds 65536 entries")
            fields = row.split()
            if len(fields) == 8 and fields[-1] == address:
                return view_probe.unseen_owner(scope) is None
    return False


def run_attach(root: Path, *, policy_only: bool = False) -> int:
    """Internal editor protocol; ordinary manual Dashboard behavior stays unchanged."""
    try:
        if policy_only:
            print(json.dumps(attach_policy(root)))
            return 0
        # Only Project policy is rechecked here: `run_dashboard` makes the single
        # ownership decision, so one attach probes the running Dashboard once.
        reason = attach_blocker(root, resolve_common_booley_dir(root))
    except (OSError, ValueError, RuntimeError, UnicodeError) as exc:
        print(f"Dashboard attach unavailable: {exc}", file=sys.stderr)
        return 2
    if reason:
        print(reason)
        return ATTACH_DISABLED
    return run_dashboard(root, on_attach=True)


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


def _bind_view(owner: socket.socket, scope: str, *, on_attach: bool) -> int | None:
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
    print("Booley Dashboard is already open in this Sandbox")
    return ALREADY_OPEN if on_attach else 0


def run_dashboard(root: Path, *, on_attach: bool = False) -> int:
    """Repeat attach keeps the existing view; an abstract socket leaves no filesystem state."""
    if not hasattr(socket, "AF_UNIX"):
        print("Dashboard unavailable: AF_UNIX sockets are unsupported")
        return 2
    scope = namespace()
    if not scope:
        print("Dashboard unavailable: Sandbox PID namespace cannot be observed")
        return 2
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as owner:
        refusal = _bind_view(owner, scope, on_attach=on_attach)
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

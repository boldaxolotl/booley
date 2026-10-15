"""Sandbox-only Dashboard command and one-view-per-Sandbox attachment policy."""

import errno
import json
import socket
import sys
from pathlib import Path

from booley.harness.dashboard.app import DashboardApp
from booley.harness.dashboard.model import DashboardReader
from booley.mcp.session_registry import namespace
from booley.runtime.booley_dir import resolve_booley_dir
from booley.runtime.dashboard_tasks import attach_blocker

ALREADY_OPEN = 3
ATTACH_DISABLED = 4


def attach_policy(root: Path) -> dict[str, object]:
    """Read live Project policy without starting a terminal or changing any files."""
    reason = attach_blocker(root, resolve_booley_dir(root))
    already_open = not reason and _dashboard_is_open()
    if already_open:
        reason = "The Dashboard is already open in this Sandbox."
    return {"protocol": 1, "enabled": not reason, "reason": reason, "already_open": already_open}


def _dashboard_is_open() -> bool:
    scope = namespace()
    if not scope or not hasattr(socket, "AF_UNIX"):
        raise RuntimeError("Sandbox Dashboard ownership is unavailable")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
        try:
            probe.bind("\0booley-dashboard:" + scope)
        except OSError as exc:
            if exc.errno == errno.EADDRINUSE:
                return True
            raise
    return False


def run_attach(root: Path, *, policy_only: bool = False) -> int:
    """Internal editor protocol; ordinary manual Dashboard behavior stays unchanged."""
    try:
        policy = attach_policy(root)
    except (OSError, ValueError, RuntimeError, UnicodeError) as exc:
        print(f"Dashboard attach unavailable: {exc}", file=sys.stderr)
        return 2
    if policy_only:
        print(json.dumps(policy))
        return 0
    if policy["already_open"]:
        print(policy["reason"])
        return ALREADY_OPEN
    if not policy["enabled"]:
        print(policy["reason"])
        return ATTACH_DISABLED
    return run_dashboard(root, on_attach=True)


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
        try:
            owner.bind("\0booley-dashboard:" + scope)
        except OSError as exc:
            if exc.errno == errno.EADDRINUSE:
                print("Booley Dashboard is already open in this Sandbox")
                return ALREADY_OPEN if on_attach else 0
            print(f"Dashboard unavailable: cannot reserve the Sandbox view: {exc}")
            return 2
        DashboardApp(DashboardReader(root, resolve_booley_dir(root)).read).run()
    return 0

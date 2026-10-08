"""Sandbox-only Dashboard command and one-view-per-Sandbox attachment policy."""

import errno
import socket
from pathlib import Path

from booley.harness.dashboard.app import DashboardApp
from booley.harness.dashboard.model import DashboardReader
from booley.mcp.session_registry import namespace
from booley.runtime.project_dir import resolve_project_dir


def run_dashboard(root: Path) -> int:
    """Repeat attach keeps the existing view; an abstract socket leaves no filesystem state."""
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
                return 0
            print(f"Dashboard unavailable: cannot reserve the Sandbox view: {exc}")
            return 2
        DashboardApp(DashboardReader(root, resolve_project_dir(root)).read).run()
    return 0

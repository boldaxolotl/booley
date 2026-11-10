"""Sandbox detection and entry-point guards.

Booley workflows execute inside the per-project Sandbox.  This module
depends only on the stdlib, ``booley.core`` and the import-free leaf
``sandbox_layout``, so it sits at the bottom of the import graph and every
entry point can detect that runtime and restore its fixed egress proxy
environment.
"""

from __future__ import annotations

import os
from enum import Enum
from pathlib import Path, PurePosixPath

from booley.core.egress_modes import EGRESS_MODES
from booley.runtime.sandbox_layout import (
    BOOLEY_DIR_TARGET,
    DEVCONTAINER_TARGET,
    PROJECT_DATA_MOUNT_PATH,
)


def inside_session_runtime() -> bool:
    """Return whether this process runs inside a Booley Sandbox."""
    if os.environ.get("BOOLEY_CONTAINER") == "1":
        return True
    return Path("/.dockerenv").exists() or Path("/run/.containerenv").exists()


class RuntimeLocation(Enum):
    """Where this process runs, as far as `booley init` needs to tell."""

    HOST = "host"
    HAND_WRITTEN_SANDBOX = "hand-written Sandbox"
    ISSUED_SANDBOX = "Booley-issued Sandbox"


_MOUNTINFO = Path("/proc/self/mountinfo")
#: Mount points only Session Issuance creates; a process cannot unmount them unprivileged.
_ISSUED_MOUNT_TARGETS = (BOOLEY_DIR_TARGET, PROJECT_DATA_MOUNT_PATH, DEVCONTAINER_TARGET)


def _issued_mount_points() -> bool:
    """Return whether any mount point is, or lies under, an issued-Sandbox target.

    An unreadable mountinfo counts as issued: the only consequence of a false
    "issued" verdict is a refusal.
    """
    try:
        lines = _MOUNTINFO.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return True
    for line in lines:
        fields = line.split()
        if len(fields) < 5:
            continue
        # Field 5 is the mount point; spaces in it are escaped as ``\040``.
        mount_point = PurePosixPath(fields[4].replace("\\040", " "))
        if any(mount_point.is_relative_to(target) for target in _ISSUED_MOUNT_TARGETS):
            return True
    return False


def runtime_location() -> RuntimeLocation:
    """Classify the runtime as host, hand-written Sandbox or Booley-issued Sandbox.

    Not cached, so long-lived processes see the current mounts. No environment or
    platform variable takes part in the issued verdict.
    """
    if not inside_session_runtime():
        return RuntimeLocation.HOST
    if _issued_mount_points():
        return RuntimeLocation.ISSUED_SANDBOX
    return RuntimeLocation.HAND_WRITTEN_SANDBOX


def booley_init_hint(*, code: bool = False) -> str:
    """Name the `booley init` command together with where to run it.

    The host says only the command; a hand-written Sandbox runs it in place; a
    Booley-issued Sandbox refuses it, so the hint sends the user to the host.
    ``code=True`` wraps the command in backticks for prose.
    """
    command = "`booley init`" if code else "booley init"
    location = runtime_location()
    if location is RuntimeLocation.HAND_WRITTEN_SANDBOX:
        return f"{command} in this Sandbox"
    if location is RuntimeLocation.ISSUED_SANDBOX:
        return f"{command} on the host"
    return command


_PROXY_URL = "http://booley-proxy:8080"
_PROXY_ENV_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")


class EgressModeError(ValueError):
    """``BOOLEY_EGRESS`` names no Sandbox egress mode."""


def egress_mode() -> str:
    """Return the Sandbox egress mode declared by ``BOOLEY_EGRESS``.

    Unset means ``proxy``. The variable is a declaration, not the isolation
    boundary: it only decides whether Booley forces its proxy environment.
    """
    declared = os.environ.get("BOOLEY_EGRESS")
    if declared is None:
        return "proxy"
    if declared not in EGRESS_MODES:
        raise EgressModeError(
            f"BOOLEY_EGRESS={declared!r} is not a Sandbox egress mode; set it to "
            "`proxy` (the default when unset) or `unproxied`"
        )
    return declared


def ensure_proxy_env() -> bool:
    """Restore the fixed proxy environment for bare ``docker exec`` calls.

    Raises :class:`EgressModeError` for an invalid ``BOOLEY_EGRESS``; an
    ``unproxied`` Sandbox keeps whatever proxy environment it was given.
    """
    if os.environ.get("BOOLEY_CONTAINER") != "1":
        return False
    if egress_mode() == "unproxied":
        return False
    if any(os.environ.get(var) for var in _PROXY_ENV_VARS):
        return False
    for var in _PROXY_ENV_VARS:
        os.environ[var] = _PROXY_URL
    os.environ.setdefault("NO_PROXY", "localhost,127.0.0.1")
    return True


def agent_session_app() -> str | None:
    """Return the agent CLI whose shell spawned this process, if known."""
    for app, markers in _AGENT_SESSION_MARKERS:
        if any(os.environ.get(marker) for marker in markers):
            return app
    return None


_AGENT_SESSION_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("claude", ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")),
    ("codex", ("CODEX_SANDBOX", "CODEX_HOME")),
)


def container_only_error(what: str) -> str | None:
    """Return an actionable refusal when *what* runs outside the runtime."""
    if inside_session_runtime():
        return None
    return (
        f"ERROR: `{what}` runs inside the Booley Sandbox "
        f"(the project's devcontainer), not on the host.\n\n"
        f'  Open the project in VS Code and accept "Reopen in Container", '
        f"then run this\n"
        f"  command in the integrated terminal. Or enter the Sandbox "
        f"from the host:\n"
        f"      booley session enter -- {what}\n\n"
        f"  See `booley session enter --help` for Sandbox entry options, "
        f"`booley --help` for public commands,\n"
        f"  and `booley cheat --skills` for guided agent workflows."
    )


def host_only_error(what: str) -> str | None:
    """Return an actionable refusal when host administration runs in-container."""
    if not inside_session_runtime():
        return None
    return (
        f"ERROR: `{what}` is a host-side command and cannot run inside the "
        f"Booley container.\n\n"
        f"  Run it from a HOST terminal (outside the devcontainer).\n"
        f"  See `booley --help` for public commands and "
        f"`booley cheat --skills` for guided agent workflows."
    )

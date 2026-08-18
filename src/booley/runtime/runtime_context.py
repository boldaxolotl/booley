"""Session Runtime detection and entry-point guards.

Booley workflows execute inside the per-project Session Runtime.  This
stdlib-only module sits at the bottom of the import graph so every entry point
can detect that runtime and restore its fixed egress proxy environment.
"""

from __future__ import annotations

import os
from pathlib import Path


def inside_session_runtime() -> bool:
    """Return whether this process runs inside a Booley Session Runtime."""
    if os.environ.get("BOOLEY_CONTAINER") == "1":
        return True
    return Path("/.dockerenv").exists() or Path("/run/.containerenv").exists()


_PROXY_URL = "http://booley-proxy:8080"
_PROXY_ENV_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")


def ensure_proxy_env() -> bool:
    """Restore the fixed proxy environment for bare ``docker exec`` calls."""
    if os.environ.get("BOOLEY_CONTAINER") != "1":
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
        f"ERROR: `{what}` runs inside the Booley Session Runtime "
        f"(the project's devcontainer), not on the host.\n\n"
        f'  Open the project in VS Code and accept "Reopen in Container", '
        f"then run this\n"
        f"  command in the integrated terminal. Or exec into the running "
        f"container:\n"
        f"      docker exec -it <container> {what}\n\n"
        f"  Only `booley init` and Session Runtime administration run on the "
        f"host (ADR 0049)."
    )


def host_only_error(what: str) -> str | None:
    """Return an actionable refusal when host administration runs in-container."""
    if not inside_session_runtime():
        return None
    return (
        f"ERROR: `{what}` is a host-side command and cannot run inside the "
        f"Booley container.\n\n"
        f"  Run it from a HOST terminal (outside the devcontainer). Everything "
        f"else —\n"
        f"  `booley run`, `booley board`, Flows, MCP tools, and tickets — belongs "
        f"inside the container\n"
        f"  (ADR 0049)."
    )

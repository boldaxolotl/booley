"""Per-call facts the MCP server resolves once for each tool call.

A tool call runs against one checkout and records its jobs under one root.
Before this module the server answered each of those questions with its own
read of ``arguments`` or ``os.environ`` at the point of use. :func:`resolve_call_context` is now the one place that decides them, so a
later resolver can retarget a whole call (jobs, logs, and the endpoint
subprocess environment) without touching every reader.

Ordinary calls use the explicit ``work_dir`` or the server's cwd, the
container-wide jobs root, and the server process's own ``BOOLEY_*`` variables.
An omitted ``work_dir`` may already have been filled at the SDK boundary from
the calling agent's linked worktree. ``subprocess_env_overrides`` is empty.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from booley.core.checkout_role import SourceCheckoutProjectError
from booley.runtime.booley_dir import resolve_booley_dir
from booley.runtime.session_paths import session_jobs_dir


@dataclass(frozen=True, slots=True)
class CallContext:
    """Facts one MCP tool call runs under, resolved when the call arrives.

    ``explicit_work_dir`` is the call's ``work_dir`` argument, or ``None`` when
    omitted; :attr:`work_dir` applies the cwd default only when read, so a call
    that never needs its checkout never touches the cwd. ``jobs_root``,
    ``logs_dir``, and ``runtime_dir`` are ``None`` when the
    server environment leaves them unset, exactly as the reads they replaced
    returned nothing. ``runtime_dir`` is the explicit ``BOOLEY_RUNTIME_DIR``
    value, not the logs-derived fallback.
    """

    explicit_work_dir: Path | None
    jobs_root: Path | None
    logs_dir: Path | None
    runtime_dir: Path | None
    subprocess_env_overrides: Mapping[str, str]
    session_key: str | None = None

    @property
    def work_dir(self) -> Path:
        """Checkout the call targets: the explicit ``work_dir``, else the cwd now.

        An omitted ``work_dir`` may already have been filled at the SDK boundary
        from the caller's checkout, in which case it is explicit here.
        """
        if self.explicit_work_dir is not None:
            return self.explicit_work_dir
        return Path.cwd()


def container_jobs_root() -> Path | None:
    """Container-wide job-record root configured in the server environment."""
    return session_jobs_dir()


def _explicit_work_dir(arguments: Mapping[str, Any]) -> Path | None:
    """The call's ``work_dir`` argument as a path, or ``None`` when omitted or empty."""
    work_dir = arguments.get("work_dir")
    return Path(str(work_dir)) if work_dir else None


def resolve_work_dir(arguments: Mapping[str, Any]) -> Path:
    """Checkout a call targets: its ``work_dir`` argument, else the server cwd.

    An omitted ``work_dir`` may already have been filled at the SDK boundary
    from the caller's checkout; the cwd default applies only when it was not.

    Validation stays with the caller (``_validate_work_dir``); this only
    applies the default.
    """
    explicit = _explicit_work_dir(arguments)
    return explicit if explicit is not None else Path.cwd()


def _env_path(name: str) -> Path | None:
    """Path held by environment variable *name*, or ``None`` when unset or empty."""
    value = os.environ.get(name)
    return Path(value) if value else None


def resolve_call_context(arguments: Mapping[str, Any]) -> CallContext:
    """Resolve the facts one tool call with *arguments* runs under."""
    return CallContext(
        explicit_work_dir=_explicit_work_dir(arguments),
        jobs_root=container_jobs_root(),
        logs_dir=_env_path("BOOLEY_LOGS_DIR"),
        runtime_dir=_env_path("BOOLEY_RUNTIME_DIR"),
        subprocess_env_overrides=MappingProxyType({}),
    )


def project_booley_dir() -> Path | None:
    """Resolve the session's Booley Directory, or ``None`` for calls without a Project."""
    try:
        return resolve_booley_dir()
    except (FileNotFoundError, SourceCheckoutProjectError):
        return None

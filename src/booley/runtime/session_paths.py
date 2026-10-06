"""Where an execution session keeps its runtime files and durable Job records.

A session's human-facing log root holds a ``.runtime/`` directory for
machine-owned state, and Job records live in ``<runtime root>/jobs``. Interactive
Mode, Ticket Mode, and Goal Mode all share that layout; they differ only in
which root they pass. :func:`session_jobs_dir` with no argument reads the root
the execution caller configured in this process's environment, and with an
explicit *root* it names that root's Job records directly, so a caller that
resolved its own runtime root never has to export it first.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import overload

RUNTIME_DIR = ".runtime"
JOBS_DIR = "jobs"


def logs_runtime_dir(logs_dir: str | Path) -> Path:
    """Return the runtime directory beneath one human-facing log root."""
    return Path(logs_dir) / RUNTIME_DIR


@overload
def session_jobs_dir(root: Path) -> Path: ...


@overload
def session_jobs_dir(root: None = None) -> Path | None: ...


def session_jobs_dir(root: Path | None = None) -> Path | None:
    """Return the Job record directory for an explicit runtime *root* or this session.

    With *root*, return ``root/jobs``. Without it, resolve the root the
    execution caller configured: ``BOOLEY_RUNTIME_DIR`` when set, otherwise
    ``$BOOLEY_LOGS_DIR/.runtime``. Return ``None`` when ``BOOLEY_LOGS_DIR`` is
    unset, which disables Job persistence for that caller.
    """
    if root is not None:
        return root / JOBS_DIR
    logs_dir = os.environ.get("BOOLEY_LOGS_DIR", "")
    if not logs_dir:
        return None
    runtime_env = os.environ.get("BOOLEY_RUNTIME_DIR", "")
    runtime = Path(runtime_env) if runtime_env else logs_runtime_dir(logs_dir)
    return runtime / JOBS_DIR

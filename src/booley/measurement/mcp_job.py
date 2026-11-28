"""The files an MCP ``measure`` or ``review`` Job shares with the MCP server (#4134, #5599).

The ``booley measure`` or ``booley review`` process an MCP Job runs is that Job: it writes no
Standalone Job record. Beside the Job's ``job.log``, in ``<jobs root>/<run_id>/``, it keeps
``progress.json`` (units finished of total and the units running) and, for ``measure`` only,
just before it exits, ``result.json`` (its exit code and the summary the Developer Agent
reads). The server renders both, so a poll, and a server that lost the process, still find
them. A ``review`` Job publishes progress only: its Review Run is stored by the process.
"""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
from typing import Any

from booley.core.atomic_files import atomic_replace_bytes
from booley.flows.cli_selection import INVOCATION_ORIGIN_ENV
from booley.runtime import job_records
from booley.runtime.session_paths import session_jobs_dir

MEASURE_ENDPOINT = "measure"
REVIEW_ENDPOINT = "review"
# What each command Job counts in its progress.
_UNITS = {MEASURE_ENDPOINT: "Flows", REVIEW_ENDPOINT: "reviews"}
COMMAND_ENDPOINTS = frozenset(_UNITS)
# A command Job has no budget, like a Standalone run of the command.
COMMAND_JOB_TIMEOUT_S = 7 * 24 * 3600
_RUN_ID_ENV = "BOOLEY_RUN_ID"
PROGRESS_NAME = "progress.json"
RESULT_NAME = "result.json"


def _read(job_dir: Path, name: str) -> dict[str, Any] | None:
    """The JSON object in *job_dir*/*name*; missing or unparseable is None."""
    try:
        data: object = json.loads((job_dir / name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return {str(key): value for key, value in data.items()} if isinstance(data, dict) else None


def read_progress(job_dir: Path) -> dict[str, Any] | None:
    """The progress the Job last wrote: ``finished``, ``total`` and ``running``."""
    return _read(job_dir, PROGRESS_NAME)


def read_result(job_dir: Path) -> dict[str, Any] | None:
    """The result the Job wrote when it ended: ``run_id``, ``exit_code`` and ``summary``."""
    return _read(job_dir, RESULT_NAME)


def progress_text(endpoint: str, progress: dict[str, Any]) -> str:
    """``2 of 5 Flows finished; running: sim_core.sim, soc.lint``."""
    unit = _UNITS[endpoint]
    text = f"{progress.get('finished', 0)} of {progress.get('total', 0)} {unit} finished"
    running = [str(name) for name in progress.get("running") or ()]
    return f"{text}; running: {', '.join(running)}" if running else text


def running_text(endpoint: str, progress: dict[str, Any]) -> str:
    """What a poll of a running command Job adds: units finished of the total, and running."""
    return f"Progress: {progress_text(endpoint, progress)}."


class McpCommandJob:
    """The ``measure`` or ``review`` Job record an MCP server submitted this process for."""

    def __init__(
        self,
        run_id: str,
        job_dir: Path,
        endpoint: str,
        client: str | None = None,
        thread: str | None = None,
    ) -> None:
        self.run_id = run_id
        self._dir = job_dir
        self.endpoint = endpoint
        # The agent that called the tool; a measurement's Flow Jobs record it too.
        self.client = client
        self.thread = thread

    @classmethod
    def find(cls, endpoint: str) -> McpCommandJob | None:
        """The Job this process runs as, or None when it is not an MCP *endpoint* Job."""
        run_id = os.environ.get(_RUN_ID_ENV)
        root = session_jobs_dir()
        if os.environ.get(INVOCATION_ORIGIN_ENV) != "transport" or not run_id or root is None:
            return None
        with contextlib.suppress(OSError):
            record = job_records.read_record(run_id, root)
            if record is not None and record.endpoint == endpoint:
                return cls(run_id, root / run_id, endpoint, record.client, record.thread)
        return None

    def _write(self, name: str, content: dict[str, Any]) -> None:
        with contextlib.suppress(OSError):  # best effort: the Job log keeps the full report
            self._dir.mkdir(parents=True, exist_ok=True)
            atomic_replace_bytes(self._dir / name, json.dumps(content).encode("utf-8"))

    def record_progress(self, finished: int, total: int, running: tuple[str, ...]) -> None:
        """Record that *finished* of *total* units have ended and *running* are running."""
        self._write(
            PROGRESS_NAME, {"finished": finished, "total": total, "running": list(running)}
        )

    def publish_result(self, exit_code: int, summary: str) -> None:
        """Record the outcome before the process exits, for a server that lost it."""
        self._write(
            RESULT_NAME,
            {
                "run_id": self.run_id,
                "endpoint": self.endpoint,
                "exit_code": exit_code,
                "summary": summary,
            },
        )

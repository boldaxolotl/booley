"""The files an MCP ``measure`` Job shares between its process and the MCP server (#4134).

The ``booley measure`` process an MCP Job runs is that Job: it writes no Standalone Job
record and takes the Job's run id for its Measurement Run. Beside the Job's ``job.log``, in
``<jobs root>/<run_id>/``, it keeps ``progress.json`` (Flows finished of total and the Flows
running) and, just before it exits, ``result.json`` (its exit code and the summary the
Developer Agent reads). The server renders both, so a poll, and a server that lost the
process, still find them.
"""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
from typing import Any

from booley.flows.cli_selection import INVOCATION_ORIGIN_ENV
from booley.runtime import job_records
from booley.runtime.atomic_files import atomic_replace_bytes
from booley.runtime.session_paths import session_jobs_dir

MEASURE_ENDPOINT = "measure"
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


def progress_text(progress: dict[str, Any]) -> str:
    """``2 of 5 Flows finished; running: sim_core.sim, soc.lint``."""
    text = f"{progress.get('finished', 0)} of {progress.get('total', 0)} Flows finished"
    running = [str(name) for name in progress.get("running") or ()]
    return f"{text}; running: {', '.join(running)}" if running else text


class McpMeasureJob:
    """The ``measure`` Job record an MCP server submitted this process for."""

    def __init__(self, run_id: str, job_dir: Path, session_key: str | None = None) -> None:
        self.run_id = run_id
        self._dir = job_dir
        # The session that called ``booley_measure``; the measurement's Flow Jobs are its too.
        self.session_key = session_key

    @classmethod
    def find(cls) -> McpMeasureJob | None:
        """The Job this process runs as, or None when it is not an MCP ``measure`` Job."""
        run_id = os.environ.get(_RUN_ID_ENV)
        root = session_jobs_dir()
        if os.environ.get(INVOCATION_ORIGIN_ENV) != "transport" or not run_id or root is None:
            return None
        with contextlib.suppress(OSError):
            record = job_records.read_record(run_id, root)
            if record is not None and record.endpoint == MEASURE_ENDPOINT:
                return cls(run_id, root / run_id, record.session_key)
        return None

    def _write(self, name: str, content: dict[str, Any]) -> None:
        with contextlib.suppress(OSError):  # best effort: the Job log keeps the full report
            self._dir.mkdir(parents=True, exist_ok=True)
            atomic_replace_bytes(self._dir / name, json.dumps(content).encode("utf-8"))

    def record_progress(self, finished: int, total: int, running: tuple[str, ...]) -> None:
        """Record that *finished* of *total* Flows have ended and *running* are running."""
        self._write(
            PROGRESS_NAME, {"finished": finished, "total": total, "running": list(running)}
        )

    def publish_result(self, exit_code: int, summary: str) -> None:
        """Record the outcome before the process exits, for a server that lost it."""
        self._write(
            RESULT_NAME,
            {
                "run_id": self.run_id,
                "endpoint": MEASURE_ENDPOINT,
                "exit_code": exit_code,
                "summary": summary,
            },
        )

"""Shared process harness for Runtime execution integration tests."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from booley.runtime.execution_records import RUNTIME_EXECUTION_ENV, ExecutionId

_SRC_ROOT = Path(__file__).parents[1] / "src"
_WAIT_SECONDS = 5.0
_T = TypeVar("_T")


def runtime_env(project_dir: Path, execution_id: ExecutionId | None = None) -> dict[str, str]:
    """Return a source-checkout subprocess environment for one Project."""
    python_path = os.environ.get("PYTHONPATH", "")
    source_path = str(_SRC_ROOT)
    if python_path:
        source_path = f"{source_path}{os.pathsep}{python_path}"
    env = {
        **os.environ,
        "BOOLEY_PROJECT_DIR": str(project_dir),
        "PYTHONPATH": source_path,
    }
    if execution_id is not None:
        env[RUNTIME_EXECUTION_ENV] = execution_id
    return env


def wait_for_value(
    probe: Callable[[], _T | None],
    *,
    failure: str,
    timeout_s: float = _WAIT_SECONDS,
) -> _T:
    """Return the first non-None probe result within a bounded interval."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        result = probe()
        if result is not None:
            return result
        time.sleep(0.02)
    raise AssertionError(failure)


def wait_for(
    probe: Callable[[], bool],
    *,
    failure: str,
    timeout_s: float = _WAIT_SECONDS,
) -> None:
    """Wait for a boolean probe within a bounded interval."""
    wait_for_value(
        lambda: True if probe() else None,
        failure=failure,
        timeout_s=timeout_s,
    )


def start_supervisor_with_detached_descendant(
    project_dir: Path,
    execution_id: ExecutionId,
    descendant_pid_file: Path,
) -> subprocess.Popen[str]:
    """Start a marked supervisor whose SIGINT-resistant descendant calls setsid."""
    descendant_script = (
        "import os,signal,time\n"
        "from pathlib import Path\n"
        "signal.signal(signal.SIGINT,signal.SIG_IGN)\n"
        f"pid_file = Path({str(descendant_pid_file)!r})\n"
        "pending = pid_file.with_suffix('.tmp')\n"
        "pending.write_text(str(os.getpid()),encoding='utf-8')\n"
        "pending.replace(pid_file)\n"
        "time.sleep(120)\n"
    )
    leader_script = (
        "import subprocess,sys,time\n"
        f"subprocess.Popen([sys.executable,'-c',{descendant_script!r}],start_new_session=True)\n"
        "time.sleep(120)\n"
    )
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "booley.runtime.execution_supervisor",
            "run",
            "--execution-id",
            execution_id,
            "--grace-seconds",
            "0.1",
            "--attachment-timeout-seconds",
            "60",
            "--",
            sys.executable,
            "-c",
            leader_script,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
        env=runtime_env(project_dir, execution_id),
    )

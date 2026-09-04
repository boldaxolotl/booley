"""Project-scoped Pre-Run Commands execution for Simulation work units."""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path

from booley.flows.sim.config import resolve_pre_run_commands, resolve_run_cwd
from booley.runtime.platform_paths import bash_bin
from booley.runtime.project_dir import resolve_project_dir
from booley.targets.target import TargetHandle

from .contract import PreRunEvidence


def run_pre_run_commands(
    handle: TargetHandle,
    *,
    test_names: tuple[str, ...],
    build_root: Path,
    eda_tool: str,
    timeout_s: int,
    simulator_environment: Mapping[str, str] | None = None,
) -> PreRunEvidence | None:
    """Run the hook once for a native test or once for a Cocotb batch."""
    root = handle.project_root
    commands = tuple(resolve_pre_run_commands(root))
    if not commands:
        return None
    run_cwd = (root / resolve_run_cwd(root)).resolve()
    environment = os.environ.copy()
    environment.update(simulator_environment or {})
    environment.update(
        {
            "BOOLEY_TARGET": handle.selector,
            "BOOLEY_TEST_NAMES": " ".join(test_names),
            "BOOLEY_PROJECT_ROOT": str(root),
            "BOOLEY_RUN_CWD": str(run_cwd),
            "BOOLEY_BUILD_ROOT": str(build_root),
            "BOOLEY_SIM_EDA_TOOL": eda_tool,
        }
    )
    with suppress(FileNotFoundError):
        environment["BOOLEY_PROJECT_DIR"] = str(resolve_project_dir(root))
    if len(test_names) == 1:
        environment["BOOLEY_TEST_NAME"] = test_names[0]
    started = time.monotonic()
    try:
        result = subprocess.run(
            [bash_bin(), "-c", "\n".join(("set -e", *commands))],
            cwd=root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return PreRunEvidence(
            commands,
            "timed_out",
            time.monotonic() - started,
            str(exc),
        )
    except OSError as exc:
        return PreRunEvidence(commands, "spawn_error", time.monotonic() - started, str(exc))
    detail = result.stderr.strip() or result.stdout.strip()
    status = "passed" if result.returncode == 0 else "failed"
    return PreRunEvidence(commands, status, time.monotonic() - started, detail)


__all__ = ["run_pre_run_commands"]

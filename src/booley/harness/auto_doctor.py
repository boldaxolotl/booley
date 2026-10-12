"""Stale-triggered, one-shot automatic Doctor execution.

Sandbox startup launches a detached worker. Shared health owns report storage and
freshness; this adapter owns run serialization, Doctor invocation and progress.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from booley.audit.diagnostic_results import DiagnosticFinding, DiagnosticReport, Severity
from booley.runtime import health
from booley.runtime.project_dir import resolve_project_dir

_RUN_LOCK_NAME = "auto.lock"
Progress = Callable[[str], None]


def _ignore_progress(_message: str) -> None:
    """Accept progress when the caller has no visible sink."""


def _crash_report(exc: Exception, transcript: Path) -> DiagnosticReport:
    """Record a Doctor crash as one machinery failure."""
    finding = DiagnosticFinding(
        Severity.FAIL,
        f"automatic Doctor crashed: {type(exc).__name__}: {exc}",
        f"run `booley doctor` manually; inspect {transcript.name}",
        "automatic.crash",
    )
    return DiagnosticReport((finding,))


def _execute(
    project_root: Path,
    project_dir: Path,
    trigger: str,
    *,
    progress: Progress | None = None,
) -> dict[str, Any]:
    """Execute read-only Doctor and persist its structured result."""
    from booley.harness.doctor import run_doctor_result

    output = io.StringIO()
    started = time.monotonic()
    deep_status = None
    try:
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            result = run_doctor_result(
                argparse.Namespace(verbose=False, deep=False),
                project_root,
                read_only=True,
                record_clean=False,
                progress=progress,
            )
        diagnostics = result.diagnostics
        deep_status = result.deep_status.to_document() if result.deep_status else None
    except Exception as exc:  # noqa: BLE001 — automatic health must record crashes, never break startup
        diagnostics = _crash_report(exc, health.transcript_path(project_dir))
        output.write(f"\n{diagnostics.findings[0].message}\n")
    duration_s = time.monotonic() - started
    report = health.publish_result(
        project_root,
        trigger=trigger,
        duration_s=duration_s,
        diagnostics=diagnostics,
        transcript=output.getvalue(),
        deep_status=deep_status,
    )
    (progress or _ignore_progress)(
        f"completed in {duration_s:.1f}s; transcript: {health.transcript_path(project_dir)}"
    )
    return report


def run_if_due(
    project_root: Path,
    *,
    trigger: str,
    progress: Progress | None = None,
) -> dict[str, Any] | None:
    """Run one automatic Doctor attempt when stale; never raises."""
    try:
        project_dir = resolve_project_dir(project_root)
        lock_path = health.state_dir(project_dir) / _RUN_LOCK_NAME
        with health.try_state_lock(lock_path) as acquired:
            if not acquired:
                return None
            reason = health.due_reason(project_root)
            if reason is None:
                return None
            emit_progress = progress or _ignore_progress
            emit_progress(f"starting ({reason})")
            report = _execute(
                project_root,
                project_dir,
                trigger,
                progress=emit_progress,
            )
        return report
    except Exception:  # noqa: BLE001 — lifecycle advisory must never block a session startup
        return None


def launch(project_root: Path, *, trigger: str = "session-start") -> str:
    """Launch a detached automatic Doctor worker when due."""
    if health.due_reason(project_root) is None:
        return "current"
    cmd = [
        sys.executable,
        "-m",
        "booley.harness.auto_doctor",
        "--project-root",
        str(project_root),
        "--trigger",
        trigger,
    ]
    try:
        subprocess.Popen(
            cmd,
            cwd=project_root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        return "failed"
    return "started"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--trigger", default="session-start")
    args = parser.parse_args()
    run_if_due(args.project_root.resolve(), trigger=args.trigger)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

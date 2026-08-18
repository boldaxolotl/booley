"""Stale-triggered, one-shot automatic Doctor runs.

Automatic health checks are lifecycle work, not a scheduler: Session Runtime
startup launches one detached attempt, and ``booley run`` performs the same
check synchronously as a fallback. A file lock makes those entry points
single-flight. Every attempt writes a structured report and transcript; the
separate clean stamp remains Doctor's durable "last blessed" record.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
import time
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from booley.harness import doctor_stamp
from booley.runtime.project_dir import resolve_project_dir
from booley.runtime.timefmt import MACHINE_TIMESTAMP_FORMAT, parse_timestamp, utc_now_rfc3339

if TYPE_CHECKING:
    from booley.harness.doctor import DoctorRunResult

_STATE_DIR = Path("runtime") / "doctor"
_REPORT_NAME = "last.json"
_TRANSCRIPT_NAME = "last.log"
_ANNOUNCEMENTS_NAME = "announcements.json"
_RUN_LOCK_NAME = "auto.lock"
_ANNOUNCE_LOCK_NAME = "announcements.lock"
_TIME_FORMAT = MACHINE_TIMESTAMP_FORMAT  # Compatibility for report fixtures.
_HEALTHY_MAX_AGE = timedelta(days=doctor_stamp.MAX_AGE_DAYS)
_UNHEALTHY_RETRY_AGE = timedelta(days=1)


def state_dir(project_dir: Path) -> Path:
    """Return the automatic Doctor runtime directory."""
    return project_dir / _STATE_DIR


def report_path(project_dir: Path) -> Path:
    """Return the latest structured automatic Doctor report path."""
    return state_dir(project_dir) / _REPORT_NAME


def transcript_path(project_dir: Path) -> Path:
    """Return the latest automatic Doctor console transcript path."""
    return state_dir(project_dir) / _TRANSCRIPT_NAME


def _sha256_or_none(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _path_identity(path: Path) -> str | None:
    """Fingerprint a generated link without erasing link-vs-copy drift."""
    try:
        if path.is_symlink():
            return f"symlink:{path.readlink()}"
        if path.is_file():
            return f"file:{_sha256_or_none(path)}"
    except OSError:
        return None
    return None


def _core_digest(project_root: Path, project_dir: Path) -> str:
    """Hash authored core descriptions without scanning runtime worktrees."""
    roots = [project_root, project_dir / "cores"]
    entries: list[tuple[str, Path]] = []
    for root in roots:
        if not root.is_dir():
            continue
        for path in root.rglob("*.core"):
            if root == project_root and path.is_relative_to(project_dir):
                continue
            try:
                label = str(path.relative_to(root))
            except ValueError:
                label = str(path)
            entries.append((f"{root.name}/{label}", path))
    digest = hashlib.sha256()
    for label, path in sorted(entries):
        digest.update(label.encode())
        value = _sha256_or_none(path) or "unreadable"
        digest.update(value.encode())
    return digest.hexdigest()


def compute_fingerprint(project_dir: Path, project_root: Path) -> dict[str, Any]:
    """Hash config and authored design-description inputs relevant to Doctor."""
    import booley

    fingerprint: dict[str, Any] = doctor_stamp.compute_fingerprint(project_dir, project_root)
    for name in ("tests.toml", "configs.toml", "AGENTS.md"):
        fingerprint[f"{name}_sha256"] = _sha256_or_none(project_dir / name)
    for name in ("AGENTS.md", "CLAUDE.md"):
        fingerprint[f"root_{name}_identity"] = _path_identity(project_root / name)
    fingerprint["core_files_sha256"] = _core_digest(project_root, project_dir)
    fingerprint["booley_version"] = booley.__version__
    return fingerprint


def load_report(project_root: Path) -> dict[str, Any] | None:
    """Load the latest automatic Doctor report, or ``None`` when unavailable."""
    try:
        project_dir = resolve_project_dir(project_root)
        data = json.loads(report_path(project_dir).read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return parse_timestamp(value)
    except ValueError:
        return None


def due_reason(project_root: Path, *, now: datetime | None = None) -> str | None:
    """Return why an automatic check is due, or ``None`` when current."""
    try:
        project_dir = resolve_project_dir(project_root)
    except (FileNotFoundError, OSError):
        return None
    report = load_report(project_root)
    if report is None:
        return "no automatic Doctor result"
    return _existing_report_due_reason(
        report,
        compute_fingerprint(project_dir, project_root),
        now or datetime.now(tz=UTC),
    )


def _existing_report_due_reason(
    report: dict[str, Any],
    fingerprint: dict[str, Any],
    now: datetime,
) -> str | None:
    """Evaluate age and input drift for a parsed prior report."""
    if report.get("fingerprint") != fingerprint:
        return "Doctor inputs changed"
    checked_at = _parse_time(report.get("checked_at"))
    if checked_at is None:
        return "automatic Doctor timestamp unreadable"
    clean = bool(report.get("clean"))
    max_age = _HEALTHY_MAX_AGE if clean else _UNHEALTHY_RETRY_AGE
    if now - checked_at >= max_age:
        return "automatic Doctor result expired"
    return None


@contextlib.contextmanager
def _try_lock(path: Path):
    """Yield whether a nonblocking process lock was acquired."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        try:
            if sys.platform == "win32":
                import msvcrt

                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write("\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            yield False
            return
        try:
            yield True
        finally:
            if sys.platform == "win32":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def _finding_hash(report: dict[str, Any]) -> str:
    findings = [
        finding
        for finding in report.get("findings", [])
        if isinstance(finding, dict) and finding.get("severity") in {"fail", "warn"}
    ]
    value: object = findings or "clean"
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _exception_report(exc: Exception) -> tuple[dict[str, int], list[dict[str, Any]], int]:
    counts = {"pass": 0, "fail": 1, "warn": 0, "waived": 0, "note": 0, "skip": 0}
    finding = {
        "severity": "fail",
        "message": f"automatic Doctor crashed: {type(exc).__name__}: {exc}",
        "fix": f"run `booley doctor` manually; inspect {_TRANSCRIPT_NAME}",
        "check_id": "automatic.crash",
        "subject": None,
    }
    return counts, [finding], 1


def _execute(project_root: Path, project_dir: Path, trigger: str) -> dict[str, Any]:
    """Execute read-only Doctor and persist its structured result."""
    from booley.harness.doctor import run_doctor_result

    output = io.StringIO()
    started = time.monotonic()
    try:
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            result = run_doctor_result(
                argparse.Namespace(verbose=False, deep=False),
                project_root,
                read_only=True,
                record_clean=False,
            )
        counts = result.counts
        findings = [asdict(finding) for finding in result.findings]
        exit_code = result.exit_code
    except Exception as exc:  # noqa: BLE001 — automatic health must record crashes, never break startup
        counts, findings, exit_code = _exception_report(exc)
        output.write(f"\n{findings[0]['message']}\n")
    return _persist_report(
        project_root,
        project_dir,
        trigger=trigger,
        duration_s=time.monotonic() - started,
        counts=counts,
        findings=findings,
        exit_code=exit_code,
        transcript=output.getvalue(),
    )


def _persist_report(
    project_root: Path,
    project_dir: Path,
    *,
    trigger: str,
    duration_s: float,
    counts: dict[str, int],
    findings: list[dict[str, Any]],
    exit_code: int,
    transcript: str,
) -> dict[str, Any]:
    """Atomically persist one already-completed structured Doctor result."""
    from booley.runtime import runtime_context

    payload = {
        "schema": 1,
        "checked_at": utc_now_rfc3339(),
        "duration_s": round(duration_s, 3),
        "trigger": trigger,
        "runtime": "session-runtime" if runtime_context.inside_session_runtime() else "host",
        "fingerprint": compute_fingerprint(project_dir, project_root),
        "counts": counts,
        "findings": findings,
        "exit_code": exit_code,
        "clean": counts["fail"] == 0 and counts["warn"] == 0,
    }
    payload["finding_hash"] = _finding_hash(payload)
    _atomic_write(transcript_path(project_dir), transcript)
    _atomic_write(report_path(project_dir), json.dumps(payload, indent=2) + "\n")
    return payload


def record_manual_result(project_root: Path, result: DoctorRunResult) -> dict[str, Any] | None:
    """Make a manual in-runtime Doctor result current for interactive reporting."""
    try:
        project_dir = resolve_project_dir(project_root)
        findings = [asdict(finding) for finding in result.findings]
        transcript = "\n".join(
            f"{finding['severity'].upper()}: {finding['message']}" for finding in findings
        )
        return _persist_report(
            project_root,
            project_dir,
            trigger="manual-doctor",
            duration_s=0,
            counts=dict(result.counts),
            findings=findings,
            exit_code=int(result.exit_code),
            transcript=transcript + "\n",
        )
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def run_if_due(project_root: Path, *, trigger: str) -> dict[str, Any] | None:
    """Run one automatic Doctor attempt when stale; never raises."""
    try:
        project_dir = resolve_project_dir(project_root)
        lock_path = state_dir(project_dir) / _RUN_LOCK_NAME
        with _try_lock(lock_path) as acquired:
            if not acquired or due_reason(project_root) is None:
                return None
            report = _execute(project_root, project_dir, trigger)
        _notify_if_changed(project_root, report)
        return report
    except Exception:  # noqa: BLE001 — lifecycle advisory must never block a session or ticket sweep
        return None


def launch(project_root: Path, *, trigger: str = "session-start") -> str:
    """Launch a detached automatic Doctor worker when due."""
    if due_reason(project_root) is None:
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


def _brief_message(report: dict[str, Any], project_root: Path) -> str:
    fail_count, warn_count = issue_counts(report)
    try:
        project_dir = resolve_project_dir(project_root)
        detail = transcript_path(project_dir)
    except (FileNotFoundError, OSError):
        detail = Path(_TRANSCRIPT_NAME)
    if fail_count == 0 and warn_count == 0:
        return f"Automatic Doctor is clean. Full report: {detail}"
    findings = [
        str(item.get("message"))
        for item in report.get("findings", [])
        if isinstance(item, dict) and item.get("severity") in {"fail", "warn"}
    ]
    top = "; ".join(findings[:3])
    extra = f"; {len(findings) - 3} more" if len(findings) > 3 else ""
    return (
        f"Automatic Doctor found {fail_count} FAIL, {warn_count} WARN: "
        f"{top}{extra}. Full report: {detail}"
    )


def issue_counts(report: dict[str, Any]) -> tuple[int, int]:
    """Return validated active FAIL/WARN counts from a persisted report."""
    counts = report.get("counts")
    if not isinstance(counts, dict):
        return 0, 0

    def count(name: str) -> int:
        value = counts.get(name)
        return (
            value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0
        )

    return count("fail"), count("warn")


def current_summary(project_root: Path) -> str:
    """Return the latest automatic-health status without consuming an alert."""
    report = load_report(project_root)
    if report is None:
        return "Automatic Doctor has not completed a run yet."
    return _brief_message(report, project_root)


def _load_announcements(path: Path) -> dict[str, str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    return {str(key): str(value) for key, value in data.items()} if isinstance(data, dict) else {}


def consume_changed_summary(
    project_root: Path,
    *,
    channel: str,
    issues_only: bool = False,
) -> str | None:
    """Return a changed summary once per reporting channel."""
    report = load_report(project_root)
    if report is None:
        return None
    fail_count, warn_count = issue_counts(report)
    has_issues = bool(fail_count or warn_count)
    if issues_only and not has_issues:
        return None
    try:
        project_dir = resolve_project_dir(project_root)
        directory = state_dir(project_dir)
        announcements_path = directory / _ANNOUNCEMENTS_NAME
        with _try_lock(directory / _ANNOUNCE_LOCK_NAME) as acquired:
            if not acquired:
                return None
            announcements = _load_announcements(announcements_path)
            finding_hash = str(report.get("finding_hash") or _finding_hash(report))
            if announcements.get(channel) == finding_hash:
                return None
            announcements[channel] = finding_hash
            _atomic_write(announcements_path, json.dumps(announcements, indent=2) + "\n")
    except (FileNotFoundError, OSError):
        return None
    return _brief_message(report, project_root)


def _notify_if_changed(project_root: Path, report: dict[str, Any]) -> None:
    if not any(issue_counts(report)):
        return
    try:
        from booley.ticket_board.notifications import is_event_enabled, ntfy_send

        if not is_event_enabled("doctor"):
            return
    except (ImportError, OSError):
        return
    summary = consume_changed_summary(project_root, channel="ntfy", issues_only=True)
    if summary is not None:
        ntfy_send("Booley Doctor found issues", summary, priority="4")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--trigger", default="session-start")
    args = parser.parse_args()
    run_if_due(args.project_root.resolve(), trigger=args.trigger)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

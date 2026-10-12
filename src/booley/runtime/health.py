"""Shared Doctor observations, freshness, publication and announcement state.

This capability reads and stores diagnostics; it never executes Doctor. Consumers
render a single typed HealthSnapshot without acquiring command/SDK dependencies.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from booley.audit import health as policy
from booley.audit.diagnostic_results import DiagnosticReport
from booley.audit.health import HealthSnapshot
from booley.runtime import doctor_stamp
from booley.runtime.file_lock import try_file_lock
from booley.runtime.project_dir import contains, resolve_project_dir
from booley.runtime.timefmt import parse_timestamp, utc_now_rfc3339

_STATE_DIR = Path("runtime") / "doctor"
_REPORT_NAME = "last.json"
_TRANSCRIPT_NAME = "last.log"
_ANNOUNCEMENTS_NAME = "announcements.json"
_ANNOUNCE_LOCK_NAME = "announcements.lock"


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
            if root == project_root and contains(path.parent, project_dir=project_dir) is not None:
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
    fingerprint["upgrade_review_sha256"] = _sha256_or_none(
        project_dir / "runtime" / "upgrade_review.json"
    )
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


def read_health(project_root: Path, *, now: datetime | None = None) -> HealthSnapshot | None:
    """Read one report, validate its health interpretation, then assess its freshness."""
    try:
        project_dir = resolve_project_dir(project_root)
        document = load_report(project_root)
        if document is None:
            return None
        diagnostics = policy.parse_diagnostics(document)
        due = policy.freshness_reason(
            document,
            compute_fingerprint(project_dir, project_root),
            now or datetime.now(tz=UTC),
            assessment=diagnostics.health,
            checked_at=_parse_time(document.get("checked_at")),
        )
        return HealthSnapshot(diagnostics, document, transcript_path(project_dir), due)
    except (FileNotFoundError, OSError, ValueError, TypeError):
        return None


def due_reason(project_root: Path, *, now: datetime | None = None) -> str | None:
    """Return why an automatic check is due, or None outside a Project/when current."""
    try:
        resolve_project_dir(project_root)
    except (FileNotFoundError, OSError):
        return None
    snapshot = read_health(project_root, now=now)
    return snapshot.due if snapshot is not None else "no automatic Doctor result"


@contextlib.contextmanager
def report_lock(path: Path) -> Iterator[bool]:
    """Yield whether a nonblocking process lock was acquired."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle, try_file_lock(handle) as acquired:
        yield acquired


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


def publish_result(
    project_root: Path,
    project_dir: Path,
    *,
    trigger: str,
    duration_s: float,
    counts: dict[str, int],
    diagnostics: DiagnosticReport,
    exit_code: int,
    transcript: str,
    deep_status: dict[str, Any] | None = None,
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
        "findings": policy.finding_documents(diagnostics),
        "exit_code": exit_code,
        "clean": diagnostics.health.clean,
        "deep_status": deep_status,
    }
    payload["finding_hash"] = _finding_hash(payload)
    _atomic_write(transcript_path(project_dir), transcript)
    _atomic_write(report_path(project_dir), json.dumps(payload, indent=2) + "\n")
    return payload


def _load_announcements(path: Path) -> dict[str, str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    return {str(key): str(value) for key, value in data.items()} if isinstance(data, dict) else {}


def consume_changed_snapshot(
    project_root: Path,
    *,
    channel: str,
    issues_only: bool = False,
) -> HealthSnapshot | None:
    """Return a changed summary once per reporting channel."""
    snapshot = read_health(project_root)
    if snapshot is None:
        return None
    report = snapshot.document
    has_issues = not snapshot.assessment.clean
    if issues_only and not has_issues:
        return None
    try:
        project_dir = resolve_project_dir(project_root)
        directory = state_dir(project_dir)
        announcements_path = directory / _ANNOUNCEMENTS_NAME
        with report_lock(directory / _ANNOUNCE_LOCK_NAME) as acquired:
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
    return snapshot

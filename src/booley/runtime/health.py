"""Shared Doctor observations, freshness, publication and announcement state.

This capability reads and stores diagnostics; it never executes Doctor. Consumers
render a single typed HealthSnapshot without acquiring command/SDK dependencies.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
from collections import Counter
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from booley.audit import health as policy
from booley.audit.diagnostic_results import DiagnosticReport, Severity
from booley.audit.health import HealthSnapshot
from booley.core.boundary import as_dict
from booley.runtime import doctor_stamp
from booley.runtime.atomic_files import atomic_replace_bytes
from booley.runtime.booley_dir import (
    contains,
    resolve_common_booley_dir,
    resolve_main_checkout_root,
)
from booley.runtime.file_lock import try_file_lock
from booley.runtime.filesystem_utils import read_file_bytes
from booley.runtime.timefmt import parse_timestamp, rfc3339_from_datetime, utc_now_rfc3339

_STATE_DIR = Path("runtime") / "doctor"
_REPORT_NAME = "last.json"
_TRANSCRIPT_NAME = "last.log"
_ANNOUNCEMENTS_NAME = "announcements.json"
_ANNOUNCE_LOCK_NAME = "announcements.lock"
_INVALIDATION_NAME = "invalidated.json"
_INPUT_LABELS = {
    "booley_toml_sha256": "booley.toml",
    "doctor_waivers_sha256": "doctor-waivers.toml",
    "devcontainer_json_sha256": "devcontainer.json",
    "tests.toml_sha256": "tests.toml",
    "configs.toml_sha256": "configs.toml",
    "AGENTS.md_sha256": "Booley Directory AGENTS.md",
    "root_AGENTS.md_identity": "Project Root AGENTS.md",
    "root_CLAUDE.md_identity": "Project Root CLAUDE.md",
    "core_files_sha256": "authored .core files",
    "booley_version": "Booley version",
    "upgrade_review_sha256": "upgrade review",
}


def state_dir(booley_dir: Path) -> Path:
    """Return the automatic Doctor runtime directory."""
    return booley_dir / _STATE_DIR


def report_path(booley_dir: Path) -> Path:
    """Return the latest structured automatic Doctor report path."""
    return state_dir(booley_dir) / _REPORT_NAME


def transcript_path(booley_dir: Path) -> Path:
    """Return the latest automatic Doctor console transcript path."""
    return state_dir(booley_dir) / _TRANSCRIPT_NAME


def _sha256_or_none(path: Path, *, retry_sharing: bool = False) -> str | None:
    """Hash ``path``; ``retry_sharing`` rides out a concurrent atomic replace.

    Only Booley-written, polled inputs retry: user-authored inputs stay a
    single attempt because an error there just reports drift.
    """
    try:
        raw = read_file_bytes(path) if retry_sharing else path.read_bytes()
        return hashlib.sha256(raw).hexdigest()
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


def _core_digest(project_root: Path, booley_dir: Path) -> str:
    """Hash authored core descriptions without scanning runtime worktrees."""
    roots = [("project", project_root), ("booley-cores", booley_dir / "cores")]
    entries: list[tuple[str, Path]] = []
    for origin, root in roots:
        if not root.is_dir():
            continue
        for path in root.rglob("*.core"):
            if origin == "project" and contains(path.parent, booley_dir=booley_dir) is not None:
                continue
            label = path.relative_to(root).as_posix()
            entries.append((f"{origin}/{label}", path))
    digest = hashlib.sha256()
    for label, path in sorted(entries):
        digest.update(label.encode() + b"\0")
        value = _sha256_or_none(path) or "unreadable"
        digest.update(value.encode() + b"\0")
    return digest.hexdigest()


def compute_fingerprint(booley_dir: Path, project_root: Path) -> dict[str, Any]:
    """Hash config and authored design-description inputs relevant to Doctor.

    Like the stamp, the record is Common Booley State: *booley_dir* is the Common
    Booley Directory and root-level inputs come from the main checkout.
    """
    import booley

    project_root = resolve_main_checkout_root(project_root)

    fingerprint: dict[str, Any] = doctor_stamp.compute_fingerprint(booley_dir, project_root)
    for name in ("tests.toml", "configs.toml", "AGENTS.md"):
        fingerprint[f"{name}_sha256"] = _sha256_or_none(booley_dir / name)
    for name in ("AGENTS.md", "CLAUDE.md"):
        fingerprint[f"root_{name}_identity"] = _path_identity(project_root / name)
    fingerprint["core_files_sha256"] = _core_digest(project_root, booley_dir)
    fingerprint["booley_version"] = booley.__version__
    fingerprint["upgrade_review_sha256"] = _sha256_or_none(
        booley_dir / "runtime" / "upgrade_review.json", retry_sharing=True
    )
    return fingerprint


def load_report(project_root: Path) -> dict[str, Any] | None:
    """Load the latest automatic Doctor report, or ``None`` when unavailable."""
    try:
        booley_dir = resolve_common_booley_dir(project_root)
        data = json.loads(read_file_bytes(report_path(booley_dir)).decode("utf-8"))
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


def _input_change_reason(recorded: object, current: dict[str, Any]) -> str:
    """Name only known inputs from a compatible persisted fingerprint."""
    generic = "Doctor inputs changed"
    recorded = as_dict(recorded)
    if recorded is None or recorded.keys() != current.keys():
        return generic
    names = [label for key, label in _INPUT_LABELS.items() if recorded[key] != current[key]]
    shown = names[:3]
    if len(names) > 3:
        shown.append(f"+{len(names) - 3} more")
    return f"{generic}: {', '.join(shown)}" if shown else generic


def _load_invalidation(booley_dir: Path) -> policy.Invalidation | None:
    """The latest recorded invalidation, or ``None`` when absent or unreadable."""
    try:
        data = json.loads(read_file_bytes(state_dir(booley_dir) / _INVALIDATION_NAME))
    except (OSError, ValueError):
        return None
    data = as_dict(data)
    at = _parse_time(data.get("at")) if data is not None else None
    reason = data.get("reason") if data is not None else None
    if at is None or not isinstance(reason, str) or not reason:
        return None
    return policy.Invalidation(at, reason)


def read_health(project_root: Path, *, now: datetime | None = None) -> HealthSnapshot | None:
    """Read one report, validate its health interpretation, then assess its freshness.

    Missing, unreadable, malformed and self-contradictory reports are all
    unavailable: none of them is evidence of health, and each is due for a recheck.
    """
    try:
        booley_dir = resolve_common_booley_dir(project_root)
        document = load_report(project_root)
        if document is None:
            return None
        diagnostics = policy.parse_diagnostics(document)
        current = compute_fingerprint(booley_dir, project_root)
        due = policy.freshness_reason(
            diagnostics,
            inputs_changed=document.get("fingerprint") != current,
            checked_at=_parse_time(document.get("checked_at")),
            now=now or datetime.now(tz=UTC),
            started_at=_parse_time(document.get("started_at")),
            invalidation=_load_invalidation(booley_dir),
        )
        if due == "Doctor inputs changed":
            due = _input_change_reason(document.get("fingerprint"), current)
    except (OSError, ValueError):
        return None
    finding_hash = str(document.get("finding_hash") or _finding_hash(diagnostics))
    return HealthSnapshot(diagnostics, finding_hash, transcript_path(booley_dir), due)


def due_reason(project_root: Path, *, now: datetime | None = None) -> str | None:
    """Return why an automatic check is due, or None outside a Project/when current."""
    try:
        resolve_common_booley_dir(project_root)
    except (FileNotFoundError, OSError):
        return None
    snapshot = read_health(project_root, now=now)
    return snapshot.due if snapshot is not None else "no automatic Doctor result"


@contextlib.contextmanager
def try_state_lock(path: Path) -> Iterator[bool]:
    """Yield whether a nonblocking process lock on a Doctor state file was acquired."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle, try_file_lock(handle) as acquired:
        yield acquired


def _atomic_write(path: Path, text: str) -> None:
    # World-readable like the files it replaces: host and Sandbox both read them.
    atomic_replace_bytes(path, text.encode("utf-8"), mode=0o644)


def _finding_hash(diagnostics: DiagnosticReport) -> str:
    """Identify the active findings; every clean observation shares one identity."""
    value: object = policy.finding_documents(diagnostics.active_findings) or "clean"
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _severity_counts(diagnostics: DiagnosticReport) -> dict[str, int]:
    """Count every severity, including the ones that did not occur."""
    seen = Counter(finding.severity for finding in diagnostics.findings)
    return {severity.value: seen[severity] for severity in Severity}


def publish_result(
    project_root: Path,
    *,
    trigger: str,
    duration_s: float,
    diagnostics: DiagnosticReport,
    transcript: str,
    deep_status: dict[str, Any] | None = None,
    started_at: str | None = None,
) -> dict[str, Any]:
    """Atomically persist one already-completed structured Doctor result.

    Counts, cleanliness and exit code are derived from the findings, so a
    published report can never contradict itself. *started_at* (RFC 3339) is
    when the Doctor run began; it decides whether a later invalidation covers it.
    """
    from booley.runtime import runtime_context

    booley_dir = resolve_common_booley_dir(project_root)
    assessment = diagnostics.health
    payload = {
        "schema": 1,
        "checked_at": utc_now_rfc3339(),
        "started_at": started_at,
        "duration_s": round(duration_s, 3),
        "trigger": trigger,
        "runtime": "session-runtime" if runtime_context.inside_session_runtime() else "host",
        "fingerprint": compute_fingerprint(booley_dir, project_root),
        "counts": _severity_counts(diagnostics),
        "findings": policy.finding_documents(diagnostics.findings),
        "exit_code": assessment.exit_code,
        "clean": assessment.clean,
        "deep_status": deep_status,
        "finding_hash": _finding_hash(diagnostics),
    }
    _atomic_write(transcript_path(booley_dir), transcript)
    _atomic_write(report_path(booley_dir), json.dumps(payload, indent=2) + "\n")
    return payload


def run_start_stamp() -> str:
    """Stamp a Doctor run's start for ``publish_result(started_at=...)``.

    Microsecond resolution orders a run against an invalidation in the same second.
    """
    return rfc3339_from_datetime(datetime.now(tz=UTC), microseconds=True)


def invalidate(project_root: Path, *, reason: str) -> None:
    """Make every observation from a Doctor run started before now due for a recheck.

    For changes Doctor's fingerprint cannot see, such as the agent sign-in the
    in-Sandbox registrar resolves. A run already in flight is covered too: it
    started before this call, so its late result is still due.
    """
    booley_dir = resolve_common_booley_dir(project_root)
    document = {"at": run_start_stamp(), "reason": reason}
    _atomic_write(state_dir(booley_dir) / _INVALIDATION_NAME, json.dumps(document) + "\n")


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
    """Return a changed, current observation once per reporting channel.

    A stale observation is neither reported nor recorded: its findings may no
    longer hold, and the recheck that is due decides what to announce. An
    ``issues_only`` channel neither reports nor records clean observations.
    """
    snapshot = read_health(project_root)
    if snapshot is None or snapshot.due is not None:
        return None
    if issues_only and snapshot.assessment.clean:
        return None
    try:
        booley_dir = resolve_common_booley_dir(project_root)
        directory = state_dir(booley_dir)
        announcements_path = directory / _ANNOUNCEMENTS_NAME
        with try_state_lock(directory / _ANNOUNCE_LOCK_NAME) as acquired:
            if not acquired:
                return None
            announcements = _load_announcements(announcements_path)
            if announcements.get(channel) == snapshot.finding_hash:
                return None
            announcements[channel] = snapshot.finding_hash
            _atomic_write(announcements_path, json.dumps(announcements, indent=2) + "\n")
    except (FileNotFoundError, OSError):
        return None
    return snapshot

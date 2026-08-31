"""Durable, monotonic review state for Booley version upgrades.

Doctor health and release review are separate facts. This module owns the
representation and every mutation of ``runtime/upgrade_review.json`` so callers
cannot accidentally erase a pending range while refreshing Doctor evidence.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import IO, Any

from booley.core.boundary import (
    BoundaryError,
    as_dict,
    as_str,
    require_dict,
    require_opt_str,
    require_str,
)
from booley.runtime.changelog import ChangelogError, StableVersion, parse_releases
from booley.runtime.file_lock import LockContentionError, acquire_file_lock, release_file_lock
from booley.runtime.paths import changelog_path
from booley.runtime.timefmt import parse_timestamp, utc_now_rfc3339

STATE_SCHEMA = 1
STATE_FILENAME = "upgrade_review.json"
LOCK_FILENAME = "upgrade_review.lock"
_LOCK_TIMEOUT_S = 2.0
_LOCK_RETRY_S = 0.02


class ReviewCondition(StrEnum):
    """A scriptable summary of upgrade-review state."""

    CURRENT = "current"
    PENDING = "pending"
    STALE_RUNTIME = "stale-runtime"
    CORRUPT = "corrupt"
    UNSUPPORTED = "unsupported-version"
    UNAVAILABLE = "unavailable"


class UpgradeReviewError(RuntimeError):
    """Base error for upgrade review operations."""


class CorruptReviewStateError(UpgradeReviewError):
    """Persisted state exists but cannot be trusted."""


class ReviewStorageError(UpgradeReviewError):
    """Review state could not be read, locked, or written."""


class AcknowledgmentError(UpgradeReviewError):
    """Compare-and-swap acknowledgment preconditions were not satisfied."""


@dataclass(frozen=True)
class ReviewState:
    """Validated persisted schema."""

    reviewed_through: str
    pending_target: str | None = None
    first_seen_at: str | None = None

    def payload(self) -> dict[str, object]:
        """Return the canonical JSON representation."""
        payload: dict[str, object] = {
            "schema": STATE_SCHEMA,
            "reviewed_through": self.reviewed_through,
        }
        if self.pending_target is not None:
            payload["pending_target"] = self.pending_target
            payload["first_seen_at"] = self.first_seen_at
        return payload


@dataclass(frozen=True)
class ReviewStatus:
    """Typed result consumed by the CLI, Doctor, and startup hooks."""

    condition: ReviewCondition
    running_version: str
    state_path: str
    reviewed_through: str | None = None
    pending_target: str | None = None
    first_seen_at: str | None = None
    diagnostic: str | None = None

    def as_dict(self) -> dict[str, object]:
        """Return a stable JSON-ready status payload."""
        payload = asdict(self)
        payload["condition"] = self.condition.value
        return payload


def state_path(project_dir: Path) -> Path:
    """Return the project-local upgrade review state path."""
    return project_dir / "runtime" / STATE_FILENAME


def _lock_path(project_dir: Path) -> Path:
    return project_dir / "runtime" / LOCK_FILENAME


def _running_version(value: str | None) -> str:
    if value is not None:
        return value
    import booley

    return booley.__version__


def _parse_version(value: object, field: str) -> StableVersion:
    parsed = as_str(value)
    if parsed is None:
        raise CorruptReviewStateError(f"{field} must be a stable version string")
    try:
        from booley.runtime.changelog import parse_stable_version

        return parse_stable_version(parsed)
    except ChangelogError as exc:
        raise CorruptReviewStateError(f"invalid {field}: {exc}") from exc


def _validate_state(data: object) -> ReviewState:
    try:
        mapping = require_dict(data, field="upgrade review state")
        reviewed_value = require_str(mapping, "reviewed_through")
        pending_value = require_opt_str(mapping, "pending_target")
        first_seen = require_opt_str(mapping, "first_seen_at")
    except BoundaryError as exc:
        raise CorruptReviewStateError(str(exc)) from exc
    if mapping.get("schema") != STATE_SCHEMA:
        raise CorruptReviewStateError(
            f"unsupported upgrade review schema {mapping.get('schema')!r}"
        )
    reviewed = _parse_version(reviewed_value, "reviewed_through")
    if pending_value is None:
        if first_seen is not None:
            raise CorruptReviewStateError("first_seen_at requires pending_target")
        return ReviewState(str(reviewed))
    pending = _parse_version(pending_value, "pending_target")
    if pending <= reviewed:
        raise CorruptReviewStateError("pending_target must be newer than reviewed_through")
    if first_seen is None:
        raise CorruptReviewStateError("pending review requires first_seen_at")
    try:
        parse_timestamp(first_seen)
    except ValueError as exc:
        raise CorruptReviewStateError("first_seen_at is not a valid timestamp") from exc
    return ReviewState(str(reviewed), str(pending), first_seen)


def _read_state(path: Path) -> ReviewState | None:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except UnicodeError as exc:
        raise CorruptReviewStateError(f"cannot decode {path}: {exc}") from exc
    except OSError as exc:
        raise ReviewStorageError(f"cannot read {path}: {exc}") from exc
    try:
        return _validate_state(json.loads(text))
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise CorruptReviewStateError(f"cannot parse {path}: {exc}") from exc


def _doctor_bootstrap_version(project_dir: Path) -> StableVersion | None:
    path = project_dir / "runtime" / "doctor_stamp.json"
    try:
        data = as_dict(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, json.JSONDecodeError, UnicodeError):
        return None
    if data is None:
        return None
    try:
        return _parse_version(data.get("booley_version"), "doctor stamp booley_version")
    except CorruptReviewStateError:
        return None


def _atomic_write(path: Path, state: ReviewState) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        temporary.write_text(json.dumps(state.payload(), indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)
    except OSError as exc:
        with suppress(OSError):
            temporary.unlink(missing_ok=True)
        raise ReviewStorageError(f"cannot write {path}: {exc}") from exc


def _first_seen_at(value: str | None) -> str:
    timestamp = value or utc_now_rfc3339()
    try:
        parse_timestamp(timestamp)
    except ValueError as exc:
        raise ReviewStorageError(f"invalid observation timestamp {timestamp!r}") from exc
    return timestamp


@contextmanager
def _locked(project_dir: Path) -> Iterator[None]:
    path = _lock_path(project_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle: IO[Any] = path.open("a+", encoding="utf-8")
    except OSError as exc:
        raise ReviewStorageError(f"cannot open upgrade review lock {path}: {exc}") from exc
    deadline = time.monotonic() + _LOCK_TIMEOUT_S
    try:
        while True:
            try:
                acquire_file_lock(handle)
                break
            except LockContentionError as exc:
                if time.monotonic() >= deadline:
                    raise ReviewStorageError(
                        f"timed out acquiring upgrade review lock {path}"
                    ) from exc
                time.sleep(_LOCK_RETRY_S)
        try:
            yield
        finally:
            release_file_lock(handle)
    finally:
        handle.close()


def _status(project_dir: Path, running: StableVersion, state: ReviewState) -> ReviewStatus:
    reviewed = _parse_version(state.reviewed_through, "reviewed_through")
    pending = (
        _parse_version(state.pending_target, "pending_target")
        if state.pending_target is not None
        else None
    )
    condition = ReviewCondition.CURRENT
    if running < reviewed or (pending is not None and running < pending):
        condition = ReviewCondition.STALE_RUNTIME
    elif pending is not None:
        condition = ReviewCondition.PENDING
    return ReviewStatus(
        condition,
        str(running),
        str(state_path(project_dir)),
        state.reviewed_through,
        state.pending_target,
        state.first_seen_at,
    )


def _diagnostic_status(
    project_dir: Path,
    running_version: str,
    condition: ReviewCondition,
    diagnostic: str,
) -> ReviewStatus:
    return ReviewStatus(
        condition,
        running_version,
        str(state_path(project_dir)),
        diagnostic=diagnostic,
    )


def observe(
    project_dir: Path,
    *,
    current_version: str | None = None,
    now: str | None = None,
) -> ReviewStatus:
    """Monotonically observe *current_version* and return current review status.

    Corrupt evidence is never overwritten. Storage and version errors are
    returned as typed diagnostics so advisory startup paths remain fail-soft.
    """
    running_value = _running_version(current_version)
    try:
        running = _parse_version(running_value, "running version")
    except CorruptReviewStateError as exc:
        return _diagnostic_status(
            project_dir, running_value, ReviewCondition.UNSUPPORTED, str(exc)
        )
    try:
        with _locked(project_dir):
            path = state_path(project_dir)
            state = _read_state(path)
            if state is None:
                baseline = _doctor_bootstrap_version(project_dir) or running
                state = ReviewState(str(baseline))
            reviewed = _parse_version(state.reviewed_through, "reviewed_through")
            pending = (
                _parse_version(state.pending_target, "pending_target")
                if state.pending_target is not None
                else None
            )
            highest = max(reviewed, pending or reviewed)
            if running > highest:
                state = ReviewState(
                    state.reviewed_through,
                    str(running),
                    state.first_seen_at or _first_seen_at(now),
                )
            _atomic_write(path, state)
            return _status(project_dir, running, state)
    except CorruptReviewStateError as exc:
        return _diagnostic_status(project_dir, running_value, ReviewCondition.CORRUPT, str(exc))
    except ReviewStorageError as exc:
        return _diagnostic_status(
            project_dir, running_value, ReviewCondition.UNAVAILABLE, str(exc)
        )


def acknowledge(
    project_dir: Path,
    expected_target: str,
    *,
    current_version: str | None = None,
    packaged_changelog: Path | None = None,
) -> ReviewStatus:
    """Acknowledge one exact pending target with compare-and-swap semantics."""
    running_value = _running_version(current_version)
    try:
        running = _parse_version(running_value, "running version")
        expected = _parse_version(expected_target, "expected target")
    except CorruptReviewStateError as exc:
        raise AcknowledgmentError(str(exc)) from exc
    if running != expected:
        raise AcknowledgmentError(
            f"running Booley {running} cannot acknowledge expected target {expected}"
        )
    try:
        with _locked(project_dir):
            state = _read_state(state_path(project_dir))
            if state is None:
                raise AcknowledgmentError("upgrade review state does not exist; run status first")
            if state.pending_target != str(expected):
                actual = state.pending_target or "none"
                raise AcknowledgmentError(
                    f"pending target changed: expected {expected}, found {actual}"
                )
            path = packaged_changelog or changelog_path()
            try:
                releases = parse_releases(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, ChangelogError) as exc:
                raise AcknowledgmentError(f"packaged changelog is unreadable: {exc}") from exc
            if expected not in {entry.version for entry in releases}:
                raise AcknowledgmentError(
                    f"packaged changelog has no release entry for {expected}"
                )
            acknowledged = ReviewState(str(expected))
            _atomic_write(state_path(project_dir), acknowledged)
            return _status(project_dir, running, acknowledged)
    except CorruptReviewStateError as exc:
        raise AcknowledgmentError(f"upgrade review state is corrupt: {exc}") from exc
    except ReviewStorageError as exc:
        raise AcknowledgmentError(str(exc)) from exc

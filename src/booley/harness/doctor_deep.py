"""Deep Doctor evidence policy, independent of plain health and scheduling.

Only the executing Booley version and active immutable Sandbox Image determine
currency. Completed unsuccessful attempts veto historical success until a later
qualifying success. Config fingerprints and age belong exclusively to plain Doctor.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path

from booley.core.boundary import (
    require_bool,
    require_dict,
    require_int,
    require_list,
    require_str,
    require_str_value,
)
from booley.core.file_lock import release_file_lock, wait_for_file_lock
from booley.runtime.timefmt import format_human_date, parse_timestamp, utc_now_rfc3339

DEEP_STAMP_FILENAME = "doctor_deep_stamp.json"
_MAX_REASONS = 64
_HEALTH_FIELDS = ("failure_count", "blocking_warning_count", "blocking_warning_ids")


class DeepOutcome(StrEnum):
    """Qualification of a completed deep attempt using existing health verdicts."""

    SUCCESS = "success"
    FAILED = "failed"
    INCOMPLETE = "incomplete"


@dataclass(frozen=True)
class DeepAttempt:
    """Typed evidence of one completed attempt."""

    completed_at: str
    completion_order_ns: int
    attempt_id: str
    booley_version: str
    image_id: str | None
    outcome: DeepOutcome
    complete: bool
    reasons: tuple[str, ...] = ()
    failure_count: int | None = None
    blocking_warning_count: int | None = None
    blocking_warning_ids: tuple[str, ...] | None = None

    @property
    def order(self) -> tuple[int, bool, str]:
        """An exact completion tie favors failed/incomplete evidence."""
        return self.completion_order_ns, self.outcome != DeepOutcome.SUCCESS, self.attempt_id


@dataclass(frozen=True)
class DeepState:
    """Historical success and latest completed result merge independently."""

    last_success: DeepAttempt | None = None
    latest_completed_attempt: DeepAttempt | None = None


@dataclass(frozen=True)
class DeepReason:
    """Stable machine code and bounded human explanation."""

    code: str
    message: str
    checks: tuple[str, ...] = ()


@dataclass(frozen=True)
class DeepStatus:
    """Advisory deep currency; it never changes health verdicts or command gates."""

    current: bool
    last_success: DeepAttempt | None
    reasons: tuple[DeepReason, ...]

    def to_document(self) -> dict:
        """Additive structured output shared by manual and automatic reports."""
        return {
            "status": "current" if self.current else "due",
            "last_success": asdict(self.last_success) if self.last_success else None,
            "reasons": [asdict(reason) for reason in self.reasons],
        }

    def render(self) -> str:
        """Render one informational line using the standard human date helper."""
        if self.current:
            assert self.last_success is not None
            success = self.last_success
            try:
                date = format_human_date(parse_timestamp(success.completed_at))
            except ValueError:
                date = "unknown"
            return f"deep Doctor current (Booley {success.booley_version}, {date})"
        return "deep Doctor due: " + "; ".join(reason.message for reason in self.reasons)


@dataclass(frozen=True)
class DeepAttemptSummary:
    """Explicit execution completeness, separate from warning waiver processing."""

    missing_checks: tuple[str, ...] = ()
    identity_reasons: tuple[str, ...] = ()
    image_id: str | None = None

    @property
    def complete(self) -> bool:
        return not self.missing_checks and not self.identity_reasons and self.image_id is not None

    def attempt(
        self,
        *,
        version: str,
        clean: bool,
        failure_count: int | None = None,
        blocking_warning_ids: tuple[str, ...] | None = None,
    ) -> DeepAttempt:
        """Freeze completion only after all orchestration returns normally."""
        outcome = (
            DeepOutcome.FAILED
            if not clean
            else (DeepOutcome.SUCCESS if self.complete else DeepOutcome.INCOMPLETE)
        )
        return DeepAttempt(
            utc_now_rfc3339(),
            time.time_ns(),
            uuid.uuid4().hex,
            version,
            self.image_id,
            outcome,
            self.complete,
            tuple(sorted(set(self.missing_checks + self.identity_reasons)))[:_MAX_REASONS],
            failure_count,
            len(blocking_warning_ids) if blocking_warning_ids is not None else None,
            tuple(sorted(set(blocking_warning_ids)))[:8]
            if blocking_warning_ids is not None
            else None,
        )


class DeepCheckTracker:
    """Track applicability and completion with stable IDs, never console text."""

    def __init__(self) -> None:
        self._checks: dict[str, bool] = {}

    def require(self, identifier: str) -> None:
        """Declare an applicable check before attempting it."""
        self._checks.setdefault(identifier, False)

    def completed(self, identifier: str, completed: bool) -> None:
        """Record execution evidence independently of PASS/WARN/FAIL grading."""
        self.require(identifier)
        self._checks[identifier] = completed

    @property
    def missing(self) -> tuple[str, ...]:
        """Return every applicable check for which execution evidence is missing."""
        return tuple(sorted(key for key, value in self._checks.items() if not value))


def deep_stamp_path(booley_dir: Path) -> Path:
    """Deep evidence lives in the shared Project runtime, separate from plain stamps."""
    return booley_dir / "runtime" / DEEP_STAMP_FILENAME


def _decode_attempt(raw: object) -> DeepAttempt | None:
    if raw is None:
        return None
    values = require_dict(raw, field="deep attempt")
    image_id = values["image_id"]
    failures = warnings = warning_ids = None
    if any(field in values for field in _HEALTH_FIELDS):
        failures = require_int(values.get("failure_count"), field="failure_count")
        warnings = require_int(
            values.get("blocking_warning_count"), field="blocking_warning_count"
        )
        warning_ids = tuple(
            require_str_value(item) for item in require_list(values.get("blocking_warning_ids"))
        )
    return DeepAttempt(
        require_str(values, "completed_at"),
        require_int(values.get("completion_order_ns"), field="completion_order_ns"),
        require_str(values, "attempt_id"),
        require_str(values, "booley_version"),
        require_str_value(image_id) if image_id is not None else None,
        DeepOutcome(require_str(values, "outcome")),
        require_bool(values, "complete"),
        tuple(require_str_value(item) for item in require_list(values.get("reasons"))),
        failures,
        warnings,
        warning_ids,
    )


def load_deep_state(booley_dir: Path) -> DeepState:
    """Missing, legacy, unsupported, or corrupt records cannot establish currency."""
    try:
        values = require_dict(json.loads(deep_stamp_path(booley_dir).read_text()))
        if require_int(values.get("schema_version"), field="schema_version") != 1:
            return DeepState()
        return DeepState(
            _decode_attempt(values["last_success"]),
            _decode_attempt(values["latest_completed_attempt"]),
        )
    except (OSError, ValueError, UnicodeError, KeyError):
        return DeepState()


def _merge(state: DeepState, attempt: DeepAttempt) -> DeepState:
    success, latest = state.last_success, state.latest_completed_attempt
    if attempt.outcome == DeepOutcome.SUCCESS and (
        success is None or attempt.order > success.order
    ):
        success = attempt
    if latest is None or attempt.order > latest.order:
        latest = attempt
    return DeepState(success, latest)


def _publish(path: Path, state: DeepState) -> None:
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            document = {"schema_version": 1, **asdict(state)}
            for slot in ("last_success", "latest_completed_attempt"):
                if document[slot] is not None:
                    for field in _HEALTH_FIELDS:
                        if document[slot][field] is None:
                            del document[slot][field]
            json.dump(document, handle)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        Path(temporary).replace(path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def record_deep_attempt(booley_dir: Path, attempt: DeepAttempt) -> bool:
    """Atomically merge completed evidence under a bounded shared runtime lock."""
    path = deep_stamp_path(booley_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.with_suffix(".lock").open("a+", encoding="utf-8") as lock:
            wait_for_file_lock(lock, timeout_s=2)
            try:
                state = _merge(load_deep_state(booley_dir), attempt)
                _publish(path, state)
            finally:
                release_file_lock(lock)
    except (OSError, TimeoutError):
        return False
    return True


def evaluate_deep_status(
    state: DeepState,
    *,
    version: str | None,
    image_id: str | None,
) -> DeepStatus:
    """Pure policy: only version/image drift and latest unsuccessful evidence veto."""
    reasons: list[DeepReason] = []
    success = state.last_success
    if success is None:
        reasons.append(DeepReason("no-qualifying-run", "no qualifying deep run recorded"))
    if version is None:
        reasons.append(DeepReason("version-unavailable", "cannot verify executing Booley version"))
    elif success is not None and success.booley_version != version:
        reasons.append(
            DeepReason("version-changed", f"Booley {success.booley_version} → {version}")
        )
    if image_id is None:
        reasons.append(
            DeepReason("image-identity-unavailable", "cannot verify Sandbox Image identity")
        )
    elif success is not None and success.image_id != image_id:
        reasons.append(DeepReason("image-changed", "Sandbox Image changed"))
    latest = state.latest_completed_attempt
    if latest is not None and latest.outcome == DeepOutcome.FAILED:
        reasons.append(_health_reason(latest))
    if latest is not None and (latest.outcome == DeepOutcome.INCOMPLETE or latest.reasons):
        checks = ", ".join(latest.reasons[:8])
        detail = f" ({checks})" if checks else ""
        reasons.append(
            DeepReason(
                "latest-attempt-incomplete",
                f"applicable deep checks incomplete{detail}",
                latest.reasons,
            )
        )
    return DeepStatus(not reasons, success, tuple(reasons))


def _health_reason(attempt: DeepAttempt) -> DeepReason:
    if attempt.failure_count is None:
        return DeepReason(
            "latest-attempt-not-qualified",
            "latest deep attempt did not qualify; legacy health verdict details unavailable",
        )
    ids = attempt.blocking_warning_ids or ()
    warning_text = ", ".join(ids[:8])
    additional = (attempt.blocking_warning_count or 0) - len(ids[:8])
    if additional:
        warning_text += f"; {additional} additional warning blocker(s)"
    detail = f": unwaived warning {warning_text}" if ids else ""
    if attempt.failure_count:
        return DeepReason(
            "latest-attempt-failed",
            f"latest deep health check failed ({attempt.failure_count} failure(s)){detail}",
            ids,
        )
    return DeepReason(
        "latest-attempt-not-qualified", f"latest deep attempt did not qualify{detail}", ids
    )

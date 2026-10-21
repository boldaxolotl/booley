"""Deep Doctor evidence: one record, replaced by each completed deep attempt.

Currency is a pure function of that record, the executing Booley version, and the
active immutable Sandbox Image. Config fingerprints and age belong to plain Doctor.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path

from booley.core.boundary import require_dict, require_list
from booley.runtime.atomic_files import atomic_replace_bytes
from booley.runtime.timefmt import format_human_date, parse_timestamp, utc_now_rfc3339

DEEP_STAMP_FILENAME = "doctor_deep_stamp.json"
_SCHEMA_VERSION = 2
_SHOWN_CHECKS = 8


class DeepOutcome(StrEnum):
    """Qualification of a completed deep attempt using existing health verdicts."""

    SUCCESS = "success"
    FAILED = "failed"
    INCOMPLETE = "incomplete"


@dataclass(frozen=True)
class DeepRecord:
    """The latest completed deep attempt; blocking or missing check IDs as reasons."""

    completed_at: str
    booley_version: str
    image_id: str | None
    outcome: DeepOutcome
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class DeepReason:
    """Stable machine code and human explanation."""

    code: str
    message: str


@dataclass(frozen=True)
class DeepStatus:
    """Advisory deep currency; it never changes health verdicts or command gates."""

    current: bool
    record: DeepRecord | None
    reasons: tuple[DeepReason, ...]

    def to_document(self) -> dict:
        """Additive structured output shared by manual and automatic reports."""
        return {
            "status": "current" if self.current else "due",
            "reasons": [asdict(reason) for reason in self.reasons],
        }

    def render(self) -> str:
        """Render one informational line using the standard human date helper."""
        if not self.current:
            return "deep Doctor due: " + "; ".join(reason.message for reason in self.reasons)
        assert self.record is not None
        try:
            date = format_human_date(parse_timestamp(self.record.completed_at))
        except (TypeError, ValueError):
            date = "unknown"
        return f"deep Doctor current (Booley {self.record.booley_version}, {date})"


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
        self, *, version: str, clean: bool, blocking_checks: tuple[str, ...] = ()
    ) -> DeepRecord:
        """Freeze one completed attempt after all orchestration returned normally."""
        missing = self.missing_checks + self.identity_reasons
        if not clean:
            outcome, reasons = DeepOutcome.FAILED, blocking_checks + missing
        elif self.complete:
            outcome, reasons = DeepOutcome.SUCCESS, ()
        else:
            outcome, reasons = DeepOutcome.INCOMPLETE, missing
        return DeepRecord(
            utc_now_rfc3339(), version, self.image_id, outcome, tuple(sorted(set(reasons)))
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


def load_deep_record(booley_dir: Path) -> DeepRecord | None:
    """Anything but a readable schema-2 record means no qualifying run is recorded."""
    try:
        values = require_dict(json.loads(deep_stamp_path(booley_dir).read_text("utf-8")))
        if values.get("schema_version") != _SCHEMA_VERSION:
            return None
        return DeepRecord(
            values["completed_at"],
            values["booley_version"],
            values["image_id"],
            DeepOutcome(values["outcome"]),
            tuple(str(item) for item in require_list(values["reasons"])),
        )
    except (OSError, ValueError, UnicodeError, KeyError, TypeError):
        return None


def record_deep_attempt(booley_dir: Path, record: DeepRecord) -> bool:
    """Atomically replace the record; ``False`` when it could not be saved."""
    document = {"schema_version": _SCHEMA_VERSION, **asdict(record)}
    try:
        atomic_replace_bytes(deep_stamp_path(booley_dir), (json.dumps(document) + "\n").encode())
    except OSError:
        return False
    return True


def evaluate_deep_status(
    record: DeepRecord | None, *, version: str | None, image_id: str | None
) -> DeepStatus:
    """Pure policy over the latest completed attempt, version, and active image."""
    reasons: list[DeepReason] = []
    if record is None:
        reasons.append(DeepReason("no-qualifying-run", "no qualifying deep run recorded"))
    if version is None:
        reasons.append(DeepReason("version-unavailable", "cannot verify executing Booley version"))
    elif record is not None and record.booley_version != version:
        reasons.append(
            DeepReason("version-changed", f"Booley {record.booley_version} → {version}")
        )
    if image_id is None:
        reasons.append(
            DeepReason("image-identity-unavailable", "cannot verify Sandbox Image identity")
        )
    elif record is not None and record.image_id not in (None, image_id):
        reasons.append(DeepReason("image-changed", "Sandbox Image changed"))
    if record is not None and record.outcome != DeepOutcome.SUCCESS:
        shown = ", ".join(record.reasons[:_SHOWN_CHECKS])
        detail = f" ({shown})" if shown else ""
        if record.outcome == DeepOutcome.FAILED:
            reasons.append(
                DeepReason("latest-attempt-failed", f"latest deep attempt failed{detail}")
            )
        else:
            reasons.append(
                DeepReason(
                    "latest-attempt-incomplete", f"applicable deep checks incomplete{detail}"
                )
            )
    return DeepStatus(not reasons, record, tuple(reasons))

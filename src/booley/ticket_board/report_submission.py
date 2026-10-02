"""Completion fencing for report candidates; a receipt never supplies positive evidence."""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from booley.core.boundary import require_dict
from booley.core.file_lock import LockTimeoutError, release_file_lock, wait_for_file_lock

from .paths import ticket_runtime_dir
from .persistence import atomic_replace_bytes
from .ticket_baseline import ticket_baseline_from_machine

logger = logging.getLogger(__name__)
KEY = "_report_submitted"
ID_KEY = "report_submission_id"
DIGEST_KEY = "report_sha256"


class ReportSubmissionError(ValueError):
    """A report candidate has no valid completion authority."""


class UnknownReportCommitError(ReportSubmissionError):
    """The storage boundary could not establish whether final visibility committed."""


def _cleanup_warning(message: str) -> None:
    # Diagnostic handlers cannot change committed authority.
    with contextlib.suppress(Exception):
        logger.warning(message, exc_info=True)


def receipt_path(log_dir: Path) -> Path:
    return ticket_runtime_dir(log_dir) / "report-submission.json"


def _encoded(value: Mapping[str, Any]) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def read_receipt(log_dir: Path) -> dict[str, Any] | None:
    path = receipt_path(log_dir)
    try:
        if path.is_symlink():
            raise ReportSubmissionError("report receipt must not be a symlink")
        if path.stat().st_size > 65536:
            raise ReportSubmissionError("report submission receipt exceeds size limit")
        row = require_dict(json.loads(path.read_bytes()), field="report submission receipt")
        _validate_receipt(row, log_dir)
        return row
    except FileNotFoundError:
        return None
    except (OSError, ValueError, TypeError) as exc:
        raise ReportSubmissionError(f"cannot validate report submission receipt: {exc}") from exc


def _validate_receipt(row: dict[str, Any], log_dir: Path) -> None:
    if type(row.get("version")) is not int or row["version"] != 1:
        raise ReportSubmissionError("unsupported report submission receipt version")
    if row.get("status") not in {"pending", "failed", "completed"}:
        raise ReportSubmissionError("invalid report submission receipt status")
    if not isinstance(row.get("submission_id"), str) or not re.fullmatch(
        r"[0-9a-f]{32}", row["submission_id"]
    ):
        raise ReportSubmissionError("invalid report submission attempt identity")
    identity = require_dict(row.get("ticket_identity"), field="receipt Ticket identity")
    if identity:
        ticket_baseline_from_machine(identity)
    if not isinstance(row.get("execution_id"), str):
        raise ReportSubmissionError("invalid receipt execution identity")
    path = row.get("report_path")
    if not isinstance(path, str) or Path(path).resolve() != (log_dir.resolve() / "REPORT.md"):
        raise ReportSubmissionError("report receipt path escapes Ticket report")
    digest = row.get("report_sha256")
    if digest is not None and (
        not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
    ):
        raise ReportSubmissionError("invalid report digest")
    if row["status"] == "completed" and digest is None:
        raise ReportSubmissionError("completed report receipt has no digest")


def effective_met(
    log_dir: Path,
    met: bool,
    detail: Mapping[str, Any],
    *,
    identity: Mapping[str, Any] | None = None,
) -> bool:
    """Mask positive evidence until its own matching attempt commits."""
    row = read_receipt(log_dir)
    tagged = detail.get(ID_KEY)
    if row is not None and identity is not None and row["ticket_identity"] != identity:
        row = None
    if not met:
        return False
    if row is None:
        return not tagged
    if row["status"] != "completed":
        return False
    if tagged != row["submission_id"] or detail.get(DIGEST_KEY) != row["report_sha256"]:
        return False
    try:
        return (
            hashlib.sha256((log_dir / "REPORT.md").read_bytes()).hexdigest()
            == row["report_sha256"]
        )
    except OSError as exc:
        raise ReportSubmissionError(f"cannot read committed report: {exc}") from exc


def state_log_dir(state: Any) -> Path | None:
    path = getattr(state, "_file_path", None)
    if path is None:
        return None
    parent = Path(path).parent
    return parent.parent if parent.name in {".runtime", "runtime"} else parent


def project_state(
    state: Any, log_dir: Path | None = None, *, identity: Mapping[str, Any] | None = None
) -> None:
    """Apply completion fencing without changing detail or unrelated Criteria."""
    root = log_dir or state_log_dir(state)
    entry = state.criteria.get(KEY)
    if root is not None and entry is not None:
        entry.met = effective_met(root, entry.met, entry.detail, identity=identity)


def synchronize(log_dir: Path) -> None:
    """Persist a visible committed receipt before preparing downstream authority."""
    if read_receipt(log_dir) is None:
        return
    if os.name == "nt":
        return
    descriptor = os.open(receipt_path(log_dir).parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    except OSError as exc:
        raise ReportSubmissionError(
            f"report receipt durability synchronization failed: {exc}"
        ) from exc
    finally:
        os.close(descriptor)


class Submission:
    """Hold one bounded Ticket lock through the final visibility commit."""

    def __init__(
        self,
        log_dir: Path,
        submission_id: str,
        identity: dict[str, Any],
        execution_id: str,
        *,
        validate: Callable[[], None] | None = None,
    ) -> None:
        self.validate = validate
        self.log_dir = log_dir
        self.path = receipt_path(log_dir)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = (self.path.parent / "report-submission.lock").open("a+", encoding="utf-8")
        self.committed = False
        self.row = {
            "version": 1,
            "status": "pending",
            "submission_id": submission_id,
            "ticket_identity": identity,
            "execution_id": execution_id,
            "report_path": str(log_dir.resolve() / "REPORT.md"),
            "report_sha256": None,
        }
        try:
            self._acquire()
            if self.validate is not None:
                self.validate()
            atomic_replace_bytes(self.path, _encoded(self.row))
        except BaseException:
            self.close()
            raise

    def _acquire(self) -> None:
        try:
            wait_for_file_lock(self.lock, timeout_s=5)
        except LockTimeoutError as exc:
            raise ReportSubmissionError("another report submission holds the Ticket lock") from exc

    def stage(self, content: bytes) -> str:
        report = self.log_dir / "REPORT.md"
        history = self.path.parent / "report-submissions" / self.row["submission_id"]
        history.mkdir(parents=True)
        if report.is_file():
            atomic_replace_bytes(history / "previous-report.md", report.read_bytes(), mode=0o644)
        atomic_replace_bytes(history / "candidate-report.md", content, mode=0o644)
        atomic_replace_bytes(report, content, mode=0o644)
        self.row["report_sha256"] = hashlib.sha256(content).hexdigest()
        return self.row["report_sha256"]

    def _require_owner(self) -> None:
        active = read_receipt(self.log_dir)
        if active is None or active["submission_id"] != self.row["submission_id"]:
            raise ReportSubmissionError("report attempt lost its active submission fence")

    def commit(self) -> None:
        if self.validate is not None:
            self.validate()
        self._require_owner()
        row = {**self.row, "status": "completed"}
        descriptor, raw = tempfile.mkstemp(dir=self.path.parent, prefix=".report-commit.")
        staged = Path(raw)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(_encoded(row))
                stream.flush()
                os.fsync(stream.fileno())
            synchronize(self.log_dir)
            try:
                staged.replace(self.path)
            except OSError as exc:
                try:
                    active = read_receipt(self.log_dir)
                except ReportSubmissionError as read_error:
                    raise UnknownReportCommitError(
                        f"report commit outcome unknown: {exc}; {read_error}"
                    ) from exc
                if active != row:
                    raise
            self.committed = True
        finally:
            try:
                staged.unlink(missing_ok=True)
            except OSError:
                _cleanup_warning("Could not clean report commit staging file")

    def fail(self) -> None:
        if self.committed:
            return
        self._require_owner()
        atomic_replace_bytes(self.path, _encoded({**self.row, "status": "failed"}))

    def close(self) -> None:
        if self.lock.closed:
            return
        try:
            try:
                release_file_lock(self.lock)
            finally:
                self.lock.close()
        except OSError:
            _cleanup_warning("Could not release report submission lock")

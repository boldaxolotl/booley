"""Shared lifecycle policy for Flow-owned progress documents."""

from __future__ import annotations

import json
import os
import stat
import tempfile
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from pathlib import Path
from typing import Any

from booley.runtime.regular_file import open_regular_nofollow
from booley.runtime.timefmt import utc_now_rfc3339

TERMINAL_PHASES = frozenset({"complete", "aborted", "superseded"})
_PROGRESS_PHASES = TERMINAL_PHASES | {"starting", "running", "baseline", "current"}
_MAX_PROGRESS_BYTES = 2 * 1024 * 1024


class ProgressPublicationError(OSError):
    """A Flow could not publish its terminal progress state."""


def validate_progress_shape(document: Mapping[str, object]) -> None:
    """Reject contradictory lifecycle and Target-partition state."""
    complete = document.get("complete")
    phase = document.get("phase")
    if type(complete) is not bool or not isinstance(phase, str):
        raise ValueError("progress requires boolean complete and string phase")
    if phase not in _PROGRESS_PHASES:
        raise ValueError(f"unsupported progress phase: {phase}")
    if complete != (phase in TERMINAL_PHASES):
        raise ValueError("progress complete and phase disagree")
    targets = _string_list(document.get("targets"), "targets")
    completed = _string_list(document.get("completed_targets"), "completed_targets")
    pending = _string_list(document.get("pending_targets"), "pending_targets")
    if len(set(targets)) != len(targets):
        raise ValueError("progress repeats Target identities")
    if len(set(completed)) != len(completed) or len(set(pending)) != len(pending):
        raise ValueError("progress repeats completed or pending Targets")
    if set(completed) & set(pending) or set(completed) | set(pending) != set(targets):
        raise ValueError("progress completed and pending Targets do not partition targets")


def _string_list(value: object, field: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"progress {field} must be a list of strings")
    return value


class ProgressLifecycle:
    """Publish a terminal checkpoint exactly once on every catchable exit."""

    def __init__(self, publish: Callable[[str], None]) -> None:
        self._publish = publish
        self._terminal_attempted = False

    def __enter__(self) -> ProgressLifecycle:
        return self

    def complete(self) -> None:
        """Publish normal completion, repairing once as aborted on failure."""
        if self._terminal_attempted:
            raise RuntimeError("progress lifecycle was already terminalized")
        self._terminal_attempted = True
        try:
            self._publish("complete")
        except Exception as first:
            with suppress(Exception):
                self._publish("aborted")
            raise ProgressPublicationError(str(first)) from first

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> bool:
        del traceback
        if self._terminal_attempted:
            return False
        self._terminal_attempted = True
        try:
            self._publish("aborted")
        except Exception as cleanup:
            if exc_type is None:
                raise ProgressPublicationError(str(cleanup)) from cleanup
        return False


def progress_document(
    *,
    flow: str,
    run_id: str,
    phase: str,
    targets: Sequence[str],
    completed_targets: Sequence[str],
    detail: Mapping[str, object],
    extra: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Build and validate the common lifecycle portion of progress JSON."""
    completed = list(completed_targets)
    completed_set = set(completed)
    document: dict[str, object] = {
        "flow": flow,
        "run_id": run_id,
        "timestamp": utc_now_rfc3339(),
        "phase": phase,
        "complete": phase in TERMINAL_PHASES,
        "targets": list(targets),
        "completed_targets": completed,
        "pending_targets": [target for target in targets if target not in completed_set],
        "detail": dict(detail),
    }
    if extra:
        document.update(extra)
    validate_progress_shape(document)
    return document


def read_progress_for_run(
    report_roots: Sequence[Path], endpoint: str, run_id: str
) -> tuple[Path, dict[str, Any]] | None:
    """Return the newest trusted run-scoped checkpoint and its exact path."""
    candidates: list[tuple[int, Path]] = []
    for root in report_roots:
        try:
            endpoint_root = root / endpoint
            if not endpoint_root.is_dir() or _has_link_between(endpoint_root, root):
                continue
            invocations = tuple(endpoint_root.iterdir())
        except (OSError, ValueError):
            continue
        for invocation in invocations:
            path = invocation / "progress.json"
            if not invocation.name.isdigit():
                continue
            try:
                candidates.append((path.lstat().st_mtime_ns, path))
            except OSError:
                continue
    for _mtime, path in sorted(candidates, key=lambda item: item[0], reverse=True):
        document = _read_trusted_progress(path, report_roots)
        if (
            document is not None
            and document.get("flow") == endpoint
            and document.get("run_id") == run_id
        ):
            return path, document
    return None


def repair_progress_after_reap(report_roots: Sequence[Path], endpoint: str, run_id: str) -> bool:
    """Best-effort terminalization after the supervisor proved a child dead."""
    try:
        found = read_progress_for_run(report_roots, endpoint, run_id)
    except (OSError, ValueError):
        return False
    if found is None:
        return False
    path, document = found
    if document.get("complete") is True:
        return False
    repaired = dict(document)
    repaired.update(
        {
            "complete": True,
            "phase": "aborted",
            "terminal_timestamp": utc_now_rfc3339(),
            "terminal_reason": "producer process exited before terminal publication",
        }
    )
    validate_progress_shape(repaired)
    try:
        _replace_if_unchanged(path, document, repaired)
    except (OSError, ValueError):
        return False
    return True


def supersede_progress(path: Path, *, new_invocation: int, new_run_id: str) -> None:
    """Mark an authenticated coverage origin terminal without changing its history."""
    document = validate_coverage_origin_progress(path)
    phase = document.get("phase")
    if phase in {"superseded", "complete"}:
        return
    if phase not in {"running", "aborted"}:
        raise ValueError("resume origin progress has an ineligible phase")
    updated = dict(document)
    updated.update(
        {
            "complete": True,
            "phase": "superseded",
            "terminal_timestamp": utc_now_rfc3339(),
            "superseded_by": {
                "invocation": new_invocation,
                **({"run_id": new_run_id} if new_run_id else {}),
            },
        }
    )
    validate_progress_shape(updated)
    _replace_if_unchanged(path, document, updated)


def validate_coverage_origin_progress(path: Path) -> dict[str, Any]:
    """Validate the exact authenticated invocation progress before recovery."""
    document = _read_trusted_progress(path, (path.parents[2],))
    if document is None or document.get("flow") != "sim" or document.get("coverage") is not True:
        raise ValueError("resume origin has no valid coverage progress")
    return document


def _read_trusted_progress(path: Path, report_roots: Sequence[Path]) -> dict[str, Any] | None:
    try:
        if _has_link_between(path, _containing_root(path, report_roots)):
            return None
        document = _read_json_object_nofollow(path)
        validate_progress_shape(document)
        return document
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def _containing_root(path: Path, roots: Sequence[Path]) -> Path:
    absolute = path.absolute()
    for root in roots:
        candidate = root.absolute()
        try:
            absolute.relative_to(candidate)
        except ValueError:
            continue
        return candidate
    raise ValueError("progress path is outside configured report roots")


def _has_link_between(path: Path, root: Path) -> bool:
    current = path.absolute()
    root = root.absolute()
    current.relative_to(root)
    while True:
        try:
            info = current.lstat()
        except FileNotFoundError:
            return False
        if stat.S_ISLNK(info.st_mode):
            return True
        if current == root:
            return False
        current = current.parent


def _replace_if_unchanged(
    path: Path, original: Mapping[str, object], updated: Mapping[str, object]
) -> None:
    current = _read_json_object_nofollow(path)
    if current != original:
        raise ValueError("progress changed concurrently")
    descriptor, temporary = tempfile.mkstemp(prefix=".progress-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(updated, stream, sort_keys=True, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        Path(temporary).replace(path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _read_json_object_nofollow(path: Path) -> dict[str, Any]:
    descriptor = open_regular_nofollow(path)
    with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
        if os.fstat(stream.fileno()).st_size > _MAX_PROGRESS_BYTES:
            raise ValueError("progress exceeds the size limit")
        document = json.load(stream)
    if not isinstance(document, dict):
        raise ValueError("progress must be a JSON object")
    return document

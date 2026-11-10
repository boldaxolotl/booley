"""Dependency-free shape policy for Flow progress documents.

Shared by Flow publication, Runtime Job observation and Campaign retention so
none of them imports another layer to agree on what a valid document is.
"""

from __future__ import annotations

from collections.abc import Mapping

TERMINAL_PHASES = frozenset({"complete", "aborted", "superseded"})
_PROGRESS_PHASES = TERMINAL_PHASES | {"starting", "running", "baseline", "current"}


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

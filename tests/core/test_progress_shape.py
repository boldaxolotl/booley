"""Progress-shape policy shared by Flows, Job observation and Campaign retention."""

from __future__ import annotations

from collections.abc import Mapping

import pytest


def _validate(document: Mapping[str, object]) -> None:
    """Validate through the Core owner, imported at call time so a missing owner fails the test."""
    from booley.core.progress_shape import validate_progress_shape

    validate_progress_shape(document)


def _running_document() -> dict[str, object]:
    """A valid in-flight progress document with one pending Target."""
    return {
        "flow": "sim",
        "run_id": "run-1",
        "timestamp": "2026-01-01T00:00:00Z",
        "phase": "running",
        "complete": False,
        "targets": ["sim_a"],
        "completed_targets": [],
        "pending_targets": ["sim_a"],
        "detail": {},
    }


def test_contradictory_terminal_shape_is_rejected() -> None:
    document = _running_document()
    document["complete"] = True
    with pytest.raises(ValueError, match="complete and phase disagree"):
        _validate(document)


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        pytest.param({"complete": "yes"}, "boolean complete", id="complete-not-bool"),
        pytest.param({"phase": "unknown"}, "unsupported progress phase", id="unknown-phase"),
        pytest.param(
            {"targets": ["sim_a", "sim_a"]}, "repeats Target identities", id="repeated-targets"
        ),
        pytest.param(
            {"completed_targets": ["sim_a", "sim_a"]},
            "repeats completed",
            id="repeated-completed",
        ),
        pytest.param({"pending_targets": []}, "do not partition", id="not-partitioned"),
        pytest.param({"targets": [1]}, "list of strings", id="non-string-target"),
    ],
)
def test_invalid_progress_shapes_are_rejected(updates: dict[str, object], message: str) -> None:
    document = _running_document()
    document.update(updates)

    with pytest.raises(ValueError, match=message):
        _validate(document)

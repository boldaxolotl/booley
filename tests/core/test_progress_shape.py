"""Progress-shape policy shared by Flows and Job observation."""

from __future__ import annotations

import pytest

from booley.core.progress_shape import validate_progress_shape


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        pytest.param({"complete": True}, "complete and phase disagree", id="contradictory"),
        pytest.param({"complete": "yes"}, "boolean complete", id="complete-not-bool"),
        pytest.param({"phase": "unknown"}, "unsupported progress phase", id="unknown-phase"),
        pytest.param(
            {"targets": ["sim_a", "sim_a"]}, "repeats Target identities", id="repeated-targets"
        ),
        pytest.param(
            {"completed_targets": ["sim_a", "sim_a"]}, "repeats completed", id="repeated-completed"
        ),
        pytest.param({"pending_targets": []}, "do not partition", id="not-partitioned"),
        pytest.param({"targets": [1]}, "list of strings", id="non-string-target"),
    ],
)
def test_invalid_progress_shapes_are_rejected(updates: dict[str, object], message: str) -> None:
    running = {
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

    with pytest.raises(ValueError, match=message):
        validate_progress_shape({**running, **updates})

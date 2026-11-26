"""Tests for run-indexed transcript rotation and crash-recovery run counting.

Regression context: ``_detect_crash_recovery`` rotated transcripts as
``run_NNN.jsonl``, but both backends then relabeled the path via
``transcript_path_for_label`` which REPLACED the stem with the label — every
developer run silently overwrote ``developer.jsonl`` and the rotation never
happened. The label transform now preserves the run tag
(``developer.run_003.jsonl``), and run counting must survive both namings.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from booley.runtime._retry import transcript_path_for_attempt, transcript_path_for_label


@pytest.mark.parametrize(
    ("base", "label", "expected"),
    [
        (None, "developer", None),
        ("/x/run_001.jsonl", None, "/x/run_001.jsonl"),
        ("/x/run_001.jsonl", "", "/x/run_001.jsonl"),
        ("/x/run_003.jsonl", "developer", "/x/developer.run_003.jsonl"),
        ("/x/run_003-retry2.jsonl", "developer", "/x/developer.run_003-retry2.jsonl"),
        # Specialist bases (no run tag) keep the plain label swap.
        ("/x/whatever.jsonl", "reviewer_round2", "/x/reviewer_round2.jsonl"),
        ("/x/whatever-retry3.jsonl", "reviewer", "/x/reviewer-retry3.jsonl"),
        # Only bare run_NNN[-retryN] stems count as run-indexed.
        ("/x/run_abc.jsonl", "developer", "/x/developer.jsonl"),
        ("/x/rerun_001.jsonl", "developer", "/x/developer.jsonl"),
    ],
)
def test_transcript_path_for_label(base, label, expected):
    path = transcript_path_for_label(None if base is None else Path(base), label)
    assert path == (None if expected is None else Path(expected))


def test_retry_suffix_applied_after_relabel_keeps_run_tag():
    """Backends relabel first, then suffix per attempt — both must compose."""
    labeled = transcript_path_for_label(Path("/x/run_003.jsonl"), "developer")
    assert transcript_path_for_attempt(labeled, 2) == Path("/x/developer.run_003-retry2.jsonl")

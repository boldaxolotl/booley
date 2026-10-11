"""Tests for run-indexed transcript rotation and crash-recovery run counting.

Regression context: ``_detect_crash_recovery`` rotated transcripts as
``run_NNN.jsonl``, but both backends then relabeled the path via
``transcript_path_for_label`` which REPLACED the stem with the label — every
developer run silently overwrote ``developer.jsonl`` and the rotation never
happened. The label transform now preserves the run tag
(``developer.run_003.jsonl``), and run counting must survive both namings.
"""

from __future__ import annotations

import os
from pathlib import Path

from booley.runtime._retry import transcript_path_for_attempt, transcript_path_for_label

# ---------------------------------------------------------------------------
# transcript_path_for_label
# ---------------------------------------------------------------------------


class TestTranscriptPathForLabel:
    def test_none_base_or_label_passthrough(self):
        assert transcript_path_for_label(None, "developer") is None
        base = Path("/x/run_001.jsonl")
        assert transcript_path_for_label(base, None) == base
        assert transcript_path_for_label(base, "") == base

    def test_run_indexed_base_preserves_run_tag(self):
        base = Path("/x/run_003.jsonl")
        assert transcript_path_for_label(base, "developer") == Path("/x/developer.run_003.jsonl")

    def test_run_indexed_base_preserves_retry_suffix(self):
        base = Path("/x/run_003-retry2.jsonl")
        assert transcript_path_for_label(base, "developer") == Path(
            "/x/developer.run_003-retry2.jsonl"
        )

    def test_retry_suffix_applied_after_relabel_keeps_run_tag(self):
        """Backends relabel first, then suffix per attempt — both must compose."""
        labeled = transcript_path_for_label(Path("/x/run_003.jsonl"), "developer")
        assert transcript_path_for_attempt(labeled, 2) == Path("/x/developer.run_003-retry2.jsonl")

    def test_non_run_indexed_base_keeps_legacy_behavior(self):
        # Specialist bases (no run tag) keep the plain label swap
        base = Path("/x/whatever.jsonl")
        assert transcript_path_for_label(base, "mutation_creator_round2") == Path(
            "/x/mutation_creator_round2.jsonl"
        )

    def test_non_run_indexed_base_keeps_retry_suffix(self):
        base = Path("/x/whatever-retry3.jsonl")
        assert transcript_path_for_label(base, "reviewer") == Path("/x/reviewer-retry3.jsonl")

    def test_run_like_but_not_run_indexed_stems_are_legacy(self):
        # Only bare run_NNN[-retryN] stems count as run-indexed
        base = Path("/x/run_abc.jsonl")
        assert transcript_path_for_label(base, "developer") == Path("/x/developer.jsonl")
        base = Path("/x/rerun_001.jsonl")
        assert transcript_path_for_label(base, "developer") == Path("/x/developer.jsonl")


# ---------------------------------------------------------------------------
# _detect_crash_recovery
# ---------------------------------------------------------------------------


def _transcript_dir(logs_dir: Path) -> Path:
    d = logs_dir / ".runtime" / "developer"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _touch(path: Path, mtime: float) -> Path:
    path.write_text("{}\n", encoding="utf-8")
    os.utime(path, (mtime, mtime))
    return path

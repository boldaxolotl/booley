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

from booley.harness._retry import transcript_path_for_attempt, transcript_path_for_label
from booley.harness.developer import _detect_crash_recovery

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


class TestDetectCrashRecovery:
    def test_no_prior_transcripts(self, tmp_path: Path):
        is_recovery, crash, run_index, next_path = _detect_crash_recovery(tmp_path)
        assert is_recovery is False
        assert crash is None
        assert run_index == 1
        assert next_path.name == "run_001.jsonl"

    def test_retry_variants_count_as_one_run(self, tmp_path: Path):
        d = _transcript_dir(tmp_path)
        _touch(d / "developer.run_001.jsonl", 1000)
        latest = _touch(d / "developer.run_001-retry2.jsonl", 2000)

        is_recovery, crash, run_index, next_path = _detect_crash_recovery(tmp_path)

        assert is_recovery is True
        assert run_index == 2  # one distinct prior run, not two
        assert crash == latest  # most recent by mtime
        assert next_path.name == "run_002.jsonl"

    def test_specialist_transcripts_are_ignored(self, tmp_path: Path):
        d = _transcript_dir(tmp_path)
        _touch(d / "developer.run_001.jsonl", 1000)
        # Specialists writing into this dir must not inflate the run index
        _touch(d / "mutation_creator_round1.jsonl", 3000)
        _touch(d / "reviewer.jsonl", 4000)

        is_recovery, crash, run_index, _ = _detect_crash_recovery(tmp_path)

        assert is_recovery is True
        assert run_index == 2
        assert crash == d / "developer.run_001.jsonl"

    def test_legacy_namings_still_count(self, tmp_path: Path):
        d = _transcript_dir(tmp_path)
        _touch(d / "run_001.jsonl", 1000)  # legacy bare run naming
        _touch(d / "developer.jsonl", 2000)  # legacy flat label naming
        latest = _touch(d / "developer-retry2.jsonl", 3000)  # retry of the flat run

        is_recovery, crash, run_index, _ = _detect_crash_recovery(tmp_path)

        assert is_recovery is True
        # run_001 + developer(+retry collapsed) = 2 distinct prior runs
        assert run_index == 3
        assert crash == latest

    def test_crash_transcript_is_latest_by_mtime(self, tmp_path: Path):
        d = _transcript_dir(tmp_path)
        latest = _touch(d / "developer.run_001.jsonl", 5000)
        _touch(d / "developer.run_002.jsonl", 1000)  # older despite higher index

        _, crash, run_index, _ = _detect_crash_recovery(tmp_path)

        assert crash == latest
        assert run_index == 3

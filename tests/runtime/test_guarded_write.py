"""Tests for the shared clobber-guarded writer at its runtime import path.

The full ownership contract is pinned by ``tests/harness/setup/test_common.py``
through the init re-export; these tests cover each outcome at the new path.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from booley.harness.setup import common
from booley.runtime.guarded_write import WriteOutcome, guarded_write

MARKER = "# managed by test"


def test_init_common_reexports_the_shared_writer() -> None:
    # The init path is a compatibility re-export, not a second implementation.
    assert common.guarded_write is guarded_write
    assert common.WriteOutcome is WriteOutcome


def test_create_only_file_becomes_user_owned(tmp_path: Path) -> None:
    target = tmp_path / "sub" / "booley.toml"

    assert guarded_write(target, "skeleton\n") is WriteOutcome.WRITTEN
    target.write_text("user content\n", encoding="utf-8")
    assert guarded_write(target, "skeleton\n") is WriteOutcome.SKIPPED
    assert target.read_text(encoding="utf-8") == "user content\n"


def test_managed_file_is_refreshed_then_unchanged(tmp_path: Path) -> None:
    target = tmp_path / "unit.service"
    target.write_text(f"{MARKER}\nbody v1\n", encoding="utf-8")
    content = f"{MARKER}\nbody v2\n"

    assert guarded_write(target, content, owner_marker=MARKER) is WriteOutcome.WRITTEN
    assert guarded_write(target, content, owner_marker=MARKER) is WriteOutcome.UNCHANGED
    assert target.read_text(encoding="utf-8") == content


def test_foreign_file_is_refused_or_backed_up(tmp_path: Path) -> None:
    refused = tmp_path / "unit.service"
    refused.write_text("hand-rolled unit\n", encoding="utf-8")
    backed_up = tmp_path / "commit-msg"
    backed_up.write_text("custom hook\n", encoding="utf-8")
    content = f"{MARKER}\nours\n"

    assert guarded_write(refused, content, owner_marker=MARKER) is WriteOutcome.REFUSED
    assert refused.read_text(encoding="utf-8") == "hand-rolled unit\n"
    outcome = guarded_write(backed_up, content, owner_marker=MARKER, backup_suffix=".pre-booley")
    assert outcome is WriteOutcome.BACKED_UP
    assert (tmp_path / "commit-msg.pre-booley").read_text(encoding="utf-8") == "custom hook\n"
    assert backed_up.read_text(encoding="utf-8") == content


def test_dry_run_reports_without_writing(tmp_path: Path) -> None:
    target = tmp_path / "booley.toml"

    assert guarded_write(target, "skeleton\n", dry_run=True) is WriteOutcome.WRITTEN
    assert not target.exists()


def test_content_missing_its_own_marker_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="owner marker"):
        guarded_write(tmp_path / "x", "no marker here\n", owner_marker=MARKER)

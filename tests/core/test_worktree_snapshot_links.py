"""A Snapshot never copies below a symbolic link in the destination."""

from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.mark.skipif(os.name == "nt", reason="needs symlinks")
def test_copy_snapshot_refuses_a_destination_link_that_aliases_the_source(tmp_path: Path) -> None:
    from booley.runtime.worktree_snapshot import SnapshotError, copy_snapshot

    source, destination = tmp_path / "data", tmp_path / "copy"
    record = source / "goals" / "history" / "H1.md"
    record.parent.mkdir(parents=True)
    record.write_text("keep\n", encoding="utf-8")
    destination.mkdir()
    (destination / "goals").symlink_to(source / "goals")

    with pytest.raises(SnapshotError, match="symbolic link"):
        copy_snapshot(source, destination)

    assert record.read_text(encoding="utf-8") == "keep\n"

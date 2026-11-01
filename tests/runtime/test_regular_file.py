"""Tests for secure retained-file opening."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from tests.conftest import symlink_or_skip

from booley.runtime.regular_file import open_regular_nofollow


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor traversal contract")
def test_open_regular_nofollow_reads_regular_file(tmp_path: Path) -> None:
    path = tmp_path / "evidence.json"
    path.write_bytes(b"evidence")

    descriptor = open_regular_nofollow(path)
    try:
        assert os.read(descriptor, 8) == b"evidence"
    finally:
        os.close(descriptor)


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor traversal contract")
def test_open_regular_nofollow_rejects_directory(tmp_path: Path) -> None:
    with pytest.raises(OSError, match="regular file"):
        open_regular_nofollow(tmp_path)


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor traversal contract")
def test_open_regular_nofollow_rejects_parent_traversal(tmp_path: Path) -> None:
    path = tmp_path / "child" / ".." / "evidence.json"

    with pytest.raises(OSError, match="traversal"):
        open_regular_nofollow(path)


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor traversal contract")
def test_open_regular_nofollow_follows_only_the_booley_directory_entry(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    store = tmp_path / "store"
    (store / "runtime").mkdir(parents=True)
    record = store / "runtime" / "x.json"
    record.write_bytes(b"record")
    symlink_or_skip(project / ".booley", store, target_is_directory=True)
    entry = project / ".booley"
    through_entry = entry / "runtime" / "x.json"

    # The entry is an ordinary link unless it is named as the Booley Directory.
    with pytest.raises(OSError):
        open_regular_nofollow(through_entry)
    descriptor = open_regular_nofollow(through_entry, booley_dir=entry)
    try:
        assert os.read(descriptor, 6) == b"record"
    finally:
        os.close(descriptor)

    # A link below the entry is still refused.
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "y.json").write_bytes(b"y")
    symlink_or_skip(store / "runtime" / "link", elsewhere, target_is_directory=True)
    with pytest.raises(OSError):
        open_regular_nofollow(entry / "runtime" / "link" / "y.json", booley_dir=entry)
    # A link as the final component is still refused.
    symlink_or_skip(store / "runtime" / "final", record)
    with pytest.raises(OSError):
        open_regular_nofollow(entry / "runtime" / "final", booley_dir=entry)

    # A link above the entry is still refused.
    alias = tmp_path / "alias"
    symlink_or_skip(alias, project, target_is_directory=True)
    with pytest.raises(OSError):
        open_regular_nofollow(
            alias / ".booley" / "runtime" / "x.json", booley_dir=alias / ".booley"
        )

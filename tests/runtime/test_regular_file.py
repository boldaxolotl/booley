"""Tests for secure retained-file opening."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

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

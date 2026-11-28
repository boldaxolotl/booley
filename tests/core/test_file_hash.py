"""The one SHA-256-of-a-file helper agrees with hashing the bytes."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "content",
    [b"", b"abc", bytes(range(256)) * 8193],  # the last is larger than one read
    ids=["empty", "small", "multi-chunk"],
)
def test_file_digest_equals_hashing_the_bytes(tmp_path: Path, content: bytes) -> None:
    from booley.core import file_hash

    path = tmp_path / "input.bin"
    path.write_bytes(content)
    expected = hashlib.sha256(content).hexdigest()

    assert file_hash.file_sha256(path) == expected
    assert file_hash.file_sha256(path, retry_sharing=True) == expected
    assert file_hash.file_sha256_or_none(path) == expected
    assert file_hash.file_sha256_and_size(path) == (expected, len(content))


def test_an_absent_file_has_no_digest(tmp_path: Path) -> None:
    from booley.core import file_hash

    absent = tmp_path / "absent"

    assert file_hash.file_sha256_or_none(absent) is None
    with pytest.raises(FileNotFoundError):
        file_hash.file_sha256(absent)

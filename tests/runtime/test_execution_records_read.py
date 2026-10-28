"""Protocol record reads: transient Windows retry, otherwise indeterminate.

``read_file_bytes`` owns the retry budget and its POSIX/Windows split (see
``tests/test_filesystem_utils.py``); these tests pin only what ``read_json``
adds on top of it.
"""

from __future__ import annotations

import errno
import json
import sys
from pathlib import Path

import pytest
from tests.filesystem_test_support import FlakyOpen

from booley.runtime.execution_records import read_json


def _bare_eacces() -> PermissionError:
    """The error Windows open() raises for a sharing violation: EACCES, no winerror."""
    return PermissionError(errno.EACCES, "Permission denied")


def test_read_json_retries_transient_windows_sharing_violation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A rewrite in flight must not read as "indeterminate" (#2352 follow-up).
    record = tmp_path / "record.json"
    record.write_text(json.dumps({"state": "running"}), encoding="utf-8")
    FlakyOpen(record, [_bare_eacces()]).install(monkeypatch)
    with monkeypatch.context() as windows:
        windows.setattr(sys, "platform", "win32")
        assert read_json(record) == {"state": "running"}


def test_read_json_treats_an_unreadable_record_as_indeterminate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = tmp_path / "record.json"
    record.write_text("{}", encoding="utf-8")
    # On POSIX a bare EACCES is final, so this needs no retry wait.
    FlakyOpen(record, [_bare_eacces()]).install(monkeypatch)
    with monkeypatch.context() as posix:
        posix.setattr(sys, "platform", "linux")
        assert read_json(record) is None


@pytest.mark.parametrize(
    "content",
    [None, b"\xff{not json", b"[1, 2]"],
    ids=["missing", "corrupt", "not-an-object"],
)
def test_read_json_treats_missing_or_invalid_records_as_indeterminate(
    tmp_path: Path, content: bytes | None
) -> None:
    record = tmp_path / "record.json"
    if content is not None:
        record.write_bytes(content)
    assert read_json(record) is None

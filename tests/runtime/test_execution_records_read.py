"""Protocol record reads survive transient Windows sharing violations."""

from __future__ import annotations

import errno
import json
import sys
from pathlib import Path

import pytest

from booley.runtime.execution_records import read_json


def _fail_first_opens(monkeypatch: pytest.MonkeyPatch, target: Path, failures: int) -> list[int]:
    """Make the first ``failures`` opens of ``target`` raise a bare EACCES.

    Windows open() goes through the C runtime, which reports a sharing
    violation (a concurrent ``replace_file``) as EACCES with no winerror.
    Returns a one-item counter of open attempts on ``target``.
    """
    real_open = Path.open
    calls = [0]

    def flaky_open(path: Path, *args: object, **kwargs: object):
        if path == target:
            calls[0] += 1
            if calls[0] <= failures:
                raise PermissionError(errno.EACCES, "Permission denied")
        return real_open(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "open", flaky_open)
    return calls


def test_read_json_retries_transient_windows_sharing_violation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A rewrite in flight must not read as "indeterminate" (#2352 follow-up).
    record = tmp_path / "record.json"
    record.write_text(json.dumps({"state": "running"}), encoding="utf-8")
    calls = _fail_first_opens(monkeypatch, record, failures=2)
    delays: list[float] = []
    with monkeypatch.context() as windows:
        windows.setattr(sys, "platform", "win32")
        assert read_json(record, sleep=delays.append) == {"state": "running"}
    assert calls[0] == 3
    assert len(delays) == 2


def test_read_json_keeps_indeterminate_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    delays: list[float] = []
    assert read_json(tmp_path / "missing.json", sleep=delays.append) is None

    corrupt = tmp_path / "corrupt.json"
    corrupt.write_bytes(b"\xff{not json")
    assert read_json(corrupt, sleep=delays.append) is None

    not_object = tmp_path / "list.json"
    not_object.write_text("[1, 2]", encoding="utf-8")
    assert read_json(not_object, sleep=delays.append) is None

    # A persistent Windows sharing error still collapses to None, after a bounded wait.
    held = tmp_path / "held.json"
    held.write_text("{}", encoding="utf-8")
    calls = _fail_first_opens(monkeypatch, held, failures=10_000)
    with monkeypatch.context() as windows:
        windows.setattr(sys, "platform", "win32")
        assert read_json(held, sleep=delays.append) is None
    assert calls[0] == len(delays) + 1
    assert sum(delays) < 2.0

    # POSIX EACCES is a real permission error: no retry.
    delays.clear()
    posix_calls = _fail_first_opens(monkeypatch, held, failures=10_000)
    with monkeypatch.context() as posix:
        posix.setattr(sys, "platform", "linux")
        assert read_json(held, sleep=delays.append) is None
    assert posix_calls[0] == 1
    assert delays == []

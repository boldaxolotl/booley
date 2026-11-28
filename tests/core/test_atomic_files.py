"""Crash-safety tests for the shared atomic file publication primitives."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from booley.core import atomic_files
from booley.core.atomic_files import Durability


def test_write_once_never_exposes_partial_final_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "receipt.json"

    def interrupt(_source: Path, _destination: Path) -> None:
        raise OSError("link interrupted")

    monkeypatch.setattr(os, "link", interrupt)

    with pytest.raises(OSError, match="link interrupted"):
        atomic_files.atomic_write_once(destination, b"complete\n")
    assert not destination.exists()
    assert list(tmp_path.iterdir()) == []


def test_write_once_accepts_identical_bytes_and_rejects_conflicts(tmp_path: Path) -> None:
    destination = tmp_path / "nested" / "receipt.json"

    assert atomic_files.atomic_write_once(destination, b"first\n") is True
    assert atomic_files.atomic_write_once(destination, b"first\n") is False
    with pytest.raises(atomic_files.WriteOnceConflictError, match="conflicting write-once"):
        atomic_files.atomic_write_once(destination, b"second\n")
    assert destination.read_bytes() == b"first\n"
    assert sorted(path.name for path in destination.parent.iterdir()) == ["receipt.json"]


def test_replace_bytes_publishes_complete_content(tmp_path: Path) -> None:
    destination = tmp_path / "state.json"

    atomic_files.atomic_replace_bytes(destination, b"old\n")
    atomic_files.atomic_replace_bytes(destination, b"new\n")

    assert destination.read_bytes() == b"new\n"
    assert list(tmp_path.iterdir()) == [destination]

    atomic_files.atomic_write_json(destination, {"b": 1, "a": 2})
    assert destination.read_bytes() == b'{"a": 2, "b": 1}\n'


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
@pytest.mark.parametrize(
    ("existing", "options", "expected"),
    [
        (None, {"mode": 0o640}, 0o640),
        (None, {"mode": None}, None),
        (0o604, {"keep_mode": True}, 0o604),
        (None, {"keep_mode": True, "mode": 0o644}, 0o644),
    ],
    ids=["fixed", "umask", "keep", "keep-absent"],
)
def test_published_mode_follows_the_mode_options(
    tmp_path: Path, existing: int | None, options: dict, expected: int | None
) -> None:
    destination = tmp_path / "published"
    if existing is not None:
        destination.write_bytes(b"old")
        destination.chmod(existing)
    if expected is None:
        # What an ordinary write gets under this process's umask.
        reference = tmp_path / "reference"
        reference.write_bytes(b"")
        expected = stat.S_IMODE(reference.stat().st_mode)

    atomic_files.atomic_write_text(destination, "new", **options)

    assert stat.S_IMODE(destination.stat().st_mode) == expected


@pytest.mark.parametrize(
    ("durability", "synced"),
    [(Durability.FULL, ["file", "directory"]), (Durability.FILE, ["file"]), (Durability.NONE, [])],
    ids=["full", "file", "none"],
)
def test_durability_option_controls_fsync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, durability: Durability, synced: list[str]
) -> None:
    calls: list[str] = []
    real_fsync = os.fsync

    def record(descriptor: int) -> None:
        calls.append("directory" if stat.S_ISDIR(os.fstat(descriptor).st_mode) else "file")
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", record)

    atomic_files.atomic_write_text(tmp_path / "published", "new", durability=durability)

    if os.name == "nt":
        synced = [kind for kind in synced if kind == "file"]
    assert calls == synced


class _FailingStream:
    def __init__(self, descriptor: int) -> None:
        self.descriptor = descriptor

    def __enter__(self) -> _FailingStream:
        return self

    def __exit__(self, *_error: object) -> None:
        os.close(self.descriptor)

    def write(self, _content: bytes) -> None:
        raise OSError("step failed")


def _fail(*_args: object) -> None:
    raise OSError("step failed")


@pytest.mark.parametrize("step", ["write", "file-fsync", "replace", "json-replace"])
def test_failed_write_before_replace_keeps_previous_file_and_no_temporary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, step: str
) -> None:
    destination = tmp_path / "state.json"
    destination.write_bytes(b"previous")
    if step == "write":
        monkeypatch.setattr(os, "fdopen", lambda descriptor, _mode: _FailingStream(descriptor))
    elif step == "file-fsync":
        monkeypatch.setattr(os, "fsync", _fail)
    else:
        monkeypatch.setattr(Path, "replace", _fail)

    with pytest.raises(OSError, match="failed"):
        if step == "json-replace":
            atomic_files.atomic_write_json(destination, {"new": True})
        else:
            atomic_files.atomic_write_text(destination, "new")

    assert destination.read_bytes() == b"previous"
    assert list(tmp_path.iterdir()) == [destination]


def test_durable_unlink_reports_whether_the_path_existed(tmp_path: Path) -> None:
    destination = tmp_path / "claim.json"
    destination.write_bytes(b"claim\n")

    assert atomic_files.durable_unlink(destination) is True
    assert not destination.exists()
    assert atomic_files.durable_unlink(destination) is False


def test_durable_unlink_survives_transient_sharing_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.sharing_error_support import hold_unlinks

    destination = tmp_path / "claim.json"
    destination.write_bytes(b"claim\n")
    attempts = hold_unlinks(monkeypatch, {destination: 1})
    assert atomic_files.durable_unlink(destination) is True
    assert not destination.exists()
    assert attempts[destination] == 2


def test_durable_unlink_retries_before_raising_lasting_sharing_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.sharing_error_support import hold_unlinks

    destination = tmp_path / "claim.json"
    destination.write_bytes(b"claim\n")
    attempts = hold_unlinks(monkeypatch, {destination: 100})
    with pytest.raises(PermissionError):
        atomic_files.durable_unlink(destination)
    assert attempts[destination] > 1
    assert destination.exists()

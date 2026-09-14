"""Security and durability contracts shared by host-private state callers."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from booley.core import private_store


class StoreError(RuntimeError):
    """The test caller's diagnostic type."""


@pytest.fixture
def store(tmp_path: Path) -> private_store.PrivateStore:
    anchor = tmp_path / "anchor"
    return private_store.PrivateStore(anchor / "state" / "records", anchor, "test", StoreError)


def test_creates_private_chain_and_round_trips_json(store):
    assert not store.validate_existing_directory()
    assert store.ensure_directory() == store.root
    store.atomic_write_text("record.json", '{"value": 1}')
    assert store.read_json("record.json") == {"value": 1}
    assert store.validate_existing_directory()
    if os.name != "nt":
        assert stat.S_IMODE(store.root.stat().st_mode) == 0o700
        assert stat.S_IMODE(store.root.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE((store.root / "record.json").stat().st_mode) == 0o600


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode and ownership policy")
@pytest.mark.parametrize("location,mode", [("anchor", 0o777), ("root", 0o755), ("file", 0o644)])
def test_rejects_unsafe_modes(store, location, mode):
    store.ensure_directory()
    store.atomic_write_text("record.json", "{}")
    path = {"anchor": store.anchor, "root": store.root, "file": store.root / "record.json"}[
        location
    ]
    path.chmod(mode)
    with pytest.raises(StoreError, match=r"unsafe|mode"):
        if location == "file":
            store.read_json("record.json")
        else:
            store.validate_existing_directory()


@pytest.mark.skipif(os.name == "nt", reason="POSIX ownership policy")
@pytest.mark.parametrize("operation", ["directory", "file", "lock"])
def test_rejects_foreign_ownership(store, monkeypatch, operation):
    store.ensure_directory()
    store.atomic_write_text("record.json", "{}")
    uid = os.getuid()
    monkeypatch.setattr(private_store.os, "getuid", lambda: uid + 1)
    with pytest.raises(StoreError, match=r"unsafe|owned"):
        if operation == "directory":
            store.validate_existing_directory()
        elif operation == "file":
            store.read_json("record.json")
        else:
            with store.locked("record.lock", busy_message="busy"):
                pytest.fail("foreign-owned lock was admitted")


@pytest.mark.parametrize("location", ["anchor", "intermediate", "root", "file", "lock"])
def test_rejects_symlinks(store, tmp_path, location):
    from tests.conftest import symlink_or_skip

    if location == "lock" and not hasattr(os, "O_NOFOLLOW"):
        pytest.skip("secure lock opening uses the platform's O_NOFOLLOW flag")
    store.ensure_directory()
    if location in {"file", "lock"}:
        target = tmp_path / "outside.json"
        target.write_text("{}")
        link = store.root / ("record.json" if location == "file" else "record.lock")
    else:
        link = {"anchor": store.anchor, "intermediate": store.root.parent, "root": store.root}[
            location
        ]
        target = link.with_name(link.name + "-real")
        link.rename(target)
    symlink_or_skip(link, target, target_is_directory=location not in {"file", "lock"})
    with pytest.raises((StoreError, OSError), match=r"symlink|securely|symbolic"):
        if location == "file":
            store.read_json("record.json")
        elif location == "lock":
            with store.locked("record.lock", busy_message="busy"):
                pytest.fail("symlink lock was admitted")
        else:
            store.ensure_directory()


def test_directory_cannot_be_read_as_private_file(store):
    store.ensure_directory()
    (store.root / "record.json").mkdir()
    with pytest.raises((StoreError, OSError)):
        store.read_json("record.json")


def test_lock_contends_then_releases_after_body_error(store):
    store.ensure_directory()
    with (
        pytest.raises(ValueError, match="body"),
        store.locked("record.lock", busy_message="outer busy"),
    ):
        with (
            pytest.raises(StoreError, match="inner busy"),
            store.locked("record.lock", busy_message="inner busy", timeout_s=0),
        ):
            pytest.fail("contended lock was admitted")
        raise ValueError("body")
    with store.locked("record.lock", busy_message="released"):
        assert (store.root / "record.lock").is_file()


def test_atomic_replacement_fsyncs_file_before_replace_and_directory_after(store, monkeypatch):
    store.ensure_directory()
    store.atomic_write_text("record.json", '{"old": true}')
    events = []
    real_replace = Path.replace
    real_fsync = os.fsync

    def fsync(fd):
        events.append("directory" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file")
        real_fsync(fd)

    def replace(path, destination):
        assert store.read_json("record.json") == {"old": True}
        assert path.parent == store.root
        events.append("replace")
        return real_replace(path, destination)

    monkeypatch.setattr(private_store.os, "fsync", fsync)
    monkeypatch.setattr(Path, "replace", replace)
    store.atomic_write_text("record.json", '{"new": true}')
    assert events == (["file", "replace"] if os.name == "nt" else ["file", "replace", "directory"])
    assert store.read_json("record.json") == {"new": True}
    assert sorted(p.name for p in store.root.iterdir()) == ["record.json"]


def test_runtime_compatibility_import_preserves_store_identity():
    from booley.runtime.private_store import PrivateStore

    assert PrivateStore is private_store.PrivateStore


@pytest.mark.parametrize("failure", ["file_fsync", "replace", "directory_fsync"])
def test_failed_write_propagates_and_cleans_temporary_file(store, monkeypatch, failure):
    if os.name == "nt" and failure == "directory_fsync":
        pytest.skip("Windows does not fsync directories")
    store.ensure_directory()
    store.atomic_write_text("record.json", '{"old": true}')
    real_fsync = os.fsync

    def fsync(fd):
        directory = stat.S_ISDIR(os.fstat(fd).st_mode)
        if (failure == "directory_fsync" and directory) or (
            failure == "file_fsync" and not directory
        ):
            raise OSError("durability failure")
        real_fsync(fd)

    def fail_replace(_path, _destination):
        raise OSError("replace failure")

    monkeypatch.setattr(private_store.os, "fsync", fsync)
    if failure == "replace":
        monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(OSError, match="failure"):
        store.atomic_write_text("record.json", '{"new": true}')
    expected = {"new": True} if failure == "directory_fsync" else {"old": True}
    assert store.read_json("record.json") == expected
    assert sorted(p.name for p in store.root.iterdir()) == ["record.json"]

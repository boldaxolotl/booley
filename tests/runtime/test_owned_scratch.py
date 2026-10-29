"""Owner-marked scratch: removal on exit and the conservative stale sweep."""

import json
import os
import sys
from pathlib import Path

import pytest
from tests.conftest import require_symlinks
from tests.owned_scratch_support import exited_process_payload, write_marker

from booley.runtime import owned_scratch
from booley.runtime.pid import UNKNOWN, ProcessObservation, capture_process_identity

requires_procfs = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="owner liveness fixtures need procfs"
)


def _own_payload() -> dict[str, object]:
    identity = capture_process_identity(os.getpid())
    assert identity is not None
    return identity.to_payload()


def _entry(root: Path, name: str = "booley-specialist-ro-case") -> Path:
    entry = root / name
    entry.mkdir()
    (entry / "payload.txt").write_text("data", encoding="utf-8")
    return entry


@requires_procfs
def test_sweep_removes_dead_reused_and_zombie_owners(tmp_path, monkeypatch):
    dead = _entry(tmp_path, "booley-specialist-ro-dead")
    write_marker(dead, exited_process_payload())
    reused = _entry(tmp_path, "codex-nested-home-reused")
    write_marker(reused, {**_own_payload(), "start_token": 1})
    live = _entry(tmp_path, "booley-coverage-analysis-live")
    write_marker(live, _own_payload())

    removed = owned_scratch.sweep_stale_scratch(tmp_path)

    assert sorted(path.name for path in removed) == [dead.name, reused.name]
    assert live.is_dir()


@requires_procfs
@pytest.mark.parametrize(
    "case",
    [
        "no-marker",
        "malformed-json",
        "wrong-kind",
        "wrong-version",
        "oversized",
        "bad-owner",
        "marker-is-directory",
        "foreign-prefix",
        "symlink-entry",
        "unknown-observation",
    ],
)
def test_sweep_never_removes_unowned_or_live_entries(tmp_path, monkeypatch, case):
    dead_owner = exited_process_payload()
    entry = _entry(tmp_path)
    marker = entry / owned_scratch.OWNER_MARKER
    survivor = entry
    if case == "malformed-json":
        marker.write_text("{not json", encoding="utf-8")
    elif case == "wrong-kind":
        marker.write_text(json.dumps({"kind": "other", "version": 1, "owner": dead_owner}))
    elif case == "wrong-version":
        marker.write_text(json.dumps({"kind": owned_scratch.MARKER_KIND, "version": 2}))
    elif case == "oversized":
        padding = "x" * owned_scratch.MAX_MARKER_BYTES
        marker.write_text(
            json.dumps(
                {
                    "kind": owned_scratch.MARKER_KIND,
                    "version": 1,
                    "owner": dead_owner,
                    "p": padding,
                }
            )
        )
    elif case == "bad-owner":
        write_marker(entry, {"pid": "not-a-pid"})
    elif case == "marker-is-directory":
        marker.mkdir()
    elif case == "foreign-prefix":
        survivor = _entry(tmp_path, "unrelated-dir")
        write_marker(survivor, dead_owner)
    elif case == "symlink-entry":
        require_symlinks(tmp_path)
        survivor = _entry(tmp_path, "elsewhere")
        write_marker(survivor, dead_owner)
        (tmp_path / "booley-specialist-ro-link").symlink_to(survivor, target_is_directory=True)
    elif case == "unknown-observation":
        write_marker(entry, dead_owner)
        monkeypatch.setattr(
            owned_scratch, "observe_process", lambda _identity: ProcessObservation(UNKNOWN)
        )

    assert owned_scratch.sweep_stale_scratch(tmp_path) == []
    assert survivor.is_dir()
    assert (survivor / "payload.txt").read_text() == "data"


def test_sweep_ignores_files_and_missing_root(tmp_path):
    (tmp_path / "booley-specialist-ro-file").write_text("not a directory")
    assert owned_scratch.sweep_stale_scratch(tmp_path) == []
    assert owned_scratch.sweep_stale_scratch(tmp_path / "absent") == []


def test_sweep_skips_directories_of_other_users(tmp_path, monkeypatch):
    entry = _entry(tmp_path)
    write_marker(entry, exited_process_payload())
    monkeypatch.setattr(os, "getuid", lambda: entry.stat().st_uid + 1, raising=False)
    assert owned_scratch.sweep_stale_scratch(tmp_path) == []
    assert entry.is_dir()


@requires_procfs
def test_owned_tempdir_marks_then_removes_on_exception(tmp_path, monkeypatch):
    import tempfile

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    seen: list[Path] = []
    with (
        pytest.raises(RuntimeError, match="boom"),
        owned_scratch.owned_tempdir("booley-specialist-ro-") as directory,
    ):
        seen.append(directory)
        assert directory.parent == tmp_path
        assert (directory / owned_scratch.OWNER_MARKER).is_file()
        raise RuntimeError("boom")
    assert seen and list(tmp_path.iterdir()) == []


def test_mark_without_identity_leaves_directory_unmarked(tmp_path, monkeypatch):
    monkeypatch.setattr(owned_scratch, "capture_process_identity", lambda _pid: None)
    owned_scratch.mark_owned(tmp_path)
    assert list(tmp_path.iterdir()) == []

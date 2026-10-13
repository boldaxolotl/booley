"""Old source stash diagnostics are read-only and scoped to this Project."""

from __future__ import annotations

import json
import os
import stat
from unittest.mock import Mock

import pytest

from booley.agent_workspace.legacy_stashes import legacy_stash_manifests
from booley.harness.doctor import _check_legacy_source_stashes
from tests.conftest import symlink_or_skip


def test_doctor_warns_without_changing_legacy_stash(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "booley.agent_workspace.legacy_stashes.tempfile.gettempdir", lambda: str(tmp_path)
    )
    project = tmp_path / "project"
    project.mkdir()
    stash = tmp_path / "booley_isolation_old"
    stash.mkdir()
    (stash / "source.sv").write_text("uncommitted work\n")
    manifest = tmp_path / "booley_isolation_old.manifest.json"
    content = json.dumps(
        {
            "work_dir": str(project),
            "stash_dir": str(stash),
            "moved": [[str(project / "source.sv"), str(stash / "source.sv")]],
        }
    )
    manifest.write_text(content)
    stash.chmod(0)
    locked_mode = stat.S_IMODE(stash.stat().st_mode)
    reporter = Mock()
    try:
        _check_legacy_source_stashes(project, reporter)
        warning, guidance = reporter.warn_.call_args.args
        assert warning.check_id == "workspace.legacy-source-stash"
        assert str(manifest) in warning
        assert "Recovery is manual" in guidance
        assert manifest.read_text() == content
        assert stat.S_IMODE(stash.stat().st_mode) == locked_mode
        assert not (project / "source.sv").exists()
    finally:
        stash.chmod(0o700)
    assert (stash / "source.sv").read_text() == "uncommitted work\n"


@pytest.mark.parametrize("suffix", [".manifest.json", ".manifest.json.retired"])
def test_matching_retired_and_file_stash_records_are_reported(tmp_path, monkeypatch, suffix):
    monkeypatch.setattr(
        "booley.agent_workspace.legacy_stashes.tempfile.gettempdir", lambda: str(tmp_path)
    )
    manifest = tmp_path / ("booley_hide_files_old" + suffix)
    manifest.write_text(json.dumps({"work_dir": str(tmp_path)}))
    assert legacy_stash_manifests(tmp_path) == [manifest]


def test_foreign_malformed_and_symlink_records_are_ignored(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "booley.agent_workspace.legacy_stashes.tempfile.gettempdir", lambda: str(tmp_path)
    )
    for name, content in [
        ("foreign", json.dumps({"work_dir": "/other"})),
        ("malformed", "{"),
        ("array", "[]"),
        ("encoding", "\udcff"),
    ]:
        (tmp_path / f"booley_isolation_{name}.manifest.json").write_bytes(
            content.encode("utf-8", errors="surrogateescape")
        )
    target = tmp_path / "record.json"
    target.write_text(json.dumps({"work_dir": str(tmp_path)}))
    symlink_or_skip(tmp_path / "booley_isolation_link.manifest.json", target)
    assert legacy_stash_manifests(tmp_path) == []
    assert target.exists()


@pytest.mark.skipif(not hasattr(os, "getuid"), reason="UID ownership unavailable")
def test_other_uid_record_is_ignored(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "booley.agent_workspace.legacy_stashes.tempfile.gettempdir", lambda: str(tmp_path)
    )
    manifest = tmp_path / "booley_isolation_other_uid.manifest.json"
    manifest.write_text(json.dumps({"work_dir": str(tmp_path)}))
    monkeypatch.setattr(
        "booley.agent_workspace.legacy_stashes.os.getuid", lambda: manifest.stat().st_uid + 1
    )
    assert legacy_stash_manifests(tmp_path) == []

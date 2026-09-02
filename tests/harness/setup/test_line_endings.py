"""Boundary-level contracts for Project-repository line-ending reconciliation."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import patch

from booley.harness.setup import line_endings
from booley.harness.setup.line_endings import (
    LineEndingActionKind,
    LineEndingActionState,
    LineEndingMode,
    LineEndingObservationCode,
    LineEndingStatus,
    reconcile_project_line_endings,
)


def _git(root: Path, *args: str, input_bytes: bytes | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        input=input_bytes,
        capture_output=True,
        check=False,
    )


def _init(root: Path, *, autocrlf: str = "false") -> None:
    root.mkdir(parents=True, exist_ok=True)
    assert _git(root, "init", "-q").returncode == 0
    assert _git(root, "config", "core.autocrlf", autocrlf).returncode == 0


def _commit_file(root: Path, name: str, data: bytes) -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    assert _git(root, "add", "-f", name).returncode == 0
    assert (
        _git(
            root,
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "commit",
            "-qm",
            name,
        ).returncode
        == 0
    )


def _crlf_repo(root: Path, name: str = "a.v") -> Path:
    _init(root, autocrlf="true")
    _commit_file(root, name, b"module a;\nendmodule\n")
    (root / name).unlink()
    assert _git(root, "checkout", "--", name).returncode == 0
    assert b"\r\n" in (root / name).read_bytes()
    return root


def _index_path(root: Path) -> Path:
    result = _git(root, "rev-parse", "--path-format=absolute", "--git-path", "index")
    assert result.returncode == 0
    return Path(os.fsdecode(result.stdout).strip())


def _snapshot(path: Path) -> tuple[bytes, int, int]:
    metadata = path.stat()
    return path.read_bytes(), metadata.st_mtime_ns, metadata.st_mode


def test_no_repository_is_not_applicable(tmp_path: Path):
    report = reconcile_project_line_endings(tmp_path, None, mode=LineEndingMode.INSPECT)

    assert report.status is LineEndingStatus.NOT_APPLICABLE
    assert report.repositories == ()


def test_inspection_is_mechanically_read_only(tmp_path: Path):
    _init(tmp_path)
    _commit_file(tmp_path, "a.v", b"module a;\nendmodule\n")
    attributes = tmp_path / ".gitattributes"
    attributes.write_text("*.bat -text\n", encoding="utf-8")
    config = tmp_path / ".git" / "config"
    index = _index_path(tmp_path)
    tracked = tmp_path / "a.v"
    os.utime(tracked, None)
    before = {path: _snapshot(path) for path in (config, index, attributes, tracked)}

    report = reconcile_project_line_endings(tmp_path, None, mode=LineEndingMode.INSPECT)

    assert report.status is LineEndingStatus.SAFE
    assert {path: _snapshot(path) for path in before} == before


def test_repair_is_idempotent_through_public_interface(tmp_path: Path):
    _crlf_repo(tmp_path)

    first = reconcile_project_line_endings(tmp_path, None, mode=LineEndingMode.REPAIR)
    second = reconcile_project_line_endings(tmp_path, None, mode=LineEndingMode.REPAIR)

    assert first.status is LineEndingStatus.SAFE
    assert second.status is LineEndingStatus.SAFE
    assert second.repositories[0].actions == ()


def test_stale_plan_never_overwrites_intervening_worktree_edit(tmp_path: Path):
    _crlf_repo(tmp_path)
    local_edit = b"module a;\r\n  localparam KEEP = 1;\r\nendmodule\r\n"
    real_actions = line_endings._repair_actions

    def edit_then_apply(plan):
        (tmp_path / "a.v").write_bytes(local_edit)
        return real_actions(plan)

    with patch.object(line_endings, "_repair_actions", side_effect=edit_then_apply):
        report = reconcile_project_line_endings(tmp_path, None, mode=LineEndingMode.REPAIR)

    assert report.status is LineEndingStatus.UNSAFE
    assert (tmp_path / "a.v").read_bytes() == local_edit
    normalize = next(
        action
        for action in report.repositories[0].actions
        if action.kind is LineEndingActionKind.NORMALIZE_FILES
    )
    assert normalize.state is LineEndingActionState.FAILED


def test_stale_index_input_refuses_worktree_replacement(tmp_path: Path):
    _crlf_repo(tmp_path)
    original = (tmp_path / "a.v").read_bytes()
    real_actions = line_endings._repair_actions

    def change_index_then_apply(plan):
        blob = _git(tmp_path, "hash-object", "-w", "--stdin", input_bytes=b"changed index\n")
        assert blob.returncode == 0
        oid = os.fsdecode(blob.stdout).strip()
        assert _git(tmp_path, "update-index", "--cacheinfo", "100644", oid, "a.v").returncode == 0
        return real_actions(plan)

    with patch.object(line_endings, "_repair_actions", side_effect=change_index_then_apply):
        report = reconcile_project_line_endings(tmp_path, None, mode=LineEndingMode.REPAIR)

    assert report.status is LineEndingStatus.UNSAFE
    assert (tmp_path / "a.v").read_bytes() == original


def test_attribute_change_refuses_worktree_replacement(tmp_path: Path):
    _crlf_repo(tmp_path)
    original = (tmp_path / "a.v").read_bytes()
    real_actions = line_endings._repair_actions

    def change_attributes_then_apply(plan):
        info_attributes = tmp_path / ".git" / "info" / "attributes"
        info_attributes.write_text("a.v -text\n", encoding="utf-8")
        return real_actions(plan)

    with patch.object(line_endings, "_repair_actions", side_effect=change_attributes_then_apply):
        report = reconcile_project_line_endings(tmp_path, None, mode=LineEndingMode.REPAIR)

    assert report.status is LineEndingStatus.UNSAFE
    assert (tmp_path / "a.v").read_bytes() == original


def test_concurrent_gitattributes_creation_is_preserved(tmp_path: Path):
    _init(tmp_path, autocrlf="true")
    real_pin = line_endings._pin_autocrlf
    user_policy = b"* -text\n"

    def pin_then_create(plan):
        result = real_pin(plan)
        (tmp_path / ".gitattributes").write_bytes(user_policy)
        return result

    with patch.object(line_endings, "_pin_autocrlf", side_effect=pin_then_create):
        report = reconcile_project_line_endings(tmp_path, None, mode=LineEndingMode.REPAIR)

    assert report.status is LineEndingStatus.UNSAFE
    assert (tmp_path / ".gitattributes").read_bytes() == user_policy
    publish = next(
        action
        for action in report.repositories[0].actions
        if action.kind is LineEndingActionKind.PUBLISH_ATTRIBUTES
    )
    assert publish.state is LineEndingActionState.REFUSED


def test_config_change_after_inspection_is_refused_for_that_run(tmp_path: Path):
    _init(tmp_path, autocrlf="true")
    real_actions = line_endings._repair_actions

    def change_config_then_apply(plan):
        assert _git(tmp_path, "config", "core.autocrlf", "false").returncode == 0
        return real_actions(plan)

    with patch.object(line_endings, "_repair_actions", side_effect=change_config_then_apply):
        report = reconcile_project_line_endings(tmp_path, None, mode=LineEndingMode.REPAIR)

    assert report.status is LineEndingStatus.UNSAFE
    pin = next(
        action
        for action in report.repositories[0].actions
        if action.kind is LineEndingActionKind.PIN_AUTOCRLF
    )
    assert pin.state is LineEndingActionState.REFUSED


def test_failed_config_command_remains_unsafe_even_if_value_changed(tmp_path: Path):
    _init(tmp_path, autocrlf="true")
    real_run = line_endings.subprocess.run

    def fail_after_config(*args, **kwargs):
        command = args[0]
        result = real_run(*args, **kwargs)
        if command[-4:] == ["config", "--local", "core.autocrlf", "false"]:
            return subprocess.CompletedProcess(command, 1, result.stdout, "simulated failure")
        return result

    with patch.object(line_endings.subprocess, "run", side_effect=fail_after_config):
        report = reconcile_project_line_endings(tmp_path, None, mode=LineEndingMode.REPAIR)

    assert _git(tmp_path, "config", "--local", "--get", "core.autocrlf").stdout.strip() == b"false"
    assert report.status is LineEndingStatus.UNSAFE


def test_stale_index_refresh_preserves_unrelated_staged_and_unstaged_edits(tmp_path: Path):
    _crlf_repo(tmp_path)
    _commit_file(tmp_path, "b.v", b"module b;\nendmodule\n")
    _commit_file(tmp_path, "c.v", b"module c;\nendmodule\n")
    assert _git(tmp_path, "config", "core.autocrlf", "false").returncode == 0
    (tmp_path / "a.v").write_bytes(b"module a;\nendmodule\n")
    (tmp_path / "b.v").write_bytes(b"module b;\nlocalparam STAGED = 1;\nendmodule\n")
    assert _git(tmp_path, "add", "b.v").returncode == 0
    unstaged = b"module c;\nlocalparam UNSTAGED = 1;\nendmodule\n"
    (tmp_path / "c.v").write_bytes(unstaged)
    staged_before = _git(tmp_path, "diff", "--cached", "--binary").stdout

    report = reconcile_project_line_endings(tmp_path, None, mode=LineEndingMode.REPAIR)

    assert report.status is LineEndingStatus.SAFE
    assert _git(tmp_path, "diff", "--cached", "--binary").stdout == staged_before
    assert (tmp_path / "c.v").read_bytes() == unstaged
    assert b"a.v" not in _git(tmp_path, "status", "--porcelain").stdout


def test_post_repair_unreadable_inspection_is_unsafe_and_retry_converges(tmp_path: Path):
    _crlf_repo(tmp_path)
    real_scan = line_endings._crlf_worktree_files
    calls = 0

    def fail_final_scan(root: Path):
        nonlocal calls
        calls += 1
        return None if calls == 2 else real_scan(root)

    with patch.object(line_endings, "_crlf_worktree_files", side_effect=fail_final_scan):
        first = reconcile_project_line_endings(tmp_path, None, mode=LineEndingMode.REPAIR)
    retry = reconcile_project_line_endings(tmp_path, None, mode=LineEndingMode.REPAIR)

    assert first.status is LineEndingStatus.UNSAFE
    assert LineEndingObservationCode.EOL_SCAN_UNREADABLE in {
        item.code for item in first.repositories[0].observations
    }
    assert retry.status is LineEndingStatus.SAFE


def test_separate_repository_failure_does_not_rollback_safe_outer_progress(tmp_path: Path):
    outer = tmp_path / "outer"
    data = tmp_path / "data"
    _init(outer, autocrlf="true")
    _crlf_repo(data, "hook.sh")
    dirty = b"#!/bin/sh\r\necho local edit\r\n"
    (data / "hook.sh").write_bytes(dirty)

    report = reconcile_project_line_endings(outer, data, mode=LineEndingMode.REPAIR)

    assert report.status is LineEndingStatus.UNSAFE
    by_role = {item.repository.role: item for item in report.repositories}
    assert by_role["project-checkout"].status is LineEndingStatus.SAFE
    assert by_role["project-data"].status is LineEndingStatus.UNSAFE
    assert (data / "hook.sh").read_bytes() == dirty
    assert _git(outer, "config", "--local", "--get", "core.autocrlf").stdout.strip() == b"false"

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


def test_repository_discovery_preserves_probe_failure_context(tmp_path: Path):
    failures = (
        (FileNotFoundError(), "git unavailable"),
        (subprocess.TimeoutExpired(["git"], 10), "Git probe failed"),
        (OSError("permission denied"), "Git probe failed"),
    )

    for failure, expected in failures:
        with patch.object(line_endings.subprocess, "run", side_effect=failure):
            discovery = line_endings.discover_line_ending_repositories(tmp_path)
        assert discovery.repositories == ()
        assert len(discovery.failures) == 1
        assert expected in discovery.failures[0].detail


def test_git_probe_error_helpers_preserve_binary_and_failed_output(tmp_path: Path):
    assert line_endings._error_text(b"bad bytes\xff\n") == "bad bytes�"
    failed = subprocess.CompletedProcess(["git"], 2, "", "failed")
    with patch.object(line_endings.subprocess, "run", return_value=failed):
        assert line_endings._crlf_worktree_files(tmp_path) is None
        assert line_endings.read_autocrlf_setting(tmp_path) is None
    with patch.object(line_endings.subprocess, "run", side_effect=OSError("unavailable")):
        assert line_endings._crlf_worktree_files(tmp_path) is None
        assert line_endings.read_autocrlf_setting(tmp_path) is None


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


def test_atomic_replacement_failure_preserves_original_file(tmp_path: Path):
    _crlf_repo(tmp_path)
    original = (tmp_path / "a.v").read_bytes()

    with patch.object(Path, "replace", side_effect=OSError("simulated publication failure")):
        report = reconcile_project_line_endings(tmp_path, None, mode=LineEndingMode.REPAIR)

    assert report.status is LineEndingStatus.UNSAFE
    assert (tmp_path / "a.v").read_bytes() == original
    assert not list(tmp_path.glob(".booley-eol-*"))


def test_atomic_staging_failure_preserves_original_file(tmp_path: Path):
    _crlf_repo(tmp_path)
    original = (tmp_path / "a.v").read_bytes()

    with patch.object(Path, "chmod", side_effect=OSError("simulated metadata failure")):
        report = reconcile_project_line_endings(tmp_path, None, mode=LineEndingMode.REPAIR)

    assert report.status is LineEndingStatus.UNSAFE
    assert (tmp_path / "a.v").read_bytes() == original
    assert not list(tmp_path.glob(".booley-eol-*"))


def test_atomic_attributes_creation_failure_leaves_no_partial_file(tmp_path: Path):
    _init(tmp_path, autocrlf="true")

    with patch.object(line_endings.os, "link", side_effect=OSError("simulated link failure")):
        report = reconcile_project_line_endings(tmp_path, None, mode=LineEndingMode.REPAIR)

    assert report.status is LineEndingStatus.UNSAFE
    assert not (tmp_path / ".gitattributes").exists()
    assert not list(tmp_path.glob(".booley-eol-*"))


def test_atomic_attributes_update_failure_preserves_original_file(tmp_path: Path):
    _crlf_repo(tmp_path)
    _commit_file(tmp_path, ".gitattributes", b"*.bat -text\n")
    original = (tmp_path / ".gitattributes").read_bytes()
    real_replace = Path.replace

    def fail_attributes(staged: Path, target: Path):
        if target.name == ".gitattributes":
            raise OSError("simulated attributes publication failure")
        return real_replace(staged, target)

    with patch.object(Path, "replace", new=fail_attributes):
        report = reconcile_project_line_endings(tmp_path, None, mode=LineEndingMode.REPAIR)

    assert report.status is LineEndingStatus.UNSAFE
    assert (tmp_path / ".gitattributes").read_bytes() == original
    assert not list(tmp_path.glob(".booley-eol-*"))


def test_index_refresh_exception_restores_and_remains_unsafe(tmp_path: Path):
    _crlf_repo(tmp_path)
    real_run = line_endings.subprocess.run

    def fail_refresh(*args, **kwargs):
        command = args[0]
        if "add" in command and "-u" in command:
            raise OSError("simulated refresh failure")
        return real_run(*args, **kwargs)

    with patch.object(line_endings.subprocess, "run", side_effect=fail_refresh):
        report = reconcile_project_line_endings(tmp_path, None, mode=LineEndingMode.REPAIR)

    assert report.status is LineEndingStatus.UNSAFE
    assert _git(tmp_path, "diff", "--cached", "--quiet").returncode == 0
    assert _git(tmp_path, "ls-files", "-v", "a.v").stdout.startswith(b"H ")


def test_index_refresh_restores_unexpected_entry_and_flag_changes(tmp_path: Path):
    _crlf_repo(tmp_path)
    real_run = line_endings.subprocess.run

    def mutate_then_fail(*args, **kwargs):
        command = args[0]
        result = real_run(*args, **kwargs)
        if "add" not in command or "-u" not in command:
            return result
        blob = real_run(
            ["git", "-C", str(tmp_path), "hash-object", "-w", "--stdin"],
            input=b"unexpected staged content\n",
            capture_output=True,
            check=False,
        )
        oid = blob.stdout.decode().strip()
        real_run(
            ["git", "-C", str(tmp_path), "update-index", "--cacheinfo", "100644", oid, "a.v"],
            capture_output=True,
            check=False,
        )
        real_run(
            ["git", "-C", str(tmp_path), "update-index", "--assume-unchanged", "a.v"],
            capture_output=True,
            check=False,
        )
        return subprocess.CompletedProcess(command, 1, result.stdout, b"simulated failure")

    with patch.object(line_endings.subprocess, "run", side_effect=mutate_then_fail):
        report = reconcile_project_line_endings(tmp_path, None, mode=LineEndingMode.REPAIR)

    assert report.status is LineEndingStatus.UNSAFE
    assert _git(tmp_path, "diff", "--cached", "--quiet").returncode == 0
    assert _git(tmp_path, "ls-files", "-v", "a.v").stdout.startswith(b"H ")


def test_index_recovery_reports_restore_and_verification_failures(tmp_path: Path):
    before = {"a.v": line_endings._IndexPathState(b"entry", b"H a.v\0")}

    with patch.object(line_endings, "_restore_index_state", return_value="restore failed"):
        assert (
            "could not restore exact index state"
            in line_endings._restore_and_verify_index_state(tmp_path, before)
        )
    with (
        patch.object(line_endings, "_restore_index_state", return_value=None),
        patch.object(
            line_endings,
            "_index_path_states",
            return_value=(None, "verification failed"),
        ),
    ):
        assert "could not verify" in line_endings._restore_and_verify_index_state(tmp_path, before)
    with (
        patch.object(line_endings, "_restore_index_state", return_value=None),
        patch.object(line_endings, "_index_path_states", return_value=({}, None)),
    ):
        assert "did not restore exact" in line_endings._restore_and_verify_index_state(
            tmp_path, before
        )


def test_index_flag_groups_preserve_combined_flags():
    states = {
        "plain.v": line_endings._IndexPathState(b"entry", b"H plain.v\0"),
        "assume.v": line_endings._IndexPathState(b"entry", b"h assume.v\0"),
        "skip.v": line_endings._IndexPathState(b"entry", b"S skip.v\0"),
        "both.v": line_endings._IndexPathState(b"entry", b"s both.v\0"),
    }

    assume_unchanged, skip_worktree = line_endings._index_flag_groups(states)

    assert assume_unchanged == ["assume.v", "both.v"]
    assert skip_worktree == ["skip.v", "both.v"]


def test_index_flag_update_errors_are_reported(tmp_path: Path):
    with patch.object(line_endings.subprocess, "run", side_effect=OSError("git unavailable")):
        assert line_endings._update_index_paths(tmp_path, "--skip-worktree", ["a.v"]) == (
            "git unavailable"
        )
    failed = subprocess.CompletedProcess(["git", "update-index"], 1, b"", b"flag failed")
    with patch.object(line_endings.subprocess, "run", return_value=failed):
        assert line_endings._update_index_paths(tmp_path, "--skip-worktree", ["a.v"]) == (
            "flag failed"
        )


def test_index_refresh_verification_failure_restores_and_remains_unsafe(tmp_path: Path):
    _crlf_repo(tmp_path)
    real_states = line_endings._index_path_states
    calls = 0

    def fail_first_verification(root: Path, paths: list[str]):
        nonlocal calls
        calls += 1
        if calls == 2:
            return None, "simulated verification failure"
        return real_states(root, paths)

    with patch.object(
        line_endings,
        "_index_path_states",
        side_effect=fail_first_verification,
    ):
        report = reconcile_project_line_endings(tmp_path, None, mode=LineEndingMode.REPAIR)

    assert report.status is LineEndingStatus.UNSAFE
    assert _git(tmp_path, "diff", "--cached", "--quiet").returncode == 0
    assert _git(tmp_path, "ls-files", "-v", "a.v").stdout.startswith(b"H ")


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

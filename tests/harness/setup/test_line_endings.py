"""Boundary-level contracts for Project-repository line-ending reconciliation."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

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


@pytest.mark.parametrize("ending", [b"\r\n", b"\n"])
def test_clean_baseline_never_discards_later_content_edits(tmp_path: Path, ending: bytes):
    _crlf_repo(tmp_path)
    baseline = line_endings.sample_worktree_cleanliness(tmp_path)
    assert baseline[tmp_path] is True
    edited = b"module a;\r\n  localparam KEEP = 1;" + ending + b"endmodule\r\n"
    (tmp_path / "a.v").write_bytes(edited)
    index_before = _git(tmp_path, "ls-files", "--stage", "-z").stdout

    report = reconcile_project_line_endings(
        tmp_path, mode=LineEndingMode.REPAIR, clean_baseline=baseline
    )

    assert report.status is LineEndingStatus.UNSAFE
    assert (tmp_path / "a.v").read_bytes() == edited
    assert _git(tmp_path, "ls-files", "--stage", "-z").stdout == index_before


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
    _disable_stealth(tmp_path)
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


def test_readonly_staged_file_cleanup_does_not_crash(tmp_path: Path):
    """Windows [WinError 5] when unlinking a read-only temp file must not crash."""
    from booley.harness.setup.line_endings import _cleanup_staged_files

    readonly = tmp_path / ".booley-eol-readonly"
    readonly.write_bytes(b"staged content")
    readonly.chmod(0o444)

    _cleanup_staged_files({"a.v": readonly})

    if os.name == "nt":
        readonly.chmod(0o644)
        readonly.unlink()
    else:
        assert not readonly.exists()


def test_stealth_default_is_local_and_keeps_upstream_checkout_clean(tmp_path: Path):
    root = tmp_path / "upstream"
    _init(root)
    _commit_file(root, ".gitignore", b".booley_project/\n")
    data = root / ".booley_project"
    data.mkdir()
    (data / "booley.toml").write_text("[stealth]\nenabled = true\n")
    assert _git(root, "config", "--unset", "core.autocrlf").returncode == 0

    report = reconcile_project_line_endings(root, data, mode=LineEndingMode.REPAIR)

    assert report.status is LineEndingStatus.SAFE
    assert not (root / ".gitattributes").exists()
    assert _git(root, "status", "--porcelain").stdout == b""
    assert (root / ".git/info/attributes").read_text() == "* text=auto eol=lf\n"
    assert _git(root, "check-attr", "text", "eol", "--", ".gitignore").stdout == (
        b".gitignore: text: auto\n.gitignore: eol: lf\n"
    )


def _disable_stealth(root: Path) -> None:
    data = root / ".booley_project"
    data.mkdir(parents=True, exist_ok=True)
    (data / "booley.toml").write_text("[stealth]\nenabled = false\n")


def _stealth_repo(root: Path, *, enabled: bool | None = True) -> Path:
    _init(root)
    _commit_file(root, ".gitignore", b".booley_project/\n")
    data = root / ".booley_project"
    data.mkdir(exist_ok=True)
    setting = "" if enabled is None else f"enabled = {str(enabled).lower()}\n"
    (data / "booley.toml").write_text("[stealth]\n" + setting)
    return data


@pytest.mark.parametrize("enabled", [True, None])
def test_pinned_stealth_missing_policy_is_repaired_idempotently(tmp_path: Path, enabled):
    data = _stealth_repo(tmp_path, enabled=enabled)
    config, index = tmp_path / ".git/config", _index_path(tmp_path)
    before = {path: _snapshot(path) for path in (config, index)}
    report = reconcile_project_line_endings(tmp_path, data, mode=LineEndingMode.INSPECT)
    assert report.status is LineEndingStatus.UNSAFE
    assert any(
        o.code is LineEndingObservationCode.LOCAL_POLICY_MISSING
        for o in report.repositories[0].observations
    )
    assert {path: _snapshot(path) for path in before} == before
    attrs = tmp_path / ".git/info/attributes"
    assert not attrs.exists()
    first = reconcile_project_line_endings(tmp_path, data, mode=LineEndingMode.REPAIR)
    assert first.status is LineEndingStatus.SAFE
    snapshot = _snapshot(attrs)
    second = reconcile_project_line_endings(tmp_path, data, mode=LineEndingMode.REPAIR)
    assert second.status is LineEndingStatus.SAFE
    assert second.repositories[0].actions == ()
    assert _snapshot(attrs) == snapshot


@pytest.mark.parametrize(
    "name,content,tracked",
    [
        (".gitattributes", b"* text eol=crlf\n", True),
        ("sub/.gitattributes", b"*.bat -text\n", True),
        ("sub/.gitattributes", b"*.txt text eol=crlf\n", False),
        ("ignored/.gitattributes", b"[attr]binary -diff -merge -text\n*.dat binary\n", False),
        ("sub/.gitattributes", b"*.nonexistent export-ignore\n", True),
        ("sub/.gitattributes", b"*.txt !text !eol\n", True),
    ],
)
def test_stealth_preserves_all_upstream_attribute_policies(tmp_path: Path, name, content, tracked):
    data = _stealth_repo(tmp_path)
    _commit_file(tmp_path, "run.bat", b"@echo off\r\n")
    _commit_file(tmp_path, "sub/a.txt", b"a\n")
    _commit_file(tmp_path, ".gitignore", b".booley_project/\nignored/\n")
    path = tmp_path / name
    path.parent.mkdir(exist_ok=True)
    if tracked:
        _commit_file(tmp_path, name, content)
    else:
        path.write_bytes(content)
    before = _snapshot(path)
    attributes = _git(
        tmp_path, "check-attr", "text", "eol", "--", "run.bat", "sub/a.txt", ".gitignore"
    ).stdout
    report = reconcile_project_line_endings(tmp_path, data, mode=LineEndingMode.REPAIR)
    assert report.status is LineEndingStatus.SAFE
    assert not (tmp_path / ".git/info/attributes").exists()
    assert _snapshot(path) == before
    assert (
        _git(
            tmp_path, "check-attr", "text", "eol", "--", "run.bat", "sub/a.txt", ".gitignore"
        ).stdout
        == attributes
    )
    assert b".gitignore: text: unspecified" in attributes or name == ".gitattributes"
    assert any(
        o.code is LineEndingObservationCode.UPSTREAM_POLICY
        for o in report.repositories[0].observations
    )


def test_stealth_sparse_index_only_attributes_are_preserved(tmp_path: Path):
    data = _stealth_repo(tmp_path)
    _commit_file(tmp_path, "sub/.gitattributes", b"*.txt text eol=crlf\n")
    assert _git(tmp_path, "update-index", "--skip-worktree", "sub/.gitattributes").returncode == 0
    (tmp_path / "sub/.gitattributes").unlink()
    before = _snapshot(_index_path(tmp_path))
    report = reconcile_project_line_endings(tmp_path, data, mode=LineEndingMode.REPAIR)
    assert report.status is LineEndingStatus.SAFE
    assert not (tmp_path / ".git/info/attributes").exists()
    assert _snapshot(_index_path(tmp_path)) == before


@pytest.mark.parametrize("opt_out", [False, True])
def test_local_default_warns_after_upstream_policy_or_opt_out(tmp_path: Path, opt_out):
    data = _stealth_repo(tmp_path)
    assert (
        reconcile_project_line_endings(tmp_path, data, mode=LineEndingMode.REPAIR).status
        is LineEndingStatus.SAFE
    )
    attrs = tmp_path / ".git/info/attributes"
    before = _snapshot(attrs)
    _commit_file(tmp_path, "sub/.gitattributes", b"*.txt text eol=crlf\n")
    if opt_out:
        (data / "booley.toml").write_text("[stealth]\nenabled = false\n")
    report = reconcile_project_line_endings(tmp_path, data, mode=LineEndingMode.REPAIR)
    assert report.status is LineEndingStatus.UNSAFE
    assert _snapshot(attrs) == before
    assert any(
        o.code is LineEndingObservationCode.LOCAL_POLICY_CONFLICT
        for o in report.repositories[0].observations
    )
    assert _git(tmp_path, "check-attr", "eol", "--", "sub/a.txt").stdout == b"sub/a.txt: eol: lf\n"


def test_old_untracked_default_is_preserved_until_manual_migration(tmp_path: Path):
    data = _stealth_repo(tmp_path)
    leaked = tmp_path / ".gitattributes"
    leaked.write_text(line_endings.GITATTRIBUTES_RULE + "\n")
    before = _snapshot(leaked)
    report = reconcile_project_line_endings(tmp_path, data, mode=LineEndingMode.REPAIR)
    assert report.status is LineEndingStatus.UNSAFE
    assert _snapshot(leaked) == before
    assert any(
        o.code is LineEndingObservationCode.LEAKED_ROOT_POLICY
        for o in report.repositories[0].observations
    )
    assert not (tmp_path / ".git/info/attributes").exists()
    leaked.unlink()
    assert (
        reconcile_project_line_endings(tmp_path, data, mode=LineEndingMode.REPAIR).status
        is LineEndingStatus.SAFE
    )


@pytest.mark.parametrize(
    "content",
    [b"* text=auto eol=lf\n", b"*.txt text eol=crlf\n", b"[attr]custom -text\n*.dat custom\n"],
)
def test_existing_local_policy_is_preserved(tmp_path: Path, content):
    data = _stealth_repo(tmp_path)
    attrs = tmp_path / ".git/info/attributes"
    attrs.write_bytes(content)
    before = _snapshot(attrs)
    report = reconcile_project_line_endings(tmp_path, data, mode=LineEndingMode.REPAIR)
    assert report.status is LineEndingStatus.SAFE
    assert _snapshot(attrs) == before


def test_local_comments_without_final_newline_preserve_bytes_and_mode(tmp_path: Path):
    data = _stealth_repo(tmp_path)
    attrs = tmp_path / ".git/info/attributes"
    attrs.write_bytes(b"# custom comment")
    attrs.chmod(0o600)
    report = reconcile_project_line_endings(tmp_path, data, mode=LineEndingMode.REPAIR)
    assert report.status is LineEndingStatus.SAFE
    assert attrs.read_bytes() == b"* text=auto eol=lf\n# custom comment\n"
    assert attrs.stat().st_mode & 0o777 == 0o600


def test_linked_worktree_uses_shared_common_attributes(tmp_path: Path):
    root = tmp_path / "main"
    _stealth_repo(root)
    sibling = tmp_path / "linked"
    assert _git(root, "worktree", "add", "-qb", "linked", str(sibling)).returncode == 0
    data = sibling / ".booley_project"
    data.mkdir()
    (data / "booley.toml").write_text("[stealth]\nenabled = true\n")
    report = reconcile_project_line_endings(sibling, data, mode=LineEndingMode.REPAIR)
    assert report.status is LineEndingStatus.SAFE
    assert (root / ".git/info/attributes").exists()
    assert not (sibling / ".gitattributes").exists()
    for worktree in (root, sibling):
        assert (
            _git(worktree, "check-attr", "eol", "--", ".gitignore").stdout
            == b".gitignore: eol: lf\n"
        )
        assert _git(worktree, "status", "--porcelain").stdout == b""


def test_hostile_git_location_environment_cannot_redirect_repair(tmp_path: Path, monkeypatch):
    root, foreign = tmp_path / "project", tmp_path / "foreign"
    data = _stealth_repo(root)
    _stealth_repo(foreign)
    before = {
        _index_path(foreign): _snapshot(_index_path(foreign)),
        foreign / ".git/config": _snapshot(foreign / ".git/config"),
    }
    for key, value in {
        "GIT_DIR": foreign / ".git",
        "GIT_COMMON_DIR": foreign / ".git",
        "GIT_WORK_TREE": foreign,
        "GIT_INDEX_FILE": _index_path(foreign),
    }.items():
        monkeypatch.setenv(key, str(value))
    report = reconcile_project_line_endings(root, data, mode=LineEndingMode.REPAIR)
    assert report.status is LineEndingStatus.SAFE
    assert (root / ".git/info/attributes").exists()
    assert not (foreign / ".git/info/attributes").exists()
    assert {path: _snapshot(path) for path in before} == before


@pytest.mark.parametrize("unsafe", ["file", "parent", "common-query"])
def test_local_destination_safety_failures_never_fall_back(tmp_path: Path, unsafe):
    data = _stealth_repo(tmp_path)
    outside = tmp_path.parent / "outside"
    outside.mkdir(exist_ok=True)
    victim = outside / "attributes"
    victim.write_bytes(b"keep\n")
    attrs = tmp_path / ".git/info/attributes"
    if unsafe == "file":
        attrs.symlink_to(victim)
    elif unsafe == "parent":
        (attrs.parent / "exclude").unlink()
        attrs.parent.rmdir()
        attrs.parent.symlink_to(outside, target_is_directory=True)
    with (
        patch.object(line_endings, "_common_attributes", side_effect=ValueError("query failed"))
        if unsafe == "common-query"
        else patch.object(line_endings, "_error_text", wraps=line_endings._error_text)
    ):
        report = reconcile_project_line_endings(tmp_path, data, mode=LineEndingMode.REPAIR)
    assert report.status is LineEndingStatus.UNSAFE
    assert not (tmp_path / ".gitattributes").exists()
    assert victim.read_bytes() == b"keep\n"


@pytest.mark.parametrize("change", ["target", "upstream", "index", "parent"])
def test_local_publication_revalidates_racing_policy_inputs(tmp_path: Path, change):
    data = _stealth_repo(tmp_path)
    attrs = tmp_path / ".git/info/attributes"
    if change == "parent":
        (attrs.parent / "exclude").unlink()
        attrs.parent.rmdir()
    real_actions = line_endings._repair_actions

    def race(plan):
        if change == "target":
            attrs.write_bytes(b"*.txt -text\n")
        elif change == "upstream":
            (tmp_path / ".gitattributes").write_bytes(b"*.txt -text\n")
        elif change == "index":
            blob = (
                _git(tmp_path, "hash-object", "-w", "--stdin", input_bytes=b"*.txt -text\n")
                .stdout.decode()
                .strip()
            )
            assert (
                _git(
                    tmp_path,
                    "update-index",
                    "--add",
                    "--cacheinfo",
                    "100644",
                    blob,
                    ".gitattributes",
                ).returncode
                == 0
            )
        else:
            attrs.parent.mkdir()
        return real_actions(plan)

    with patch.object(line_endings, "_repair_actions", side_effect=race):
        report = reconcile_project_line_endings(tmp_path, data, mode=LineEndingMode.REPAIR)
    if change == "parent":
        assert report.status is LineEndingStatus.SAFE
        assert attrs.read_bytes() == b"* text=auto eol=lf\n"
    else:
        assert report.status is LineEndingStatus.UNSAFE
        assert not attrs.exists() or attrs.read_bytes() == b"*.txt -text\n"


def test_unrelated_local_attribute_lines_are_byte_preserved(tmp_path: Path):
    data = _stealth_repo(tmp_path)
    attrs = tmp_path / ".git/info/attributes"
    attrs.write_bytes(b"*.tar export-ignore")
    attrs.chmod(0o600)
    assert (
        reconcile_project_line_endings(tmp_path, data, mode=LineEndingMode.REPAIR).status
        is LineEndingStatus.SAFE
    )
    assert attrs.read_bytes() == b"* text=auto eol=lf\n*.tar export-ignore\n"
    assert attrs.stat().st_mode & 0o777 == 0o600


def test_crlf_index_blob_remains_unchanged_and_meaningful_dirt_is_unsafe(tmp_path: Path):
    data = _stealth_repo(tmp_path)
    _commit_file(tmp_path, "a.txt", b"alpha\r\nbeta\r\n")
    before = _git(tmp_path, "show", ":a.txt").stdout
    assert before == b"alpha\r\nbeta\r\n"
    report = reconcile_project_line_endings(tmp_path, data, mode=LineEndingMode.REPAIR)
    assert _git(tmp_path, "show", ":a.txt").stdout == before
    (tmp_path / "a.txt").write_bytes(b"changed content\r\n")
    report = reconcile_project_line_endings(tmp_path, data, mode=LineEndingMode.INSPECT)
    assert report.status is LineEndingStatus.UNSAFE
    assert (tmp_path / "a.txt").read_bytes() == b"changed content\r\n"
    assert _git(tmp_path, "diff", "--quiet").returncode == 1


@pytest.mark.parametrize("kind", ["directory", "symlink", "unreadable"])
def test_unsafe_upstream_attributes_refuse_local_default(tmp_path, kind):
    data = _stealth_repo(tmp_path)
    attrs = tmp_path / "sub/.gitattributes"
    attrs.parent.mkdir()
    if kind == "directory":
        attrs.mkdir()
    elif kind == "symlink":
        victim = tmp_path / "policy.txt"
        victim.write_bytes(b"*.txt -text\n")
        attrs.symlink_to(victim)
    else:
        attrs.write_bytes(b"*.txt -text\n")
    real_read = Path.read_bytes

    def unreadable(path):
        if path == attrs:
            raise PermissionError("fixture unreadable")
        return real_read(path)

    with (
        patch.object(Path, "read_bytes", new=unreadable)
        if kind == "unreadable"
        else patch.object(line_endings, "_error_text", wraps=line_endings._error_text)
    ):
        report = reconcile_project_line_endings(tmp_path, data, mode=LineEndingMode.REPAIR)
    assert report.status is LineEndingStatus.UNSAFE
    assert not (tmp_path / ".git/info/attributes").exists()
    assert not (tmp_path / ".gitattributes").exists()


def test_local_publication_revalidates_normalized_comment_only_root_attributes(tmp_path):
    data = _stealth_repo(tmp_path)
    assert _git(tmp_path, "config", "core.autocrlf", "true").returncode == 0
    _commit_file(tmp_path, ".gitattributes", b"# upstream comment\n")
    (tmp_path / ".gitattributes").unlink()
    assert _git(tmp_path, "checkout", "--", ".gitattributes").returncode == 0
    assert b"\r\n" in (tmp_path / ".gitattributes").read_bytes()
    before = _git(tmp_path, "show", ":.gitattributes").stdout
    report = reconcile_project_line_endings(tmp_path, data, mode=LineEndingMode.REPAIR)
    assert report.status is LineEndingStatus.SAFE
    assert (tmp_path / ".gitattributes").read_bytes() == b"# upstream comment\n"
    assert _git(tmp_path, "show", ":.gitattributes").stdout == before
    assert (tmp_path / ".git/info/attributes").read_bytes() == b"* text=auto eol=lf\n"

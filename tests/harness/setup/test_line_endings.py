"""Boundary-level contracts for Project line-ending reconciliation."""

from __future__ import annotations

import os
import struct
import subprocess
import time
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
from tests.subprocess_budget import SUBPROCESS_TIMEOUT_S

pytestmark = pytest.mark.usefixtures("isolated_git_attributes")


def _git(root: Path, *args: str, input_bytes: bytes | None = None) -> subprocess.CompletedProcess:
    # Kept on subprocess.run: needs stdin (`hash-object --stdin`) and binary output.
    return subprocess.run(
        ["git", "-C", str(root), *args],
        input=input_bytes,
        capture_output=True,
        check=False,
        timeout=SUBPROCESS_TIMEOUT_S,
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


def _normalize_action(
    report: line_endings.LineEndingReport,
) -> line_endings.LineEndingActionResult:
    return next(
        action
        for action in report.repositories[0].actions
        if action.kind is LineEndingActionKind.NORMALIZE_FILES
    )


def _recheckout_again(root: Path, name: str) -> None:
    """Delete and let Git (autocrlf=true) check one tracked path out again, now with CRLF."""
    (root / name).unlink()
    assert _git(root, "checkout", "--", name).returncode == 0


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
    normalize = _normalize_action(report)
    assert normalize.state is LineEndingActionState.REFUSED
    assert normalize.reason is line_endings.LineEndingRefusalReason.NOT_LINE_ENDING_ONLY


def test_clean_baseline_never_discards_later_content_edits(tmp_path: Path):
    for ending in (b"\r\n", b"\n"):
        root = tmp_path / ("crlf" if ending == b"\r\n" else "lf")
        _crlf_repo(root)
        _commit_file(root, "b.v", b"module b;\nendmodule\n")
        _recheckout_again(root, "b.v")
        baseline = line_endings.sample_worktree_cleanliness(root)
        assert baseline[root] is True
        edited = b"module a;\r\n  localparam KEEP = 1;" + ending + b"endmodule\r\n"
        (root / "a.v").write_bytes(edited)
        index_before = _git(root, "ls-files", "--stage", "-z").stdout

        report = reconcile_project_line_endings(
            root, mode=LineEndingMode.REPAIR, clean_baseline=baseline
        )

        normalize = _normalize_action(report)
        assert report.status is LineEndingStatus.UNSAFE
        assert normalize.state is LineEndingActionState.REFUSED
        assert normalize.reason is line_endings.LineEndingRefusalReason.NOT_LINE_ENDING_ONLY
        assert normalize.count == 2
        assert "normalized 1" in (normalize.detail or "")
        assert (root / "a.v").read_bytes() == edited
        assert (root / "b.v").read_bytes() == b"module b;\nendmodule\n"
        assert _git(root, "ls-files", "--stage", "-z").stdout == index_before


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


def test_crlf_attribute_added_after_planning_keeps_crlf(tmp_path: Path):
    _crlf_repo(tmp_path)
    before = _snapshot(tmp_path / "a.v")
    real_actions = line_endings._repair_actions

    def add_attribute_then_apply(plan):
        info_attributes = tmp_path / ".git" / "info" / "attributes"
        info_attributes.parent.mkdir(exist_ok=True)
        info_attributes.write_text("a.v text eol=crlf\n", encoding="utf-8")
        return real_actions(plan)

    with patch.object(line_endings, "_repair_actions", side_effect=add_attribute_then_apply):
        reconcile_project_line_endings(tmp_path, None, mode=LineEndingMode.REPAIR)

    # A re-checkout would rewrite the bytes and the mtime: the fresh scan must see the rule.
    assert _snapshot(tmp_path / "a.v") == before


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


def test_refused_normalization_withholds_the_autocrlf_flip_and_leaves_no_phantom_diffs(
    tmp_path: Path,
):
    # Flipping core.autocrlf=true to false over a CRLF checkout that stays would make Git
    # report every such file as modified; with no attributes rule nothing masks that.
    _crlf_repo(tmp_path)
    (tmp_path / "a.v").write_bytes(b"module a;\r\nendmodule\r\n// local edit\r\n")

    report = reconcile_project_line_endings(tmp_path, None, mode=LineEndingMode.REPAIR)

    pin = next(
        action
        for action in report.repositories[0].actions
        if action.kind is LineEndingActionKind.PIN_AUTOCRLF
    )
    assert pin.state is LineEndingActionState.PLANNED
    assert _local_autocrlf(tmp_path) == "true"
    assert _normalize_action(report).state is LineEndingActionState.REFUSED


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


def test_hostile_git_location_environment_cannot_redirect_repair(tmp_path: Path, monkeypatch):
    root, foreign = tmp_path / "project", tmp_path / "foreign"
    _init(root, autocrlf="true")
    _init(foreign, autocrlf="true")
    _commit_file(foreign, "a.v", b"module a;\nendmodule\n")
    foreign_config = foreign / ".git/config"
    before = {
        _index_path(foreign): _snapshot(_index_path(foreign)),
        foreign_config: _snapshot(foreign_config),
    }
    for key, value in {
        "GIT_DIR": foreign / ".git",
        "GIT_COMMON_DIR": foreign / ".git",
        "GIT_WORK_TREE": foreign,
        "GIT_INDEX_FILE": _index_path(foreign),
    }.items():
        monkeypatch.setenv(key, str(value))
    report = reconcile_project_line_endings(root, None, mode=LineEndingMode.REPAIR)
    assert report.status is LineEndingStatus.SAFE
    # Read the configs directly: the patched GIT_* variables would redirect the `_git` helper.
    assert b"autocrlf = false" in (root / ".git/config").read_bytes()
    assert b"autocrlf = true" in foreign_config.read_bytes()
    assert {path: _snapshot(path) for path in before} == before


def test_init_repair_attribute_queries_ignore_attribute_source_oid(tmp_path, monkeypatch):
    root = _crlf_repo(tmp_path / "root")
    pin = _git(root, "rev-parse", "HEAD").stdout.strip().decode()
    _commit_file(root, ".gitattributes", b"a.v text eol=crlf\n")
    poison = _git(root, "rev-parse", "HEAD^{tree}").stdout.strip().decode()
    assert _git(root, "reset", "--hard", pin).returncode == 0
    (root / "a.v").unlink()
    assert _git(root, "checkout", "--", "a.v").returncode == 0
    # Honouring the poisoned attribute source would exclude a.v as an `eol=crlf` path.
    monkeypatch.setenv("GIT_ATTR_SOURCE", poison)
    report = reconcile_project_line_endings(root, None, mode=LineEndingMode.REPAIR)
    assert report.status is LineEndingStatus.SAFE
    assert (root / "a.v").read_bytes() == b"module a;\nendmodule\n"


def test_reconciliation_never_inventories_attributes_sources(tmp_path, monkeypatch):
    root = tmp_path / "project"
    _init(root, autocrlf="true")
    _commit_file(root, "a.v", b"module a;\nendmodule\n")
    _commit_file(root, ".gitattributes", b"*.png binary\n")
    linked = tmp_path / "linked"
    assert _git(root, "worktree", "add", "-q", str(linked), "-b", "side").returncode == 0
    data = root / ".booley"
    data.mkdir()
    (data / "booley.toml").write_text("[stealth]\nenabled = true\n")
    calls: list[tuple[str, ...]] = []
    real_run = subprocess.run

    def recording_run(command, *args, **kwargs):
        calls.append(tuple(command))
        return real_run(command, *args, **kwargs)

    def forbidden_walk(*_args, **_kwargs):
        raise AssertionError("reconciliation must not walk the tree for attributes")

    monkeypatch.setattr(line_endings.subprocess, "run", recording_run)
    monkeypatch.setattr(line_endings.os, "walk", forbidden_walk)
    for mode in (LineEndingMode.INSPECT, LineEndingMode.REPAIR):
        calls.clear()
        reconcile_project_line_endings(root, data, mode=mode)
        assert calls
        for command in calls:
            assert "worktree" not in command
            assert "var" not in command
            assert "cat-file" not in command
            assert "core.attributesFile" not in command


def _local_autocrlf(root: Path) -> str:
    return os.fsdecode(_git(root, "config", "--local", "--get", "core.autocrlf").stdout).strip()


def _local_eol(root: Path) -> str:
    return os.fsdecode(_git(root, "config", "--local", "--get", "core.eol").stdout).strip()


def test_settings_repair_pins_autocrlf_on_a_tree_without_crlf(tmp_path: Path):
    _init(tmp_path, autocrlf="true")
    _commit_file(tmp_path, "a.v", b"module a;\nendmodule\n")
    (tmp_path / ".booley").mkdir()  # Stealth on: Booley must still write no attributes file
    (tmp_path / ".booley/booley.toml").write_text("[stealth]\nenabled = true\n")

    report = reconcile_project_line_endings(tmp_path, None, mode=LineEndingMode.REPAIR_SETTINGS)

    completed = {
        action.kind
        for repository in report.repositories
        for action in repository.actions
        if action.state is LineEndingActionState.COMPLETED
    }
    assert completed == {LineEndingActionKind.PIN_AUTOCRLF}
    assert report.status is LineEndingStatus.SAFE
    assert _local_autocrlf(tmp_path) == "false"
    assert _local_eol(tmp_path) == "lf"
    assert not (tmp_path / ".git" / "info" / "attributes").exists()
    assert not (tmp_path / ".gitattributes").exists()


def test_settings_repair_leaves_crlf_checkouts_and_their_settings_alone(tmp_path: Path):
    _crlf_repo(tmp_path)
    before = (tmp_path / "a.v").read_bytes()

    report = reconcile_project_line_endings(tmp_path, None, mode=LineEndingMode.REPAIR_SETTINGS)

    (repository,) = report.repositories
    assert {(action.kind, action.state) for action in repository.actions} == {
        (LineEndingActionKind.PIN_AUTOCRLF, LineEndingActionState.PLANNED),
        (LineEndingActionKind.NORMALIZE_FILES, LineEndingActionState.PLANNED),
    }
    assert report.status is LineEndingStatus.UNSAFE
    assert (tmp_path / "a.v").read_bytes() == before
    assert _local_autocrlf(tmp_path) == "true"
    assert _local_eol(tmp_path) == ""
    assert not (tmp_path / ".git" / "info" / "attributes").exists()


def _multi_crlf_repo(root: Path, count: int) -> list[str]:
    """``count`` tracked files checked out with CRLF by Git itself (autocrlf=true)."""
    _init(root, autocrlf="true")
    names = [f"m{index}.v" for index in range(count)]
    for name in names:
        _commit_file(root, name, f"module {name[:-2]};\nendmodule\n".encode())
    for name in names:
        (root / name).unlink()
    assert _git(root, "checkout", "--", *names).returncode == 0
    return names


def test_repair_rechecks_out_aged_stat_clean_crlf_paths(tmp_path: Path):
    names = _multi_crlf_repo(tmp_path, 3)
    aged = (tmp_path / names[0]).stat().st_mtime - 3600
    for name in names:
        os.utime(tmp_path / name, (aged, aged))
    # Refresh the index at the aged mtimes: the entries are stat-clean and the index is newer.
    assert _git(tmp_path, "update-index", "--refresh").returncode == 0
    crlf = b"keep\r\nme\r\n"
    (tmp_path / ".gitignore").write_bytes(b"ignored.log\n")
    (tmp_path / "ignored.log").write_bytes(crlf)
    (tmp_path / "untracked.txt").write_bytes(crlf)

    report = reconcile_project_line_endings(tmp_path, None, mode=LineEndingMode.REPAIR)

    assert report.status is LineEndingStatus.SAFE
    for name in names:
        assert (tmp_path / name).read_bytes() == f"module {name[:-2]};\nendmodule\n".encode()
    assert _git(tmp_path, "status", "--porcelain", "--untracked-files=no").stdout == b""
    assert (tmp_path / "ignored.log").read_bytes() == crlf
    assert (tmp_path / "untracked.txt").read_bytes() == crlf


@pytest.mark.skipif(os.name == "nt", reason="the executable bit is not tracked on Windows")
def test_mode_change_after_baseline_is_left_untouched(tmp_path: Path):
    _crlf_repo(tmp_path)
    baseline = line_endings.sample_worktree_cleanliness(tmp_path)
    assert baseline[tmp_path] is True
    (tmp_path / "a.v").chmod(0o755)
    before = _snapshot(tmp_path / "a.v")

    report = reconcile_project_line_endings(
        tmp_path, mode=LineEndingMode.REPAIR, clean_baseline=baseline
    )

    normalize = _normalize_action(report)
    assert normalize.state is LineEndingActionState.REFUSED
    assert normalize.reason is line_endings.LineEndingRefusalReason.NOT_LINE_ENDING_ONLY
    assert "'a.v' (changed)" in (normalize.detail or "")
    assert _snapshot(tmp_path / "a.v") == before


@pytest.mark.parametrize("flag", ["assume-unchanged", "skip-worktree"])
def test_git_protected_crlf_path_refuses_normalization(tmp_path: Path, flag: str):
    _crlf_repo(tmp_path)
    assert _git(tmp_path, "update-index", f"--{flag}", "a.v").returncode == 0
    edited = b"module a;\r\n  localparam int KEEP = 1;\r\nendmodule\r\n"
    (tmp_path / "a.v").write_bytes(edited)

    report = reconcile_project_line_endings(tmp_path, None, mode=LineEndingMode.REPAIR)

    normalize = _normalize_action(report)
    assert normalize.state is LineEndingActionState.REFUSED
    assert normalize.reason is line_endings.LineEndingRefusalReason.PROTECTED_PATHS
    assert "Git-protected" in (normalize.detail or "")
    assert report.status is LineEndingStatus.UNSAFE
    assert (tmp_path / "a.v").read_bytes() == edited


def test_native_crlf_eol_with_text_auto_is_pinned_and_normalized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # Git for Windows' native core.eol=crlf, emulated in the isolated global config.
    (tmp_path / "isolated-git.config").write_bytes(b"[core]\n\teol = crlf\n")
    root = tmp_path / "repo"
    _init(root, autocrlf="false")
    _commit_file(root, ".gitattributes", b"* text=auto\n*.bat text eol=crlf\n")
    _commit_file(root, "a.v", b"module a;\nendmodule\n")
    _commit_file(root, "b.bat", b"@echo off\n")
    for name in ("a.v", "b.bat"):
        (root / name).unlink()
    assert _git(root, "checkout", "--", "a.v", "b.bat").returncode == 0
    assert (root / "a.v").read_bytes() == b"module a;\r\nendmodule\r\n"

    report = reconcile_project_line_endings(root, None, mode=LineEndingMode.REPAIR)

    assert report.status is LineEndingStatus.SAFE
    assert _local_eol(root) == "lf"
    assert (root / "a.v").read_bytes() == b"module a;\nendmodule\n"
    assert (root / "b.bat").read_bytes() == b"@echo off\r\n"
    assert _git(root, "status", "--porcelain", "--untracked-files=no").stdout == b""


def _gettext_catalog(messages: dict[str, str]) -> bytes:
    """A minimal little-endian GNU ``.mo`` file."""
    keys = sorted(messages)
    count = len(keys)
    original_table = 28
    translation_table = original_table + 8 * count
    strings = translation_table + 8 * count
    blob = b""
    tables: list[list[tuple[int, int]]] = [[], []]
    for position, source in enumerate((keys, [messages[key] for key in keys])):
        for text in source:
            raw = text.encode()
            tables[position].append((len(raw), strings + len(blob)))
            blob += raw + b"\0"
    header = struct.pack("<7I", 0x950412DE, 0, count, original_table, translation_table, 0, 0)
    entries = b"".join(struct.pack("<2I", *entry) for table in tables for entry in table)
    return header + entries + blob


def test_non_repository_is_skipped_under_a_localized_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    catalog = tmp_path / "locale" / "de" / "LC_MESSAGES"
    catalog.mkdir(parents=True)
    translations = {
        "not a git repository (or any of the parent directories): %s": "kein Git-Repository: %s",
        "not a git repository (or any parent up to mount point %s)\n"
        "Stopping at filesystem boundary (GIT_DISCOVERY_ACROSS_FILESYSTEM not set).": (
            "kein Git-Repository (Dateisystemgrenze bei %s)"
        ),
        "fatal: ": "fatal: ",
    }
    (catalog / "git.mo").write_bytes(_gettext_catalog(translations))
    monkeypatch.setenv("GIT_TEXTDOMAINDIR", str(tmp_path / "locale"))
    monkeypatch.setenv("LANGUAGE", "de")
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    monkeypatch.delenv("LC_ALL", raising=False)
    monkeypatch.delenv("LC_MESSAGES", raising=False)
    plain = tmp_path / "plain"
    plain.mkdir()
    raw = subprocess.run(
        ["git", "-C", str(plain), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=False,
        timeout=SUBPROCESS_TIMEOUT_S,
    )
    if "kein Git-Repository" not in raw.stderr:
        pytest.skip(
            f"needs a gettext-enabled Git and an installed en_US.UTF-8 locale: {raw.stderr}"
        )

    report = reconcile_project_line_endings(plain, None, mode=LineEndingMode.INSPECT)

    assert report.discovery_failures == ()
    assert report.status is LineEndingStatus.NOT_APPLICABLE


def _git_process_count(root: Path) -> tuple[int, line_endings.LineEndingReport]:
    real_run = subprocess.run
    count = 0

    def counting(*args, **kwargs):
        nonlocal count
        if args[0][:1] == ["git"]:
            count += 1
        return real_run(*args, **kwargs)

    with patch.object(subprocess, "run", side_effect=counting):
        report = reconcile_project_line_endings(root, None, mode=LineEndingMode.REPAIR)
    return count, report


def test_repair_git_process_count_is_independent_of_crlf_file_count(tmp_path: Path):
    _multi_crlf_repo(tmp_path / "few", 2)
    _multi_crlf_repo(tmp_path / "many", 8)

    few_count, few = _git_process_count(tmp_path / "few")
    many_count, many = _git_process_count(tmp_path / "many")

    assert few.status is LineEndingStatus.SAFE
    assert many.status is LineEndingStatus.SAFE
    assert many_count == few_count, f"{few_count} Git processes for 2 files, {many_count} for 8"


def _fail_git(*subcommands: str, stderr: bytes = b"simulated failure"):
    """A ``subprocess.run`` stand-in failing every Git command that names one of ``subcommands``."""
    real_run = subprocess.run

    def run(*args, **kwargs):
        command = args[0]
        if any(name in command for name in subcommands):
            return subprocess.CompletedProcess(command, 128, b"", stderr)
        return real_run(*args, **kwargs)

    return run


def test_failed_recheckout_is_reported_failed_and_unsafe(tmp_path: Path):
    _crlf_repo(tmp_path)
    before = _snapshot(tmp_path / "a.v")

    with patch.object(subprocess, "run", side_effect=_fail_git("checkout-index")):
        report = reconcile_project_line_endings(tmp_path, None, mode=LineEndingMode.REPAIR)

    normalize = _normalize_action(report)
    assert normalize.state is LineEndingActionState.FAILED
    assert "simulated failure" in (normalize.detail or "")
    assert "git checkout -- <path>" in (normalize.detail or "")
    assert report.status is LineEndingStatus.UNSAFE
    # The shifted mtime is put back for a path Git never rewrote.
    assert _snapshot(tmp_path / "a.v") == before


@pytest.mark.skipif(os.name == "nt", reason="needs a read-only directory")
def test_partial_recheckout_failure_leaves_no_phantom_modifications(tmp_path: Path):
    # Git rewrites every path it can and writes no index when one path cannot be replaced.
    _crlf_repo(tmp_path)
    _commit_file(tmp_path, "locked/b.v", b"module b;\nendmodule\n")
    (tmp_path / "locked" / "b.v").unlink()
    assert _git(tmp_path, "checkout", "--", "locked/b.v").returncode == 0
    # No text rule covers these files: keep their index entries out of Git's same-second
    # "racily clean" window so status compares only the refreshed stat data.
    _wait_for_next_second()
    locked = tmp_path / "locked"
    locked.chmod(0o555)
    try:
        if os.access(locked, os.W_OK):
            pytest.skip("directory permissions are not enforced for this user")
        failed = reconcile_project_line_endings(tmp_path, None, mode=LineEndingMode.REPAIR)
    finally:
        locked.chmod(0o755)

    assert _normalize_action(failed).state is LineEndingActionState.FAILED
    assert _git(tmp_path, "status", "--porcelain", "--untracked-files=no").stdout == b""
    # The next run repairs what is left: the tree is not stuck as dirty.
    retry = reconcile_project_line_endings(tmp_path, None, mode=LineEndingMode.REPAIR)
    assert retry.status is LineEndingStatus.SAFE
    assert (tmp_path / "locked" / "b.v").read_bytes() == b"module b;\nendmodule\n"


def _wait_for_next_second() -> None:
    """Git compares ctime in whole seconds; return once the clock is in a later one."""
    start = int(time.time())
    deadline = time.monotonic() + 5
    while int(time.time()) == start and time.monotonic() < deadline:
        time.sleep(0.05)


@pytest.mark.skipif(os.name == "nt", reason="needs a read-only directory")
@pytest.mark.parametrize(
    "crlf_names",
    [("a.v", "locked/b.v"), ("locked/b.v",)],
    ids=["one-rewritten", "none-rewritten"],
)
def test_failed_recheckout_keeps_crlf_files_stat_clean_without_a_text_rule(
    tmp_path: Path, crlf_names: tuple[str, ...]
):
    # No `text=auto` rule covers these files, so a CRLF file Git did not rewrite would show as
    # modified once its stat is no longer fresh.
    _init(tmp_path)
    assert _git(tmp_path, "config", "core.autocrlf", "true").returncode == 0
    _commit_file(tmp_path, ".gitattributes", b"*.png binary\n")
    for name in ("a.v", "locked/b.v"):
        _commit_file(tmp_path, name, b"module m;\nendmodule\n")
    for name in crlf_names:
        _recheckout_again(tmp_path, name)
    assert _git(tmp_path, "status", "--porcelain", "--untracked-files=no").stdout == b""
    _wait_for_next_second()
    locked = tmp_path / "locked"
    locked.chmod(0o555)
    try:
        if os.access(locked, os.W_OK):
            pytest.skip("directory permissions are not enforced for this user")
        failed = reconcile_project_line_endings(tmp_path, None, mode=LineEndingMode.REPAIR)
    finally:
        locked.chmod(0o755)

    assert _normalize_action(failed).state is LineEndingActionState.FAILED
    assert _git(tmp_path, "status", "--porcelain", "--untracked-files=no").stdout == b""
    retry = reconcile_project_line_endings(tmp_path, None, mode=LineEndingMode.REPAIR)
    assert retry.status is LineEndingStatus.SAFE
    assert (tmp_path / "locked" / "b.v").read_bytes() == b"module m;\nendmodule\n"


def test_path_git_removed_and_could_not_write_is_not_an_mtime_failure(tmp_path: Path):
    _crlf_repo(tmp_path)
    real_run = subprocess.run

    def remove_then_fail(*args, **kwargs):
        command = args[0]
        if "checkout-index" in command and "--temp" not in command:
            (tmp_path / "a.v").unlink()  # Git unlinked the path, then could not write it back
            return subprocess.CompletedProcess(command, 1, b"", b"simulated failure")
        return real_run(*args, **kwargs)

    with patch.object(subprocess, "run", side_effect=remove_then_fail):
        report = reconcile_project_line_endings(tmp_path, None, mode=LineEndingMode.REPAIR)

    detail = _normalize_action(report).detail or ""
    assert "simulated failure" in detail
    assert "git checkout -- <path>" in detail
    assert "could not restore the mtime" not in detail


def test_failed_stat_refresh_after_partial_checkout_is_reported(tmp_path: Path):
    _crlf_repo(tmp_path)
    real_run = subprocess.run

    def fail_checkout_then_refresh(*args, **kwargs):
        command = args[0]
        if "checkout-index" in command and "--temp" not in command:
            # Git rewrote the file, then failed.
            (tmp_path / "a.v").write_bytes(b"module a;\nendmodule\n")
            return subprocess.CompletedProcess(command, 1, b"", b"simulated failure")
        if "--index-info" in command:
            return subprocess.CompletedProcess(command, 128, b"", b"index failure")
        return real_run(*args, **kwargs)

    with patch.object(subprocess, "run", side_effect=fail_checkout_then_refresh):
        report = reconcile_project_line_endings(tmp_path, None, mode=LineEndingMode.REPAIR)

    detail = _normalize_action(report).detail or ""
    assert "simulated failure" in detail
    assert "could not refresh the index after the failure" in detail


@pytest.mark.skipif(os.name == "nt", reason="a colon is not valid in a Windows file name")
def test_crlf_path_starting_with_a_colon_is_normalized(tmp_path: Path):
    # "./" keeps Git from reading the leading colon as pathspec magic.
    _init(tmp_path, autocrlf="true")
    _commit_file(tmp_path, "./:a.v", b"module a;\nendmodule\n")
    (tmp_path / ":a.v").unlink()
    assert _git(tmp_path, "checkout", "--", "./:a.v").returncode == 0
    assert b"\r\n" in (tmp_path / ":a.v").read_bytes()

    report = reconcile_project_line_endings(tmp_path, None, mode=LineEndingMode.REPAIR)

    assert report.status is LineEndingStatus.SAFE
    assert (tmp_path / ":a.v").read_bytes() == b"module a;\nendmodule\n"


def test_many_left_paths_are_listed_with_a_count():
    left = {f"f{number}.v": "changed" for number in range(5)}

    detail = line_endings._left_detail(2, left)

    assert detail.startswith("normalized 2 file(s) to LF; left 5 tracked file(s) untouched:")
    assert "'f2.v' (changed), and 2 more" in detail
    assert "f3.v" not in detail


def test_unreadable_index_scan_fails_normalization(tmp_path: Path):
    _crlf_repo(tmp_path)
    real_scan = line_endings._scan_index
    calls = 0

    def fail_second_scan(root: Path):
        nonlocal calls
        calls += 1
        if calls == 2:  # planning scans once; the re-checkout scans again before touching files
            return None, "simulated scan failure"
        return real_scan(root)

    with patch.object(line_endings, "_scan_index", side_effect=fail_second_scan):
        report = reconcile_project_line_endings(tmp_path, None, mode=LineEndingMode.REPAIR)

    normalize = _normalize_action(report)
    assert normalize.state is LineEndingActionState.FAILED
    assert normalize.detail == "simulated scan failure"
    assert report.status is LineEndingStatus.UNSAFE


@pytest.mark.parametrize("failing", ["check-attr", "diff-files"])
def test_unclassifiable_candidates_fail_normalization(tmp_path: Path, failing: str):
    _crlf_repo(tmp_path)
    before = _snapshot(tmp_path / "a.v")

    with patch.object(subprocess, "run", side_effect=_fail_git(failing)):
        report = reconcile_project_line_endings(tmp_path, None, mode=LineEndingMode.REPAIR)

    normalize = _normalize_action(report)
    assert normalize.state is LineEndingActionState.FAILED
    assert failing in (normalize.detail or "")
    assert _snapshot(tmp_path / "a.v") == before


def test_mtime_that_cannot_be_shifted_leaves_the_path_with_its_cause(tmp_path: Path):
    _crlf_repo(tmp_path)
    before = _snapshot(tmp_path / "a.v")

    with patch.object(os, "utime", side_effect=OSError("read-only volume")):
        report = reconcile_project_line_endings(tmp_path, None, mode=LineEndingMode.REPAIR)

    normalize = _normalize_action(report)
    assert normalize.state is LineEndingActionState.REFUSED
    assert normalize.reason is line_endings.LineEndingRefusalReason.NOT_LINE_ENDING_ONLY
    assert "'a.v' (could not update mtime: read-only volume)" in (normalize.detail or "")
    assert _snapshot(tmp_path / "a.v") == before


def test_regular_file_check_names_what_a_candidate_became(tmp_path: Path):
    (tmp_path / "file.v").write_bytes(b"x")
    (tmp_path / "folder").mkdir()

    assert line_endings._regular_file_cause(tmp_path, "file.v") is None
    assert line_endings._regular_file_cause(tmp_path, "folder") == "not a regular file"
    assert "could not read" in (line_endings._regular_file_cause(tmp_path, "gone.v") or "")


def test_unreadable_candidate_is_left_with_its_cause(tmp_path: Path):
    entry = line_endings._IndexEntry(
        "gone.v", "H", "100644", "0" * 40, "0", "i/lf", "w/crlf", False
    )

    assert "could not read" in (line_endings._plain_path_cause(tmp_path, entry) or "")


def test_blob_object_ids_follow_the_repository_hash_algorithm(tmp_path: Path):
    _init(tmp_path)
    for algorithm in ("sha1", "sha256"):
        repository = tmp_path / algorithm
        repository.mkdir()
        assert _git(repository, "init", "-q", f"--object-format={algorithm}").returncode == 0
        expected = _git(repository, "hash-object", "--stdin", input_bytes=b"content\n")
        oid = os.fsdecode(expected.stdout).strip()
        assert line_endings._blob_oid(b"content\n", len(oid)) == oid


def _converted_repo(
    root: Path,
    attributes: bytes,
    content: bytes,
    *,
    autocrlf: str = "true",
    driver: tuple[str, str, str] | None = None,
) -> None:
    """CRLF checkout of ``a.v`` under a repo-local attributes rule (Git does the checkout).

    ``driver`` is a filter's ``(name, clean, smudge)``, configured before the commit.
    """
    _init(root, autocrlf=autocrlf)
    (root / ".git" / "info").mkdir(exist_ok=True)
    (root / ".git" / "info" / "attributes").write_bytes(attributes)
    if driver is not None:
        name, clean, smudge = driver
        for key, value in ((f"filter.{name}.clean", clean), (f"filter.{name}.smudge", smudge)):
            assert _git(root, "config", key, value).returncode == 0
    _commit_file(root, "a.v", content)
    _recheckout_again(root, "a.v")


needs_posix_filters = pytest.mark.skipif(
    os.name == "nt", reason="the filter drivers are POSIX shell commands"
)
_CAT_DRIVER = ("cat", "cat", "cat")


@needs_posix_filters
@pytest.mark.parametrize("driver", ["ident", "filter"])
def test_converted_paths_are_normalized_through_git_clean_conversion(tmp_path: Path, driver: str):
    attributes = b"a.v ident\n" if driver == "ident" else b"a.v filter=cat\n"
    _converted_repo(
        tmp_path,
        attributes,
        b"module a; // $Id$\nendmodule\n",
        driver=_CAT_DRIVER if driver == "filter" else None,
    )
    assert b"\r\n" in (tmp_path / "a.v").read_bytes()

    report = reconcile_project_line_endings(tmp_path, None, mode=LineEndingMode.REPAIR)

    assert report.status is LineEndingStatus.SAFE
    assert b"\r" not in (tmp_path / "a.v").read_bytes()
    assert _git(tmp_path, "status", "--porcelain", "--untracked-files=no").stdout == b""


@needs_posix_filters
def test_filter_that_emits_crlf_is_never_rewritten(tmp_path: Path):
    _converted_repo(
        tmp_path,
        b"a.v filter=crlf -text\n",
        b"module a;\nendmodule\n",
        autocrlf="false",
        driver=("crlf", "tr -d '\\r'", "sed 's/$/\\r/'"),
    )
    assert (tmp_path / "a.v").read_bytes() == b"module a;\r\nendmodule\r\n"
    before = _snapshot(tmp_path / "a.v")

    report = reconcile_project_line_endings(tmp_path, None, mode=LineEndingMode.REPAIR)

    normalize = _normalize_action(report)
    assert normalize.state is LineEndingActionState.REFUSED
    assert normalize.reason is line_endings.LineEndingRefusalReason.NOT_LINE_ENDING_ONLY
    assert "its line endings come from the filter" in (normalize.detail or "")
    assert _snapshot(tmp_path / "a.v") == before


@needs_posix_filters
def test_filter_output_that_differs_beyond_line_endings_is_left(tmp_path: Path):
    # A lossy clean filter: the shouting copy cleans to the stored blob, yet a checkout
    # would bring back the quiet one, so the file differs from Git's copy by more than endings.
    _converted_repo(
        tmp_path,
        b"a.v filter=quiet\n",
        b"module a;\nendmodule\n",
        driver=("quiet", "tr A-Z a-z", "cat"),
    )
    (tmp_path / "a.v").write_bytes(b"MODULE A;\r\nENDMODULE\r\n")
    before = _snapshot(tmp_path / "a.v")

    report = reconcile_project_line_endings(tmp_path, None, mode=LineEndingMode.REPAIR)

    normalize = _normalize_action(report)
    assert "checkout output differs from the file beyond line endings" in (normalize.detail or "")
    assert _snapshot(tmp_path / "a.v") == before


def test_failing_clean_conversion_leaves_converted_paths_with_their_cause(tmp_path: Path):
    _converted_repo(tmp_path, b"a.v ident\n", b"module a; // $Id$\nendmodule\n")
    before = _snapshot(tmp_path / "a.v")

    with patch.object(subprocess, "run", side_effect=_fail_git("hash-object")):
        report = reconcile_project_line_endings(tmp_path, None, mode=LineEndingMode.REPAIR)

    normalize = _normalize_action(report)
    assert normalize.state is LineEndingActionState.REFUSED
    assert "'a.v' (clean conversion failed: " in (normalize.detail or "")
    assert _snapshot(tmp_path / "a.v") == before


def _cat_filter_repo(root: Path) -> None:
    _converted_repo(root, b"a.v filter=cat\n", b"module a;\nendmodule\n", driver=_CAT_DRIVER)


@needs_posix_filters
@pytest.mark.parametrize("failure", ["exit", "incomplete"])
def test_failing_smudge_probe_leaves_filtered_paths_with_their_cause(tmp_path: Path, failure: str):
    _cat_filter_repo(tmp_path)
    before = _snapshot(tmp_path / "a.v")
    if failure == "exit":
        patched = _fail_git("--temp")
    else:
        real_run = subprocess.run

        def patched(*args, **kwargs):
            if "--temp" in args[0]:
                return subprocess.CompletedProcess(args[0], 0, b"", b"")
            return real_run(*args, **kwargs)

    with patch.object(subprocess, "run", side_effect=patched):
        report = reconcile_project_line_endings(tmp_path, None, mode=LineEndingMode.REPAIR)

    normalize = _normalize_action(report)
    assert "'a.v' (filter failed: " in (normalize.detail or "")
    assert _snapshot(tmp_path / "a.v") == before


@needs_posix_filters
@pytest.mark.parametrize("removable", [True, False], ids=["removable", "locked"])
def test_smudge_probe_timeout_removes_its_temporary_files(tmp_path: Path, removable: bool):
    _cat_filter_repo(tmp_path)
    (tmp_path / ".merge_file_keep").write_bytes(b"not ours")
    real_run = subprocess.run
    real_unlink = Path.unlink

    def killed_probe(*args, **kwargs):
        command = args[0]
        if "--temp" not in command:
            return real_run(*args, **kwargs)
        (tmp_path / ".merge_file_new").write_bytes(b"partial")
        raise subprocess.TimeoutExpired(command, 300, output=b".merge_file_new\ta.v\0")

    def locked_unlink(path: Path, missing_ok: bool = False):
        if path.name == ".merge_file_new":
            raise PermissionError("held by a scanner")
        real_unlink(path, missing_ok=missing_ok)

    with patch.object(subprocess, "run", side_effect=killed_probe):
        if removable:
            report = reconcile_project_line_endings(tmp_path, None, mode=LineEndingMode.REPAIR)
        else:
            with patch.object(Path, "unlink", new=locked_unlink):
                report = reconcile_project_line_endings(tmp_path, None, mode=LineEndingMode.REPAIR)

    detail = _normalize_action(report).detail or ""
    assert "'a.v' (filter failed: timed out after 300 s)" in detail
    assert (tmp_path / ".merge_file_keep").read_bytes() == b"not ours"
    assert (tmp_path / ".merge_file_new").exists() is not removable
    assert ("temporary file could not be removed: .merge_file_new" in detail) is not removable


def _phantom_repo(root: Path) -> None:
    """a.v is LF and matches the index, but the index still holds its CRLF checkout's stat."""
    _crlf_repo(root)
    assert _git(root, "config", "core.autocrlf", "false").returncode == 0
    (root / "a.v").write_bytes(b"module a;\nendmodule\n")


@pytest.mark.parametrize("failing", ["unstaged", "staged"])
def test_phantom_diff_failures_are_reported(tmp_path: Path, failing: str):
    _phantom_repo(tmp_path)
    real_run = subprocess.run

    def run(*args, **kwargs):
        command = args[0]
        if "--name-only" in command and ("--cached" in command) is (failing == "staged"):
            return subprocess.CompletedProcess(command, 128, b"", b"simulated diff failure")
        return real_run(*args, **kwargs)

    with patch.object(subprocess, "run", side_effect=run):
        paths, error = line_endings._tracked_phantom_paths(tmp_path)

    assert paths is None
    assert error is not None and "simulated diff failure" in error


@pytest.mark.parametrize("failing", ["--index-info", "--refresh"])
def test_stale_index_refresh_failures_are_reported(tmp_path: Path, failing: str):
    _phantom_repo(tmp_path)

    with patch.object(subprocess, "run", side_effect=_fail_git(failing)):
        report = reconcile_project_line_endings(tmp_path, None, mode=LineEndingMode.REPAIR)

    refresh = next(
        action
        for action in report.repositories[0].actions
        if action.kind is LineEndingActionKind.REFRESH_INDEX
    )
    assert refresh.state is LineEndingActionState.FAILED
    assert "simulated failure" in (refresh.detail or "")
    assert report.status is LineEndingStatus.UNSAFE


def test_stale_index_refresh_scan_failure_is_reported(tmp_path: Path):
    _phantom_repo(tmp_path)
    real_scan = line_endings._scan_index
    calls = 0

    def fail_refresh_scan(root: Path):
        nonlocal calls
        calls += 1
        if calls == 2:  # planning scans once; the refresh scans again for the entries it resets
            return None, "simulated scan failure"
        return real_scan(root)

    with patch.object(line_endings, "_scan_index", side_effect=fail_refresh_scan):
        report = reconcile_project_line_endings(tmp_path, None, mode=LineEndingMode.REPAIR)

    refresh = next(
        action
        for action in report.repositories[0].actions
        if action.kind is LineEndingActionKind.REFRESH_INDEX
    )
    assert refresh.state is LineEndingActionState.FAILED
    assert refresh.detail == "simulated scan failure"


def test_local_eol_reads_unset_set_and_unreadable(tmp_path: Path):
    _init(tmp_path)
    assert line_endings.read_local_eol(tmp_path) == ""
    assert _git(tmp_path, "config", "--local", "core.eol", "crlf").returncode == 0
    assert line_endings.read_local_eol(tmp_path) == "crlf"
    with patch.object(subprocess, "run", side_effect=_fail_git("core.eol")):
        assert line_endings.read_local_eol(tmp_path) is None
    with patch.object(subprocess, "run", side_effect=OSError("unavailable")):
        assert line_endings.read_local_eol(tmp_path) is None


def test_unreadable_local_eol_is_reported_as_unreadable_policy(tmp_path: Path):
    _init(tmp_path)

    with patch.object(subprocess, "run", side_effect=_fail_git("core.eol")):
        report = reconcile_project_line_endings(tmp_path, None, mode=LineEndingMode.INSPECT)

    observation = next(
        item
        for item in report.repositories[0].observations
        if item.code is LineEndingObservationCode.LOCAL_AUTOCRLF_UNREADABLE
    )
    assert observation.detail == "core.eol"
    assert report.status is LineEndingStatus.UNSAFE


def test_eol_pin_is_applied_when_autocrlf_is_pinned_but_crlf_checkouts_exist(tmp_path: Path):
    _crlf_repo(tmp_path)
    assert _git(tmp_path, "config", "core.autocrlf", "false").returncode == 0

    reconcile_project_line_endings(tmp_path, None, mode=LineEndingMode.REPAIR)

    assert _local_eol(tmp_path) == "lf"
    assert _local_autocrlf(tmp_path) == "false"


def test_eol_pin_is_written_on_a_clean_tree_with_autocrlf_already_pinned(tmp_path: Path):
    # A repository an earlier Booley pinned: only core.autocrlf is set, so a native
    # core.eol=crlf would bring CRLF back on the next checkout.
    _init(tmp_path)
    _commit_file(tmp_path, "a.v", b"module a;\nendmodule\n")

    report = reconcile_project_line_endings(tmp_path, None, mode=LineEndingMode.REPAIR)

    assert [action.kind for action in report.repositories[0].actions] == [
        LineEndingActionKind.PIN_AUTOCRLF
    ]
    assert _local_eol(tmp_path) == "lf"
    assert (
        reconcile_project_line_endings(tmp_path, None, mode=LineEndingMode.REPAIR)
        .repositories[0]
        .actions
        == ()
    )


def test_core_eol_changed_after_planning_is_refused_for_that_run(tmp_path: Path):
    _init(tmp_path, autocrlf="true")
    real_actions = line_endings._repair_actions

    def change_eol_then_apply(plan):
        assert _git(tmp_path, "config", "core.eol", "crlf").returncode == 0
        return real_actions(plan)

    with patch.object(line_endings, "_repair_actions", side_effect=change_eol_then_apply):
        report = reconcile_project_line_endings(tmp_path, None, mode=LineEndingMode.REPAIR)

    pin = next(
        action
        for action in report.repositories[0].actions
        if action.kind is LineEndingActionKind.PIN_AUTOCRLF
    )
    assert pin.state is LineEndingActionState.REFUSED
    assert pin.reason is line_endings.LineEndingRefusalReason.SETTING_CHANGED
    assert pin.detail == "core.eol changed during line-ending repair"
    assert report.status is LineEndingStatus.UNSAFE


def test_failed_eol_write_blocks_the_pin(tmp_path: Path):
    _init(tmp_path, autocrlf="true")

    with patch.object(subprocess, "run", side_effect=_fail_git("lf")):  # the write, not the read
        report = reconcile_project_line_endings(tmp_path, None, mode=LineEndingMode.REPAIR)

    pin = next(
        action
        for action in report.repositories[0].actions
        if action.kind is LineEndingActionKind.PIN_AUTOCRLF
    )
    assert pin.state is LineEndingActionState.FAILED
    assert pin.detail is not None and pin.detail.startswith("could not set core.eol: ")
    assert report.status is LineEndingStatus.UNSAFE


def test_unverifiable_pin_is_reported_failed(tmp_path: Path):
    _init(tmp_path, autocrlf="true")
    real_eol = line_endings.read_local_eol
    reads = 0

    def drifting_eol(root: Path):
        nonlocal reads
        reads += 1
        return "crlf" if reads >= 3 else real_eol(root)  # the read after the writes

    with patch.object(line_endings, "read_local_eol", side_effect=drifting_eol):
        report = reconcile_project_line_endings(tmp_path, None, mode=LineEndingMode.REPAIR)

    pin = next(
        action
        for action in report.repositories[0].actions
        if action.kind is LineEndingActionKind.PIN_AUTOCRLF
    )
    assert pin.state is LineEndingActionState.FAILED
    assert "did not verify" in (pin.detail or "")


def test_local_config_write_failures_carry_their_key(tmp_path: Path):
    _init(tmp_path, autocrlf="true")

    with patch.object(subprocess, "run", side_effect=_fail_git("core.autocrlf")):
        failure = line_endings._set_local_config(tmp_path, "core.autocrlf", "false")
    with patch.object(subprocess, "run", side_effect=OSError("git vanished")):
        crash = line_endings._set_local_config(tmp_path, "core.eol", "lf")

    assert failure == "could not set core.autocrlf: simulated failure"
    assert crash == "could not set core.eol: git vanished"


def test_unparseable_index_records_are_skipped():
    assert line_endings._parse_index_record("no tabs at all") is None
    assert line_endings._parse_index_record("H 100644\ti/lf w/crlf\ta.v") is None
    entry = line_endings._parse_index_record(
        "H 100644 abc 0\ti/lf    w/crlf  attr/text eol=crlf\ta\tb.v"
    )
    assert entry is not None
    assert (entry.path, entry.tag, entry.stage, entry.eol_crlf_attr) == ("a\tb.v", "H", "0", True)
    assert not entry.crlf_mismatch


def test_paths_for_git_stdin_are_c_quoted():
    assert line_endings._c_quote(b'a"\\\n\xffz') == b'"a\\042\\134\\012\\377z"'


def test_staged_file_cleanup_reports_what_it_could_not_remove(tmp_path: Path):
    stuck = tmp_path / "stuck"
    stuck.write_bytes(b"x")
    with patch.object(Path, "unlink", side_effect=PermissionError("held")):
        assert line_endings._cleanup_staged_files({"stuck": stuck}) == [stuck]
    assert line_endings._cleanup_staged_files({"gone": tmp_path / "gone"}) == []


def test_unexpected_clean_conversion_output_is_an_error(tmp_path: Path):
    with patch.object(line_endings, "_git_output", return_value=(b"", None)):
        oids, error = line_endings._clean_conversion_oids(tmp_path, ["a.v"])

    assert oids is None
    assert error == "Git returned an unexpected number of object ids"


def test_candidate_that_stopped_being_a_regular_file_is_left(tmp_path: Path):
    _crlf_repo(tmp_path)
    before = _snapshot(tmp_path / "a.v")

    with patch.object(line_endings, "_regular_file_cause", return_value="not a regular file"):
        report = reconcile_project_line_endings(tmp_path, None, mode=LineEndingMode.REPAIR)

    assert "'a.v' (not a regular file)" in (_normalize_action(report).detail or "")
    assert _snapshot(tmp_path / "a.v") == before


def test_recheckout_process_failure_is_reported_failed(tmp_path: Path):
    _crlf_repo(tmp_path)
    real_run = subprocess.run

    def run(*args, **kwargs):
        if "checkout-index" in args[0]:
            raise OSError("git vanished")
        return real_run(*args, **kwargs)

    with patch.object(subprocess, "run", side_effect=run):
        report = reconcile_project_line_endings(tmp_path, None, mode=LineEndingMode.REPAIR)

    normalize = _normalize_action(report)
    assert normalize.state is LineEndingActionState.FAILED
    assert "git vanished" in (normalize.detail or "")


@needs_posix_filters
@pytest.mark.parametrize("failure", ["probe-crash", "read-failure"])
def test_filtered_path_failures_outside_git_are_left_with_their_cause(
    tmp_path: Path, failure: str
):
    _cat_filter_repo(tmp_path)
    real_run = subprocess.run
    real_read = Path.read_bytes

    def run(*args, **kwargs):
        if failure == "probe-crash" and "--temp" in args[0]:
            raise OSError("git vanished")
        return real_run(*args, **kwargs)

    def read_bytes(path: Path):
        if failure == "read-failure" and path.name == "a.v":
            raise OSError("sharing violation")
        return real_read(path)

    with (
        patch.object(subprocess, "run", side_effect=run),
        patch.object(Path, "read_bytes", new=read_bytes),
    ):
        report = reconcile_project_line_endings(tmp_path, None, mode=LineEndingMode.REPAIR)

    expected = "git vanished" if failure == "probe-crash" else "sharing violation"
    assert expected in (_normalize_action(report).detail or "")


def test_unreadable_git_status_refuses_normalization_with_a_typed_reason(tmp_path: Path):
    _crlf_repo(tmp_path)
    before = _snapshot(tmp_path / "a.v")

    with patch.object(line_endings, "_worktree_is_clean", return_value=None):
        report = reconcile_project_line_endings(tmp_path, None, mode=LineEndingMode.REPAIR)

    normalize = _normalize_action(report)
    assert normalize.state is LineEndingActionState.REFUSED
    assert normalize.reason is line_endings.LineEndingRefusalReason.STATUS_UNREADABLE
    assert _snapshot(tmp_path / "a.v") == before


def _crlf_tracked_attributes_repo(root: Path) -> None:
    _crlf_repo(root)
    _commit_file(root, ".gitattributes", b"*.bat -text\n")
    _recheckout_again(root, ".gitattributes")
    assert b"\r\n" in (root / ".gitattributes").read_bytes()


def test_normalized_tracked_root_attributes_get_no_default_rule(tmp_path: Path):
    _crlf_tracked_attributes_repo(tmp_path)

    report = reconcile_project_line_endings(tmp_path, None, mode=LineEndingMode.REPAIR)

    assert report.status is LineEndingStatus.SAFE
    assert (tmp_path / ".gitattributes").read_bytes() == b"*.bat -text\n"

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
    LineEndingRefusalReason,
    LineEndingStatus,
    reconcile_project_line_endings,
)
from tests.git_command_fake import REAL_RUN, GitCommandFake
from tests.subprocess_budget import SUBPROCESS_TIMEOUT_S
from tests.time_budget import hang_guard_s

pytestmark = pytest.mark.usefixtures("isolated_git_attributes")


@pytest.fixture
def git_commands(monkeypatch: pytest.MonkeyPatch) -> GitCommandFake:
    return GitCommandFake.install(monkeypatch)


def _git(root: Path, *args: str, input_bytes: bytes | None = None) -> subprocess.CompletedProcess:
    # Real Git, never through the fake's rules: needs stdin and binary output.
    return REAL_RUN(
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
    commit = _git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", name)
    assert commit.returncode == 0


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


def _action(
    report: line_endings.LineEndingReport,
    kind: LineEndingActionKind = LineEndingActionKind.NORMALIZE_FILES,
) -> line_endings.LineEndingActionResult:
    return next(action for action in report.repositories[0].actions if action.kind is kind)


def _recheckout_again(root: Path, name: str) -> None:
    """Delete and let Git (autocrlf=true) check one tracked path out again, now with CRLF."""
    (root / name).unlink()
    assert _git(root, "checkout", "--", name).returncode == 0


def test_no_repository_is_not_applicable(tmp_path: Path):
    report = reconcile_project_line_endings(tmp_path, None, mode=LineEndingMode.INSPECT)

    assert report.status is LineEndingStatus.NOT_APPLICABLE
    assert report.repositories == ()


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (FileNotFoundError(), "git unavailable"),
        (subprocess.TimeoutExpired(["git"], 10), "Git probe failed"),
        (OSError("permission denied"), "Git probe failed"),
    ],
    ids=["missing", "timeout", "os-error"],
)
def test_repository_discovery_preserves_probe_failure_context(
    tmp_path: Path, git_commands: GitCommandFake, error: BaseException, expected: str
):
    git_commands.crash("rev-parse", error=error)

    discovery = line_endings.discover_line_ending_repositories(tmp_path)

    assert discovery.repositories == ()
    assert len(discovery.failures) == 1
    assert expected in discovery.failures[0].detail


@pytest.mark.parametrize(
    "reader",
    [line_endings.read_autocrlf_setting, line_endings.read_local_eol],
    ids=["autocrlf", "eol"],
)
@pytest.mark.parametrize("failure", ["exit", "crash"])
def test_unreadable_git_config_reads_as_none(
    tmp_path: Path, git_commands: GitCommandFake, reader, failure: str
):
    _init(tmp_path)
    if failure == "exit":
        git_commands.answer("config")
    else:
        git_commands.crash("config", error=OSError("unavailable"))

    assert reader(tmp_path) is None


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


@pytest.mark.parametrize(
    ("change", "status"),
    [
        ("worktree-edit", LineEndingStatus.UNSAFE),
        ("index-change", LineEndingStatus.UNSAFE),
        ("crlf-rule", LineEndingStatus.SAFE),
    ],
)
def test_change_after_planning_leaves_the_file_untouched(
    tmp_path: Path, git_commands: GitCommandFake, change: str, status: LineEndingStatus
):
    _crlf_repo(tmp_path)
    path = tmp_path / "a.v"
    snapshots: list[tuple[bytes, int, int]] = []

    def change_then_snapshot() -> None:
        if change == "worktree-edit":
            path.write_bytes(b"module a;\r\n  localparam KEEP = 1;\r\nendmodule\r\n")
        elif change == "index-change":
            blob = _git(tmp_path, "hash-object", "-w", "--stdin", input_bytes=b"changed index\n")
            oid = os.fsdecode(blob.stdout).strip()
            assert (
                _git(tmp_path, "update-index", "--cacheinfo", "100644", oid, "a.v").returncode == 0
            )
        else:
            info_attributes = tmp_path / ".git" / "info" / "attributes"
            info_attributes.parent.mkdir(exist_ok=True)
            info_attributes.write_bytes(b"a.v text eol=crlf\n")
        snapshots.append(_snapshot(path))

    # ls-files call 1 is planning; call 2 is the repair's own scan, which sees the change.
    git_commands.hook("ls-files", action=change_then_snapshot, call=2)

    report = reconcile_project_line_endings(tmp_path, None, mode=LineEndingMode.REPAIR)

    # A re-checkout would rewrite the bytes and the mtime.
    assert _snapshot(path) == snapshots[0]
    assert report.status is status


@pytest.mark.parametrize(
    "ending", [pytest.param(b"\r\n", id="crlf"), pytest.param(b"\n", id="lf")]
)
def test_clean_baseline_never_discards_later_content_edits(tmp_path: Path, ending: bytes):
    root = _crlf_repo(tmp_path)
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

    normalize = _action(report)
    assert report.status is LineEndingStatus.UNSAFE
    assert normalize.state is LineEndingActionState.REFUSED
    assert normalize.reason is LineEndingRefusalReason.NOT_LINE_ENDING_ONLY
    assert normalize.count == 2
    assert "normalized 1" in (normalize.detail or "")
    assert (root / "a.v").read_bytes() == edited
    assert (root / "b.v").read_bytes() == b"module b;\nendmodule\n"
    assert _git(root, "ls-files", "--stage", "-z").stdout == index_before


@pytest.mark.parametrize("key", ["core.autocrlf", "core.eol"])
def test_setting_changed_after_planning_refuses_the_pin(
    tmp_path: Path, git_commands: GitCommandFake, key: str
):
    _init(tmp_path, autocrlf="true")
    value = "false" if key == "core.autocrlf" else "crlf"

    # Planning reads config three times; call 4 is the pin's first re-read.
    git_commands.hook("config", action=lambda: _git(tmp_path, "config", key, value), call=4)

    report = reconcile_project_line_endings(tmp_path, None, mode=LineEndingMode.REPAIR)

    pin = _action(report, LineEndingActionKind.PIN_AUTOCRLF)
    assert pin.state is LineEndingActionState.REFUSED
    assert pin.reason is LineEndingRefusalReason.SETTING_CHANGED
    assert pin.detail == f"{key} changed during line-ending repair"
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


def test_post_repair_unreadable_inspection_is_unsafe_and_retry_converges(
    tmp_path: Path, git_commands: GitCommandFake
):
    _crlf_repo(tmp_path)
    git_commands.answer("ls-files", call=3)  # the final inspection: planning and repair scan first

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
    assert _action(report).state is LineEndingActionState.REFUSED


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

    normalize = _action(report)
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

    normalize = _action(report)
    assert normalize.state is LineEndingActionState.REFUSED
    assert normalize.reason is line_endings.LineEndingRefusalReason.PROTECTED_PATHS
    assert "Git-protected" in (normalize.detail or "")
    assert report.status is LineEndingStatus.UNSAFE
    assert (tmp_path / "a.v").read_bytes() == edited


def test_native_crlf_eol_with_text_auto_is_pinned_and_normalized(tmp_path: Path):
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


def test_repair_git_process_count_is_independent_of_crlf_file_count(
    tmp_path: Path, git_commands: GitCommandFake
):
    _multi_crlf_repo(tmp_path / "few", 2)
    _multi_crlf_repo(tmp_path / "many", 8)
    counts: list[int] = []
    reports: list[line_endings.LineEndingReport] = []
    for root in (tmp_path / "few", tmp_path / "many"):
        git_commands.commands.clear()
        reports.append(reconcile_project_line_endings(root, None, mode=LineEndingMode.REPAIR))
        counts.append(len(git_commands.commands))

    assert [report.status for report in reports] == [LineEndingStatus.SAFE] * 2
    assert counts[1] == counts[0], f"{counts[0]} Git processes for 2 files, {counts[1]} for 8"


@pytest.mark.parametrize(
    ("failure", "fragments"),
    [
        ("index-scan", ("simulated failure",)),
        ("check-attr", ("check-attr",)),
        ("diff-files", ("diff-files",)),
        (
            "checkout-exit",
            ("simulated failure\N{REPLACEMENT CHARACTER}", "git checkout -- <path>"),
        ),
        ("checkout-crash", ("git vanished", "git checkout -- <path>")),
    ],
)
def test_git_failure_during_normalization_fails_it(
    tmp_path: Path, git_commands: GitCommandFake, failure: str, fragments: tuple[str, ...]
):
    _crlf_repo(tmp_path)
    before = _snapshot(tmp_path / "a.v")
    if failure == "index-scan":
        git_commands.answer("ls-files", call=2)  # call 1 is planning
    elif failure in ("check-attr", "diff-files"):
        git_commands.answer(failure)
    elif failure == "checkout-exit":
        git_commands.answer("checkout-index", "-u", stderr=b"simulated failure\xff")
    else:
        git_commands.crash("checkout-index", "-u", error=OSError("git vanished"))

    report = reconcile_project_line_endings(tmp_path, None, mode=LineEndingMode.REPAIR)

    normalize = _action(report)
    assert normalize.state is LineEndingActionState.FAILED
    for fragment in fragments:
        assert fragment in (normalize.detail or "")
    assert report.status is LineEndingStatus.UNSAFE
    # The shifted mtime is put back for a path Git never rewrote.
    assert _snapshot(tmp_path / "a.v") == before


def _wait_for_next_second() -> None:
    """Git compares ctime in whole seconds; return once the clock is in a later one."""
    start = int(time.time())
    deadline = time.monotonic() + hang_guard_s()
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

    assert _action(failed).state is LineEndingActionState.FAILED
    assert _git(tmp_path, "status", "--porcelain", "--untracked-files=no").stdout == b""
    retry = reconcile_project_line_endings(tmp_path, None, mode=LineEndingMode.REPAIR)
    assert retry.status is LineEndingStatus.SAFE
    assert (tmp_path / "locked" / "b.v").read_bytes() == b"module m;\nendmodule\n"


@pytest.mark.parametrize("partial", ["removed", "rewritten"])
def test_checkout_failure_after_partial_work_is_reported(
    tmp_path: Path, git_commands: GitCommandFake, partial: str
):
    _crlf_repo(tmp_path)
    path = tmp_path / "a.v"
    # Git unlinked the path and could not write it back, or rewrote it; then it failed.
    before = path.unlink if partial == "removed" else lambda: path.write_bytes(b"module a;\n")
    git_commands.answer("checkout-index", "-u", returncode=1, before=before)
    git_commands.answer("--index-info", stderr=b"index failure")

    report = reconcile_project_line_endings(tmp_path, None, mode=LineEndingMode.REPAIR)

    detail = _action(report).detail or ""
    assert "simulated failure" in detail
    assert "git checkout -- <path>" in detail
    if partial == "removed":
        assert "could not restore the mtime" not in detail
    else:
        assert "could not refresh the index after the failure" in detail


@pytest.mark.skipif(os.name == "nt", reason="a colon or a tab is not valid in a Windows file name")
@pytest.mark.parametrize(
    ("git_name", "file_name"),
    [
        pytest.param("./:a.v", ":a.v", id="colon"),  # "./" keeps Git from reading pathspec magic
        pytest.param("a\tb.v", "a\tb.v", id="tab"),
    ],
)
def test_crlf_path_with_git_special_characters_is_normalized(
    tmp_path: Path, git_name: str, file_name: str
):
    _init(tmp_path, autocrlf="true")
    _commit_file(tmp_path, git_name, b"module a;\nendmodule\n")
    (tmp_path / file_name).unlink()
    assert _git(tmp_path, "checkout", "--", git_name).returncode == 0
    assert b"\r\n" in (tmp_path / file_name).read_bytes()

    report = reconcile_project_line_endings(tmp_path, None, mode=LineEndingMode.REPAIR)

    assert report.status is LineEndingStatus.SAFE
    assert (tmp_path / file_name).read_bytes() == b"module a;\nendmodule\n"


def test_malformed_index_records_are_skipped(tmp_path: Path, git_commands: GitCommandFake):
    _init(tmp_path)
    _commit_file(tmp_path, "a.v", b"module a;\nendmodule\n")
    records = b"no tabs at all\0H 100644\ti/lf w/crlf\ta.v\0H 100644 abc 0\ti/lf\ta.v\0"
    git_commands.answer("ls-files", returncode=0, stdout=records, stderr=b"")

    report = reconcile_project_line_endings(tmp_path, None, mode=LineEndingMode.INSPECT)

    assert report.status is LineEndingStatus.SAFE


def test_many_left_paths_are_listed_with_a_count(tmp_path: Path):
    names = _multi_crlf_repo(tmp_path, 6)
    baseline = line_endings.sample_worktree_cleanliness(tmp_path)
    for name in names[1:]:
        (tmp_path / name).write_bytes(b"module edited;\r\n// local edit\r\nendmodule\r\n")

    report = reconcile_project_line_endings(
        tmp_path, mode=LineEndingMode.REPAIR, clean_baseline=baseline
    )

    detail = _action(report).detail or ""
    assert detail.startswith("normalized 1 file(s) to LF; left 5 tracked file(s) untouched:")
    assert detail.endswith("'m3.v' (changed), and 2 more")
    assert "m4.v" not in detail


def test_mtime_that_cannot_be_shifted_leaves_the_path_with_its_cause(tmp_path: Path):
    _crlf_repo(tmp_path)
    before = _snapshot(tmp_path / "a.v")

    with patch.object(os, "utime", side_effect=OSError("read-only volume")):
        report = reconcile_project_line_endings(tmp_path, None, mode=LineEndingMode.REPAIR)

    normalize = _action(report)
    assert normalize.state is LineEndingActionState.REFUSED
    assert normalize.reason is line_endings.LineEndingRefusalReason.NOT_LINE_ENDING_ONLY
    assert "'a.v' (could not update mtime: read-only volume)" in (normalize.detail or "")
    assert _snapshot(tmp_path / "a.v") == before


@pytest.mark.parametrize(
    ("change", "cause"),
    [
        ("directory", "not a regular file"),
        ("deleted", "could not read"),
        pytest.param(
            "unreadable",
            "could not read",
            marks=pytest.mark.skipif(os.name == "nt", reason="needs POSIX permissions"),
        ),
    ],
)
def test_candidate_changed_after_classification_starts_is_left_with_its_cause(
    tmp_path: Path, git_commands: GitCommandFake, change: str, cause: str
):
    _crlf_repo(tmp_path)
    path = tmp_path / "a.v"
    if change == "unreadable":
        path.chmod(0)
        readable = os.access(path, os.R_OK)
        path.chmod(0o644)
        if readable:
            pytest.skip("file permissions are not enforced for this user")

    def mutate() -> None:
        if change == "unreadable":
            path.chmod(0)
            return
        path.unlink()
        if change == "directory":
            path.mkdir()

    # The change lands after Git's mode check and before the per-file classification.
    git_commands.answer("diff-files", returncode=0, stderr=b"", before=mutate)

    report = reconcile_project_line_endings(tmp_path, None, mode=LineEndingMode.REPAIR)

    normalize = _action(report)
    assert normalize.state is LineEndingActionState.REFUSED
    assert normalize.reason is LineEndingRefusalReason.NOT_LINE_ENDING_ONLY
    assert f"'a.v' ({cause}" in (normalize.detail or "")


def test_crlf_checkout_in_a_sha256_repository_is_normalized(tmp_path: Path):
    assert _git(tmp_path, "init", "-q", "--object-format=sha256").returncode == 0
    assert _git(tmp_path, "config", "core.autocrlf", "true").returncode == 0
    _commit_file(tmp_path, "a.v", b"module a;\nendmodule\n")
    _recheckout_again(tmp_path, "a.v")
    assert b"\r\n" in (tmp_path / "a.v").read_bytes()

    report = reconcile_project_line_endings(tmp_path, None, mode=LineEndingMode.REPAIR)

    assert report.status is LineEndingStatus.SAFE
    assert (tmp_path / "a.v").read_bytes() == b"module a;\nendmodule\n"


def _converted_repo(
    root: Path,
    attributes: bytes,
    content: bytes,
    *,
    autocrlf: str = "true",
    driver: tuple[str, str, str] | None = None,
    name: str = "a.v",
) -> None:
    """CRLF checkout of ``name`` under a repo-local attributes rule (Git does the checkout).

    ``driver`` is a filter's ``(name, clean, smudge)``, configured before the commit.
    """
    _init(root, autocrlf=autocrlf)
    (root / ".git" / "info").mkdir(exist_ok=True)
    (root / ".git" / "info" / "attributes").write_bytes(attributes)
    if driver is not None:
        driver_name, clean, smudge = driver
        for key, value in (
            (f"filter.{driver_name}.clean", clean),
            (f"filter.{driver_name}.smudge", smudge),
        ):
            assert _git(root, "config", key, value).returncode == 0
    _commit_file(root, name, content)
    _recheckout_again(root, name)


needs_posix_filters = pytest.mark.skipif(
    os.name == "nt", reason="the filter drivers are POSIX shell commands"
)
_CAT_DRIVER = ("cat", "cat", "cat")


@needs_posix_filters
@pytest.mark.parametrize("driver", ["ident", "filter", "ident-quoted-name"])
def test_converted_paths_are_normalized_through_git_clean_conversion(tmp_path: Path, driver: str):
    attributes = b"*.v filter=cat\n" if driver == "filter" else b"*.v ident\n"
    name = '"q\\\n\u00e9.v' if driver == "ident-quoted-name" else "a.v"
    _converted_repo(
        tmp_path,
        attributes,
        b"module a; // $Id$\nendmodule\n",
        driver=_CAT_DRIVER if driver == "filter" else None,
        name=name,
    )
    assert b"\r\n" in (tmp_path / name).read_bytes()

    report = reconcile_project_line_endings(tmp_path, None, mode=LineEndingMode.REPAIR)

    assert report.status is LineEndingStatus.SAFE
    assert b"\r" not in (tmp_path / name).read_bytes()
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

    normalize = _action(report)
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

    normalize = _action(report)
    assert "checkout output differs from the file beyond line endings" in (normalize.detail or "")
    assert _snapshot(tmp_path / "a.v") == before


@pytest.mark.parametrize(
    ("failure", "fragment"),
    [("exit", "simulated failure"), ("short-output", "unexpected number of object ids")],
)
def test_failing_clean_conversion_leaves_converted_paths_with_their_cause(
    tmp_path: Path, git_commands: GitCommandFake, failure: str, fragment: str
):
    _converted_repo(tmp_path, b"a.v ident\n", b"module a; // $Id$\nendmodule\n")
    before = _snapshot(tmp_path / "a.v")
    if failure == "exit":
        git_commands.answer("hash-object")
    else:
        git_commands.answer("hash-object", returncode=0, stderr=b"")

    report = reconcile_project_line_endings(tmp_path, None, mode=LineEndingMode.REPAIR)

    normalize = _action(report)
    assert normalize.state is LineEndingActionState.REFUSED
    assert "'a.v' (clean conversion failed: " in (normalize.detail or "")
    assert fragment in (normalize.detail or "")
    assert _snapshot(tmp_path / "a.v") == before


def _cat_filter_repo(root: Path) -> None:
    _converted_repo(root, b"a.v filter=cat\n", b"module a;\nendmodule\n", driver=_CAT_DRIVER)


@needs_posix_filters
@pytest.mark.parametrize(
    ("failure", "fragment"),
    [
        ("exit", "simulated failure"),
        ("incomplete", "incomplete Git output"),
        ("crash", "git vanished"),
    ],
)
def test_failing_smudge_probe_leaves_filtered_paths_with_their_cause(
    tmp_path: Path, git_commands: GitCommandFake, failure: str, fragment: str
):
    _cat_filter_repo(tmp_path)
    before = _snapshot(tmp_path / "a.v")
    if failure == "exit":
        git_commands.answer("--temp")
    elif failure == "incomplete":
        git_commands.answer("--temp", returncode=0, stderr=b"")
    else:
        git_commands.crash("--temp", error=OSError("git vanished"))

    report = reconcile_project_line_endings(tmp_path, None, mode=LineEndingMode.REPAIR)

    normalize = _action(report)
    assert f"'a.v' (filter failed: {fragment}" in (normalize.detail or "")
    assert _snapshot(tmp_path / "a.v") == before


@needs_posix_filters
@pytest.mark.parametrize("cleanup", ["removable", "locked", "vanished"])
def test_smudge_probe_timeout_removes_its_temporary_files(
    tmp_path: Path, git_commands: GitCommandFake, cleanup: str
):
    _cat_filter_repo(tmp_path)
    (tmp_path / ".merge_file_keep").write_bytes(b"not ours")
    temporary = tmp_path / ".merge_file_new"
    real_unlink = Path.unlink

    def partial_output() -> None:
        if cleanup != "vanished":
            temporary.write_bytes(b"partial")

    def locked_unlink(path: Path, missing_ok: bool = False) -> None:
        if path.name == ".merge_file_new":
            raise PermissionError("held by a scanner")
        real_unlink(path, missing_ok=missing_ok)

    git_commands.crash(
        "--temp",
        error=subprocess.TimeoutExpired(["git"], 300, output=b".merge_file_new\ta.v\0"),
        before=partial_output,
    )

    if cleanup == "locked":
        with patch.object(Path, "unlink", new=locked_unlink):
            report = reconcile_project_line_endings(tmp_path, None, mode=LineEndingMode.REPAIR)
    else:
        report = reconcile_project_line_endings(tmp_path, None, mode=LineEndingMode.REPAIR)

    detail = _action(report).detail or ""
    assert "'a.v' (filter failed: timed out after 300 s)" in detail
    assert (tmp_path / ".merge_file_keep").read_bytes() == b"not ours"
    assert temporary.exists() is (cleanup == "locked")
    assert ("temporary file could not be removed: .merge_file_new" in detail) is (
        cleanup == "locked"
    )


@needs_posix_filters
def test_unreadable_filtered_path_is_left_with_its_cause(tmp_path: Path):
    _cat_filter_repo(tmp_path)
    real_read = Path.read_bytes

    def read_bytes(path: Path) -> bytes:
        if path.name == "a.v":
            raise OSError("sharing violation")
        return real_read(path)

    with patch.object(Path, "read_bytes", new=read_bytes):
        report = reconcile_project_line_endings(tmp_path, None, mode=LineEndingMode.REPAIR)

    assert "sharing violation" in (_action(report).detail or "")


def _phantom_repo(root: Path) -> None:
    """a.v is LF and matches the index, but the index still holds its CRLF checkout's stat."""
    _crlf_repo(root)
    assert _git(root, "config", "core.autocrlf", "false").returncode == 0
    (root / "a.v").write_bytes(b"module a;\nendmodule\n")


@pytest.mark.parametrize(
    ("failing", "code", "fragment"),
    [
        (("core.eol",), LineEndingObservationCode.LOCAL_AUTOCRLF_UNREADABLE, "core.eol"),
        (("ls-files",), LineEndingObservationCode.EOL_SCAN_UNREADABLE, ""),
        (("--name-only",), LineEndingObservationCode.STATUS_UNREADABLE, "simulated failure"),
        (
            ("--name-only", "--cached"),
            LineEndingObservationCode.STATUS_UNREADABLE,
            "simulated failure",
        ),
    ],
    ids=["core.eol", "ls-files", "diff", "diff-cached"],
)
def test_unreadable_git_state_makes_inspection_unsafe(
    tmp_path: Path,
    git_commands: GitCommandFake,
    failing: tuple[str, ...],
    code: LineEndingObservationCode,
    fragment: str,
):
    _phantom_repo(tmp_path)
    git_commands.answer(*failing)

    report = reconcile_project_line_endings(tmp_path, None, mode=LineEndingMode.INSPECT)

    observation = next(item for item in report.repositories[0].observations if item.code is code)
    assert fragment in (observation.detail or "")
    assert report.status is LineEndingStatus.UNSAFE


@pytest.mark.parametrize("failing", ["--index-info", "--refresh", "index-scan"])
def test_stale_index_refresh_failures_are_reported(
    tmp_path: Path, git_commands: GitCommandFake, failing: str
):
    _phantom_repo(tmp_path)
    if failing == "index-scan":
        git_commands.answer("ls-files", call=2)  # call 1 is planning; 2 the refresh's own scan
    else:
        git_commands.answer(failing)

    report = reconcile_project_line_endings(tmp_path, None, mode=LineEndingMode.REPAIR)

    refresh = _action(report, LineEndingActionKind.REFRESH_INDEX)
    assert refresh.state is LineEndingActionState.FAILED
    assert "simulated failure" in (refresh.detail or "")
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


@pytest.mark.parametrize("failure", ["autocrlf-exit", "autocrlf-exit-after-write", "eol-crash"])
def test_failed_pin_write_is_reported_failed(
    tmp_path: Path, git_commands: GitCommandFake, failure: str
):
    _init(tmp_path, autocrlf="true")
    if failure == "autocrlf-exit":
        git_commands.answer("core.autocrlf", "false")
    elif failure == "autocrlf-exit-after-write":
        # The value changes and Git still exits non-zero: accepting the exit because the
        # value now verifies would hide a failed write.
        git_commands.answer(
            "core.autocrlf",
            "false",
            before=lambda: _git(tmp_path, "config", "--local", "core.autocrlf", "false"),
        )
    else:
        git_commands.crash("core.eol", "lf", error=OSError("git vanished"))

    report = reconcile_project_line_endings(tmp_path, None, mode=LineEndingMode.REPAIR)

    pin = _action(report, LineEndingActionKind.PIN_AUTOCRLF)
    expected = (
        "core.eol: git vanished" if failure == "eol-crash" else "core.autocrlf: simulated failure"
    )
    assert pin.state is LineEndingActionState.FAILED
    assert pin.detail == f"could not set {expected}"
    assert report.status is LineEndingStatus.UNSAFE


def test_unverifiable_pin_is_reported_failed(tmp_path: Path, git_commands: GitCommandFake):
    _init(tmp_path, autocrlf="true")
    # core.eol reads: planning is 1, the pin's re-read 2, its verification after the writes 3.
    git_commands.answer("--get", "core.eol", call=3, returncode=0, stdout=b"crlf\n", stderr=b"")

    report = reconcile_project_line_endings(tmp_path, None, mode=LineEndingMode.REPAIR)

    pin = _action(report, LineEndingActionKind.PIN_AUTOCRLF)
    assert pin.state is LineEndingActionState.FAILED
    assert "did not verify" in (pin.detail or "")


def test_unreadable_git_status_refuses_normalization_with_a_typed_reason(
    tmp_path: Path, git_commands: GitCommandFake
):
    _crlf_repo(tmp_path)
    before = _snapshot(tmp_path / "a.v")
    git_commands.answer("status", call=2)  # call 1 is the phantom scan; 2 the cleanliness check

    report = reconcile_project_line_endings(tmp_path, None, mode=LineEndingMode.REPAIR)

    normalize = _action(report)
    assert normalize.state is LineEndingActionState.REFUSED
    assert normalize.reason is LineEndingRefusalReason.STATUS_UNREADABLE
    assert _snapshot(tmp_path / "a.v") == before


def test_normalized_tracked_root_attributes_get_no_default_rule(tmp_path: Path):
    _crlf_repo(tmp_path)
    _commit_file(tmp_path, ".gitattributes", b"*.bat -text\n")
    _recheckout_again(tmp_path, ".gitattributes")
    assert b"\r\n" in (tmp_path / ".gitattributes").read_bytes()

    report = reconcile_project_line_endings(tmp_path, None, mode=LineEndingMode.REPAIR)

    assert report.status is LineEndingStatus.SAFE
    assert (tmp_path / ".gitattributes").read_bytes() == b"*.bat -text\n"

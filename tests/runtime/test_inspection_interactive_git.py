"""Interactive Mode Git observations: info/exclude entries and tracked scratch logs."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from tests.subprocess_budget import SUBPROCESS_TIMEOUT_S

from booley.audit.diagnostic_results import DiagnosticFinding, Severity
from booley.runtime import inspection

pytestmark = pytest.mark.usefixtures("isolated_git_excludes")

_LOGS_PASS = ".interactive_logs/ is not tracked in git"
_EXCLUDES_PASS = "Booley files excluded from git (info/exclude)"


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        check=True,
        timeout=SUBPROCESS_TIMEOUT_S,
    )


def _init_repo(root: Path) -> None:
    """Create a repository with a local identity, independent of host config."""
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-q", str(root)],
        capture_output=True,
        check=True,
        timeout=SUBPROCESS_TIMEOUT_S,
    )
    _git(root, "config", "user.name", "Test User")
    _git(root, "config", "user.email", "test@example.com")


def _write_exclude(root: Path, *entries: str) -> None:
    exclude = root / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    exclude.write_bytes("".join(f"{entry}\n" for entry in entries).encode("utf-8"))


def _inspect(project_root: Path, booley_dir: Path) -> tuple[DiagnosticFinding, ...]:
    booley_dir.mkdir(parents=True, exist_ok=True)  # git runs with it as the working dir
    request = inspection.InteractiveGitRequest(project_root, booley_dir)
    return inspection.inspect_interactive_git(request).findings


def _tuples(
    findings: tuple[DiagnosticFinding, ...],
) -> list[tuple[Severity, str, str, str | None, str | None]]:
    return [(f.severity, f.message, f.fix, f.check_id, f.subject) for f in findings]


def _seed_logs(booley_dir: Path) -> None:
    logs = booley_dir / ".interactive_logs"
    logs.mkdir(parents=True)
    (logs / "one.log").write_bytes(b"one\n")
    (logs / "two.log").write_bytes(b"two\n")


def test_missing_exclude_entries_warn_once_per_entry(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _write_exclude(tmp_path, "/.devcontainer")

    findings = _inspect(tmp_path, tmp_path / ".booley")

    assert _tuples(findings) == [
        (
            Severity.WARN,
            "git info/exclude missing Booley entry: .booley",
            "",
            "project.git-excludes-missing",
            ".booley",
        ),
        (Severity.PASS, _LOGS_PASS, "", None, None),
    ]


def test_complete_excludes_pass(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _write_exclude(tmp_path, "/.devcontainer", "/.booley")

    findings = _inspect(tmp_path, tmp_path / ".booley")

    assert _tuples(findings) == [
        (Severity.PASS, _EXCLUDES_PASS, "", None, None),
        (Severity.PASS, _LOGS_PASS, "", None, None),
    ]


def test_tracked_interactive_logs_fail_with_count_and_fix(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _write_exclude(tmp_path, "/.devcontainer", "/.booley")
    booley_dir = tmp_path / ".booley"
    _seed_logs(booley_dir)
    _git(tmp_path, "add", "-f", "--", ".booley/.interactive_logs")

    findings = _inspect(tmp_path, booley_dir)

    assert _tuples(findings)[1:] == [
        (
            Severity.FAIL,
            ".interactive_logs/ has 2 tracked file(s); committed "
            "scratch breaks fresh clones in the sandbox",
            "git rm -r --cached .interactive_logs && commit the removal",
            None,
            None,
        )
    ]


def test_untracked_interactive_logs_pass(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _write_exclude(tmp_path, "/.devcontainer", "/.booley")
    booley_dir = tmp_path / ".booley"
    _seed_logs(booley_dir)

    findings = _inspect(tmp_path, booley_dir)

    assert _tuples(findings)[1:] == [(Severity.PASS, _LOGS_PASS, "", None, None)]


def test_unlistable_booley_dir_reports_no_log_finding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The ceiling stops Git's upward search at tmp_path, so the loose directory is
    # never inside a repository wherever the temp directory lives.
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    project_root = tmp_path / "project"
    _init_repo(project_root)
    _write_exclude(project_root, "/.devcontainer", "/.booley")
    loose = tmp_path / "loose"
    loose.mkdir()

    findings = _inspect(project_root, loose)

    assert [f.message for f in findings] == [_EXCLUDES_PASS]

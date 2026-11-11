"""Git facts behind ``reviewer --base`` (issue #3270): context detection, caps, failures."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from booley.specialists import review_change
from booley.specialists.review_change import (
    MAX_COMMITS_CHARS,
    MAX_DIFF_CHARS,
    ReviewChangeError,
    resolve_review_change,
)
from tests.project_runtime_git_support import git


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Baseline commit with a package, a module, a C header and a Python file."""
    config = tmp_path / "git-config"
    config.mkdir()
    empty = config / "empty"
    empty.touch()
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("HOME", str(config))
    root = tmp_path / "repo"
    (root / "rtl").mkdir(parents=True)
    files = {
        "rtl/pkg.sv": "package my_pkg;\nendpackage\n",
        "rtl/user.sv": "module user;\n  import my_pkg::*;\nendmodule\n",
        "rtl/commented.sv": "module commented;\n  // my_pkg is not used here\nendmodule\n",
        "rtl/iface.sv": "interface bus_if;\nendinterface\n",
        "rtl/uses_if.sv": "module uses_if(bus_if b);\nendmodule\n",
        "rtl/helper.py": "import shared\n",
        "rtl/shared.py": "VALUE = 1\n",
        "rtl/lonely.sv": "module lonely;\nendmodule\n",
    }
    for name, text in files.items():
        (root / name).write_text(text, newline="\n")
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "core.excludesFile", str(empty))
    git(root, "config", "user.name", "Fixture")
    git(root, "config", "user.email", "fixture@example.invalid")
    git(root, "add", ".")
    git(root, "commit", "-qm", "baseline")
    git(root, "checkout", "-q", "-b", "feature")
    return root


def edit(root: Path, name: str, extra: str) -> None:
    path = root / name
    path.write_text(path.read_text() + extra, newline="\n")


def test_context_uses_declared_package_interface_and_stem_names(repo: Path) -> None:
    edit(repo, "rtl/pkg.sv", "// changed\n")
    edit(repo, "rtl/iface.sv", "// changed\n")
    edit(repo, "rtl/shared.py", "# changed\n")
    scope = [
        "rtl/pkg.sv",
        "rtl/user.sv",
        "rtl/commented.sv",
        "rtl/iface.sv",
        "rtl/uses_if.sv",
        "rtl/helper.py",
        "rtl/shared.py",
        "rtl/lonely.sv",
    ]

    change = resolve_review_change(repo, "main", scope)

    assert change.changed == ("rtl/iface.sv", "rtl/pkg.sv", "rtl/shared.py")
    # Comments never count as a use; unrelated files are not context.
    assert change.context == ("rtl/helper.py", "rtl/user.sv", "rtl/uses_if.sv")


def test_scope_entries_are_normalized_and_deleted_files_are_not_changes(repo: Path) -> None:
    (repo / "rtl/lonely.sv").unlink()
    edit(repo, "rtl/pkg.sv", "// changed\n")

    change = resolve_review_change(repo, "main", ["./rtl\\pkg.sv", "rtl/lonely.sv"])

    assert change.changed == ("rtl/pkg.sv",)
    assert change.context == ()


def test_no_change_in_scope_carries_no_diff_or_commits(repo: Path) -> None:
    edit(repo, "rtl/pkg.sv", "// changed\n")

    change = resolve_review_change(repo, "main", ["rtl/lonely.sv"])

    assert (change.changed, change.diff, change.commits) == ((), "", "")
    assert len(change.merge_base) == 40


def test_diff_and_commit_messages_are_capped(repo: Path) -> None:
    (repo / "rtl/pkg.sv").write_text("// x\n" * (MAX_DIFF_CHARS // 4), newline="\n")
    git(repo, "commit", "-qam", "m" * (MAX_COMMITS_CHARS + 100))

    change = resolve_review_change(repo, "main", ["rtl/pkg.sv"])

    assert change.diff.endswith("[DIFF TRUNCATED]\n")
    assert change.commits.endswith("[COMMITS TRUNCATED]\n")
    assert len(change.diff) < MAX_DIFF_CHARS + 50


def test_unreadable_scope_file_matches_nothing(repo: Path) -> None:
    edit(repo, "rtl/pkg.sv", "// changed\n")

    change = resolve_review_change(repo, "main", ["rtl/pkg.sv", "rtl/missing.sv"])

    assert change.context == ()


def test_not_a_checkout_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ReviewChangeError, match=r"--base main: not a commit"):
        resolve_review_change(tmp_path, "main", ["rtl/a.sv"])


def test_unrelated_history_has_no_merge_base(repo: Path) -> None:
    git(repo, "checkout", "-q", "--orphan", "island")
    git(repo, "commit", "-qm", "unrelated root", "--allow-empty")

    with pytest.raises(ReviewChangeError, match=r"--base main"):
        resolve_review_change(repo, "main", ["rtl/pkg.sv"])


@pytest.mark.parametrize(
    "failure,message",
    [
        (subprocess.TimeoutExpired(["git"], 30), "timed out"),
        (FileNotFoundError("git"), "could not run"),
    ],
)
def test_git_failures_are_reported(
    repo: Path, monkeypatch: pytest.MonkeyPatch, failure: Exception, message: str
) -> None:
    def broken(*_args: object, **_kwargs: object) -> None:
        raise failure

    monkeypatch.setattr(review_change, "run_captured_bounded", broken)

    with pytest.raises(ReviewChangeError, match=message):
        resolve_review_change(repo, "main", ["rtl/pkg.sv"])

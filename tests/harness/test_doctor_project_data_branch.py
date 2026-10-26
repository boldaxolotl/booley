"""Doctor diagnostics for outer/Booley Directory destination alignment."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from booley.audit.doctor_waivers import load_doctor_waivers
from booley.harness import doctor
from tests.harness.git_support import git_stdout as _git


def _repositories(tmp_path: Path, outer_branch: str, project_branch: str) -> tuple[Path, Path]:
    root = tmp_path / "project"
    root.mkdir()
    _git(root, "init", "-b", outer_branch)
    booley_dir = root / ".booley"
    booley_dir.mkdir()
    _git(booley_dir, "init", "-b", project_branch)
    return root, booley_dir


def _commit(repository: Path, name: str) -> None:
    _git(repository, "config", "user.name", "Test")
    _git(repository, "config", "user.email", "test@example.invalid")
    (repository / name).write_text(f"{name}\n", encoding="utf-8")
    _git(repository, "add", name)
    _git(repository, "commit", "-m", f"add {name}")


def _run_check(root: Path) -> doctor._Reporter:
    reporter = doctor._Reporter.create()
    doctor._check_project_data_destination_branch(root, reporter)
    return reporter


def test_missing_project_data_destination_warns_with_migration_choices(tmp_path: Path) -> None:
    root, booley_dir = _repositories(tmp_path, "main", "master")

    reporter = _run_check(root)

    warnings = [finding for finding in reporter.findings or () if finding.severity == "warn"]
    assert len(warnings) == 1
    finding = warnings[0]
    assert finding.check_id == "project-data.destination-branch-missing"
    assert finding.subject == "project-data"
    assert "main" in finding.message
    assert str(booley_dir) in finding.message
    assert "git -C" in finding.fix
    assert "switch -c main" in finding.fix
    assert "project_destination_ref" in finding.fix


@pytest.mark.parametrize("branch", ["main", "release/next"])
def test_matching_unborn_project_data_destination_passes(
    tmp_path: Path,
    branch: str,
) -> None:
    root, _booley_dir = _repositories(tmp_path, branch, branch)

    reporter = _run_check(root)

    assert reporter.counts["pass"] == 1
    assert reporter.counts["warn"] == 0


def test_unreadable_project_data_repository_has_distinct_warning(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    _git(root, "init", "-b", "main")
    booley_dir = root / ".booley"
    (booley_dir / ".git").mkdir(parents=True)

    reporter = _run_check(root)

    warnings = [finding for finding in reporter.findings or () if finding.severity == "warn"]
    assert len(warnings) == 1
    finding = warnings[0]
    assert finding.check_id == "project-data.destination-branch-unreadable"
    assert finding.subject == "project-data"
    assert str(booley_dir) in finding.message
    assert "inspect Git health" in finding.fix


def test_existing_destination_passes_when_another_branch_is_checked_out(tmp_path: Path) -> None:
    root, booley_dir = _repositories(tmp_path, "main", "master")
    _commit(booley_dir, "baseline.txt")
    _git(booley_dir, "branch", "main")

    reporter = _run_check(root)

    assert reporter.counts["pass"] == 1
    assert reporter.counts["warn"] == 0


def test_detached_outer_checkout_skips_alignment(tmp_path: Path) -> None:
    root, _booley_dir = _repositories(tmp_path, "main", "master")
    _commit(root, "README.md")
    _git(root, "checkout", "--detach")

    reporter = _run_check(root)

    assert reporter.counts["skip"] == 1
    assert reporter.counts["warn"] == 0
    assert "detached HEAD" in (reporter.findings or ())[0].message


def test_nonstandalone_project_data_skips_alignment(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    _git(root, "init", "-b", "main")
    (root / ".booley").mkdir()

    reporter = _run_check(root)

    assert reporter.counts["skip"] == 1
    assert reporter.counts["warn"] == 0


def test_linked_project_data_worktree_skips_standalone_alignment(tmp_path: Path) -> None:
    root, booley_dir = _repositories(tmp_path, "main", "main")
    _commit(root, "README.md")
    _commit(booley_dir, "baseline.txt")
    ticket_root = tmp_path / "ticket-worktree"
    _git(root, "worktree", "add", "-b", "ticket", str(ticket_root))
    _git(
        booley_dir,
        "worktree",
        "add",
        "-b",
        "ticket-project",
        str(ticket_root / ".booley"),
    )

    reporter = _run_check(ticket_root)

    assert reporter.counts["skip"] == 1
    assert reporter.counts["warn"] == 0


def test_missing_destination_warning_can_be_waived(tmp_path: Path) -> None:
    root, booley_dir = _repositories(tmp_path, "main", "master")
    (booley_dir / "doctor-waivers.toml").write_text(
        """\
version = 1

[[waiver]]
check = "project-data.destination-branch-missing"
subject = "project-data"
reason = "Branches intentionally differ."
permanent = true
""",
        encoding="utf-8",
    )
    reporter = doctor._Reporter.create(load_doctor_waivers(booley_dir))

    doctor._check_project_data_destination_branch(root, reporter)

    assert reporter.counts["warn"] == 0
    assert reporter.counts["waived"] == 1


def _linked_worktree(tmp_path: Path, *, versioned: bool, paired: bool) -> Path:
    """A primary checkout with a linked worktree whose ``.booley`` is a copy or a pair."""
    root, booley_dir = _repositories(tmp_path, "main", "main")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "commit", "--allow-empty", "-m", "base")
    if versioned:
        _commit(booley_dir, "booley.toml")
    else:
        shutil.rmtree(booley_dir / ".git")
    worktree = tmp_path / "linked"
    _git(root, "worktree", "add", "--detach", str(worktree), "HEAD")
    if paired:
        _git(booley_dir, "worktree", "add", "--detach", str(worktree / ".booley"), "HEAD")
    else:
        (worktree / ".booley").mkdir()
        (worktree / ".booley" / "booley.toml").write_text("copy\n", encoding="utf-8")
    return worktree


def _pairing_warnings(worktree: Path) -> list:
    reporter = doctor._Reporter.create()
    doctor._check_worktree_booley_pairing(worktree, reporter)
    return [finding for finding in reporter.findings or () if finding.severity == "warn"]


def test_plain_copy_of_versioned_booley_directory_in_linked_worktree_warns(
    tmp_path: Path,
) -> None:
    warnings = _pairing_warnings(_linked_worktree(tmp_path, versioned=True, paired=False))

    assert [finding.check_id for finding in warnings] == ["project-data.worktree-unpaired"]
    assert "Paired Checkout" in warnings[0].message
    assert "booley worktree new" in warnings[0].fix


@pytest.mark.parametrize(
    ("versioned", "paired"),
    [(True, True), (False, False)],
    ids=["paired-checkout", "snapshot-of-unversioned"],
)
def test_expected_worktree_booley_layouts_do_not_warn(
    tmp_path: Path, versioned: bool, paired: bool
) -> None:
    assert _pairing_warnings(_linked_worktree(tmp_path, versioned=versioned, paired=paired)) == []

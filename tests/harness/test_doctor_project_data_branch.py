"""Doctor diagnostics for outer/Project-data destination alignment."""

from __future__ import annotations

from pathlib import Path

import pytest

from booley.harness import doctor
from booley.harness.doctor_waivers import load_doctor_waivers
from tests.harness.git_support import git_stdout as _git


def _repositories(tmp_path: Path, outer_branch: str, project_branch: str) -> tuple[Path, Path]:
    root = tmp_path / "project"
    root.mkdir()
    _git(root, "init", "-b", outer_branch)
    project_dir = root / ".booley_project"
    project_dir.mkdir()
    _git(project_dir, "init", "-b", project_branch)
    return root, project_dir


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
    root, project_dir = _repositories(tmp_path, "main", "master")

    reporter = _run_check(root)

    warnings = [finding for finding in reporter.findings or () if finding.severity == "warn"]
    assert len(warnings) == 1
    finding = warnings[0]
    assert finding.check_id == "project-data.destination-branch-missing"
    assert finding.subject == "project-data"
    assert "main" in finding.message
    assert str(project_dir) in finding.message
    assert "git -C" in finding.fix
    assert "switch -c main" in finding.fix
    assert "project_destination_ref" in finding.fix


@pytest.mark.parametrize("branch", ["main", "release/next"])
def test_matching_unborn_project_data_destination_passes(
    tmp_path: Path,
    branch: str,
) -> None:
    root, _project_dir = _repositories(tmp_path, branch, branch)

    reporter = _run_check(root)

    assert reporter.counts["pass"] == 1
    assert reporter.counts["warn"] == 0


def test_unreadable_project_data_repository_has_distinct_warning(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    _git(root, "init", "-b", "main")
    project_dir = root / ".booley_project"
    (project_dir / ".git").mkdir(parents=True)

    reporter = _run_check(root)

    warnings = [finding for finding in reporter.findings or () if finding.severity == "warn"]
    assert len(warnings) == 1
    finding = warnings[0]
    assert finding.check_id == "project-data.destination-branch-unreadable"
    assert finding.subject == "project-data"
    assert str(project_dir) in finding.message
    assert "inspect Git health" in finding.fix


def test_existing_destination_passes_when_another_branch_is_checked_out(tmp_path: Path) -> None:
    root, project_dir = _repositories(tmp_path, "main", "master")
    _commit(project_dir, "baseline.txt")
    _git(project_dir, "branch", "main")

    reporter = _run_check(root)

    assert reporter.counts["pass"] == 1
    assert reporter.counts["warn"] == 0


def test_detached_outer_checkout_skips_alignment(tmp_path: Path) -> None:
    root, _project_dir = _repositories(tmp_path, "main", "master")
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
    (root / ".booley_project").mkdir()

    reporter = _run_check(root)

    assert reporter.counts["skip"] == 1
    assert reporter.counts["warn"] == 0


def test_linked_project_data_worktree_skips_standalone_alignment(tmp_path: Path) -> None:
    root, project_dir = _repositories(tmp_path, "main", "main")
    _commit(root, "README.md")
    _commit(project_dir, "baseline.txt")
    ticket_root = tmp_path / "ticket-worktree"
    _git(root, "worktree", "add", "-b", "ticket", str(ticket_root))
    _git(
        project_dir,
        "worktree",
        "add",
        "-b",
        "ticket-project",
        str(ticket_root / ".booley_project"),
    )

    reporter = _run_check(ticket_root)

    assert reporter.counts["skip"] == 1
    assert reporter.counts["warn"] == 0


def test_missing_destination_warning_can_be_waived(tmp_path: Path) -> None:
    root, project_dir = _repositories(tmp_path, "main", "master")
    (project_dir / "doctor-waivers.toml").write_text(
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
    reporter = doctor._Reporter.create(load_doctor_waivers(project_dir))

    doctor._check_project_data_destination_branch(root, reporter)

    assert reporter.counts["warn"] == 0
    assert reporter.counts["waived"] == 1

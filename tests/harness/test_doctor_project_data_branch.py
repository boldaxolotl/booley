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


def _common_state_findings(root: Path) -> list:
    reporter = doctor._Reporter.create()
    doctor._check_common_state_in_worktrees(root, reporter)
    return list(reporter.findings or ())


def _plant(path: Path, text: str = "planted\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_common_state_in_worktree_warns_on_planted_path(tmp_path: Path) -> None:
    worktree = _linked_worktree(tmp_path, versioned=False, paired=False)
    root = tmp_path / "project"
    planted = worktree / ".booley" / "runtime" / "session_activity" / "a"
    _plant(planted)
    # A main checkout full of Common state is where that state belongs.
    _plant(root / ".booley" / "runtime" / "session_activity" / "main")
    _plant(root / ".booley" / "findings.jsonl")

    findings = _common_state_findings(root)

    warnings = [finding for finding in findings if finding.severity == "warn"]
    assert [finding.check_id for finding in warnings] == ["project-data.common-state-in-worktree"]
    assert warnings[0].subject == str(worktree / ".booley")
    assert "runtime/session_activity" in warnings[0].message
    assert "findings.jsonl" not in warnings[0].message
    assert "git worktree remove" in warnings[0].fix
    assert planted.is_file()  # Doctor reports; it never deletes.
    assert not any(finding.severity == "pass" for finding in findings)


def test_common_state_in_worktree_passes_on_clean_snapshot(tmp_path: Path) -> None:
    worktree = _linked_worktree(tmp_path, versioned=False, paired=False)
    _plant(worktree / ".booley" / "cores" / "ip.core")  # Versioned state is fine.
    _plant(worktree / ".booley" / "flow-reports" / "r.json")  # So is source-tied state.

    findings = _common_state_findings(tmp_path / "project")

    assert [finding.severity for finding in findings] == ["pass"]


def test_common_state_in_worktree_ignores_tracked_paths_in_paired_checkout(
    tmp_path: Path,
) -> None:
    worktree = _linked_worktree(tmp_path, versioned=True, paired=True)
    paired = worktree / ".booley"
    # Committed in the Versioned Booley Directory, so present in every Paired Checkout.
    _plant(paired / "goalsets" / "x.md")
    _plant(paired / "docker" / "Dockerfile")
    _git(paired, "config", "user.name", "Test")
    _git(paired, "config", "user.email", "test@example.invalid")
    _git(paired, "add", "-f", "goalsets", "docker")
    _git(paired, "commit", "-m", "common files")

    assert [f.severity for f in _common_state_findings(tmp_path / "project")] == ["pass"]

    _plant(paired / "runtime" / "session_activity" / "a")
    warnings = [f for f in _common_state_findings(tmp_path / "project") if f.severity == "warn"]
    assert len(warnings) == 1
    assert "session_activity" in warnings[0].message
    assert "goalsets" not in warnings[0].message and "docker" not in warnings[0].message


def test_common_state_check_skips_when_the_project_is_the_booley_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(_root: Path) -> Path:
        raise doctor.SourceCheckoutProjectError("Booley's own source")

    monkeypatch.setattr(doctor, "resolve_common_booley_dir", refuse)

    assert [f.severity for f in _common_state_findings(tmp_path)] == ["skip"]


def _git_failure(stderr: str) -> doctor.subprocess.CalledProcessError:
    return doctor.subprocess.CalledProcessError(128, ["git"], stderr=stderr)


def test_common_state_check_skips_with_git_stderr_when_listing_worktrees_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(_root: Path) -> Path:
        raise _git_failure("fatal: not a git repository\n")

    monkeypatch.setattr("booley.runtime.directory_migration_git.primary_checkout", fail)

    findings = _common_state_findings(_linked_worktree(tmp_path, versioned=False, paired=False))

    assert [f.severity for f in findings] == ["skip"]
    assert "not a git repository" in findings[0].message


def test_common_state_check_names_exit_code_when_git_prints_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(_root: Path) -> Path:
        raise _git_failure("")

    monkeypatch.setattr("booley.runtime.directory_migration_git.primary_checkout", fail)

    findings = _common_state_findings(_linked_worktree(tmp_path, versioned=False, paired=False))

    assert "git exited 128" in findings[0].message


def test_common_state_check_ignores_worktrees_without_a_real_booley_directory(
    tmp_path: Path,
) -> None:
    worktree = _linked_worktree(tmp_path, versioned=False, paired=False)
    shutil.rmtree(worktree / ".booley")

    findings = _common_state_findings(tmp_path / "project")

    assert [(f.severity, f.message) for f in findings] == [
        ("pass", "no Common Booley State in 0 worktree Booley Directories")
    ]


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (_git_failure("fatal: bad index\n"), "bad index"),
        (_git_failure(""), "git exited 128"),
        (OSError("permission denied"), "permission denied"),
        (ValueError("bad utf-8"), "bad utf-8"),
    ],
)
def test_common_state_check_skips_a_worktree_it_cannot_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: Exception, expected: str
) -> None:
    worktree = _linked_worktree(tmp_path, versioned=False, paired=False)

    def fail(_worktree: Path, _booley_dir: Path) -> list[str]:
        raise error

    monkeypatch.setattr(doctor, "_untracked_common_paths", fail)

    findings = _common_state_findings(tmp_path / "project")

    assert [f.severity for f in findings] == ["skip", "pass"]
    assert str(worktree / ".booley") in findings[0].message
    assert expected in findings[0].message


def test_common_state_check_skips_a_worktree_removed_while_scanning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _linked_worktree(tmp_path, versioned=False, paired=False)

    def vanish(_worktree: Path, _booley_dir: Path) -> list[str]:
        raise FileNotFoundError

    monkeypatch.setattr(doctor, "_untracked_common_paths", vanish)

    assert [f.severity for f in _common_state_findings(tmp_path / "project")] == ["pass"]


def test_tracked_paths_lookup_raises_with_git_stderr_on_failure(tmp_path: Path) -> None:
    with pytest.raises(doctor.subprocess.CalledProcessError) as raised:
        doctor._tracked_booley_paths(tmp_path, tmp_path / ".booley")

    assert raised.value.stderr


def test_common_state_warning_counts_paths_beyond_the_shown_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _linked_worktree(tmp_path, versioned=False, paired=False)
    paths = [f"common{index}" for index in range(7)]
    monkeypatch.setattr(doctor, "_untracked_common_paths", lambda _worktree, _dir: paths)

    warnings = [f for f in _common_state_findings(tmp_path / "project") if f.severity == "warn"]

    assert len(warnings) == 1
    assert "common4, and 2 more" in warnings[0].message
    assert "common5" not in warnings[0].message


def _relocated_linked_worktree(tmp_path: Path, *, versioned: bool) -> Path:
    """A primary checkout with ``[project].dir = "state"`` and a linked worktree copying it."""
    root = tmp_path / "project"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.invalid")
    (root / "booley.toml").write_text('[project]\ndir = "state"\n', encoding="utf-8")
    _git(root, "add", "booley.toml")
    _git(root, "commit", "-qm", "base")
    state = root / "state"
    state.mkdir()
    (state / "booley.toml").write_text("main\n", encoding="utf-8")
    if versioned:
        _git(state, "init", "-q")
        _git(state, "config", "user.name", "Test")
        _git(state, "config", "user.email", "test@example.invalid")
        _git(state, "add", ".")
        _git(state, "commit", "-qm", "project")
    worktree = tmp_path / "linked"
    _git(root, "worktree", "add", "--detach", str(worktree), "HEAD")
    (worktree / "state").mkdir()
    (worktree / "state" / "booley.toml").write_text("copy\n", encoding="utf-8")
    return worktree


def test_plain_copy_at_relocated_directory_warns(tmp_path: Path) -> None:
    worktree = _relocated_linked_worktree(tmp_path, versioned=True)

    warnings = _pairing_warnings(worktree)

    assert [finding.check_id for finding in warnings] == ["project-data.worktree-unpaired"]
    assert str(worktree / "state") in warnings[0].message


def test_common_state_scan_reads_the_relocated_directory(tmp_path: Path) -> None:
    worktree = _relocated_linked_worktree(tmp_path, versioned=False)
    _plant(worktree / "state" / "runtime" / "session_activity" / "a")
    # Nothing under the default name is looked at.
    _plant(worktree / ".booley" / "runtime" / "session_activity" / "ignored")

    warnings = [f for f in _common_state_findings(tmp_path / "project") if f.severity == "warn"]

    assert [finding.subject for finding in warnings] == [str(worktree / "state")]


def test_tracked_paths_lookup_uses_the_relocated_pathspec(tmp_path: Path) -> None:
    worktree = _relocated_linked_worktree(tmp_path, versioned=False)
    _git(worktree, "add", "-f", "state/booley.toml")

    assert doctor._tracked_booley_paths(worktree, worktree / "state") == {"booley.toml"}

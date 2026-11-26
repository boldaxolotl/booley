"""Doctor diagnostics for outer/Booley Directory destination alignment."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from booley.core.host_shell import quote_arg
from booley.harness import doctor
from booley.runtime import directory_migration_git
from booley.runtime.worktree_evidence import tracked_booley_paths
from tests.harness.doctor_host_support import serve_healthy_doctor_host
from tests.harness.doctor_run_support import findings, run_doctor
from tests.harness.git_support import git_stdout as _git
from tests.harness.test_doctor import _write_project


def _repositories(
    tmp_path: Path, outer_branch: str, project_branch: str | None = None
) -> tuple[Path, Path]:
    """A Project on *outer_branch* whose Booley Directory is a repository on *project_branch*."""
    root = tmp_path / "project"
    root.mkdir()
    _git(root, "init", "-b", outer_branch)
    booley_dir = _write_project(root)
    if project_branch:
        _git(booley_dir, "init", "-b", project_branch)
    return root, booley_dir


def _identify(repository: Path):
    _git(repository, "config", "user.name", "Test")
    _git(repository, "config", "user.email", "test@example.invalid")


def _commit(repository: Path, name: str):
    _identify(repository)
    (repository / name).write_text(f"{name}\n", encoding="utf-8")
    _git(repository, "add", "-A")
    _git(repository, "commit", "-m", f"add {name}")


def _doctor(monkeypatch, checkout: Path) -> doctor.DoctorRunResult:
    """Run the whole Doctor on *checkout*, with real Git and a healthy host."""
    serve_healthy_doctor_host(monkeypatch, checkout)
    return run_doctor(checkout)


def _messages(result: doctor.DoctorRunResult, severity: str, needle: str = "") -> list[str]:
    return [f.message for f in result.findings if f.severity == severity and needle in f.message]


def _warnings(result: doctor.DoctorRunResult, check_id: str) -> list:
    return [f for f in findings(result, check_id) if f.severity == "warn"]


@pytest.mark.parametrize("unreadable", [False, True], ids=["missing", "unreadable"])
def test_unaligned_project_data_destination_warns_distinctly(tmp_path, monkeypatch, unreadable):
    (tmp_path / "it's").mkdir()
    root, booley_dir = _repositories(tmp_path / "it's", "main", None if unreadable else "master")
    if unreadable:
        (booley_dir / ".git").mkdir()
    kind = "unreadable" if unreadable else "missing"

    [finding] = _warnings(_doctor(monkeypatch, root), f"project-data.destination-branch-{kind}")

    assert finding.subject == "project-data"
    assert str(booley_dir) in finding.message
    if unreadable:
        assert "inspect Git health" in finding.fix
        return
    assert "main" in finding.message
    assert f"git -C {quote_arg(str(booley_dir))} switch -c {quote_arg('main')}" in finding.fix
    assert "project_destination_ref" in finding.fix


@pytest.mark.parametrize(
    ("outer", "inner", "existing"),
    [("main", "main", False), ("release/next", "release/next", False), ("main", "master", True)],
    ids=["unborn-main", "unborn-nested-name", "other-branch-checked-out"],
)
def test_aligned_project_data_destination_passes(tmp_path, monkeypatch, outer, inner, existing):
    root, booley_dir = _repositories(tmp_path, outer, inner)
    if existing:
        _commit(booley_dir, "baseline.txt")
        _git(booley_dir, "branch", outer)

    result = _doctor(monkeypatch, root)

    expected = (
        f"Booley Directory destination branch {outer!r} exists"
        if existing
        else f"Booley Directory destination branch matches outer branch {outer!r}"
    )
    assert _messages(result, "pass", "Booley Directory destination branch") == [expected]
    assert _warnings(result, "project-data.destination-branch-missing") == []


def _linked_project_data_worktree(tmp_path: Path) -> Path:
    root, booley_dir = _repositories(tmp_path, "main", "main")
    _commit(root, "README.md")
    _commit(booley_dir, "baseline.txt")
    ticket_root = tmp_path / "ticket-worktree"
    _git(root, "worktree", "add", "-b", "ticket", str(ticket_root))
    _git(booley_dir, "worktree", "add", "-b", "ticket-project", str(ticket_root / ".booley"))
    return ticket_root


def _detached_outer_checkout(tmp_path: Path) -> Path:
    root, _booley_dir = _repositories(tmp_path, "main", "master")
    _commit(root, "README.md")
    _git(root, "checkout", "--detach")
    return root


@pytest.mark.parametrize(
    ("build", "reason"),
    [
        (_detached_outer_checkout, "detached HEAD"),
        (lambda tmp_path: _repositories(tmp_path, "main")[0], "no standalone repository"),
        (_linked_project_data_worktree, "no standalone repository"),
    ],
    ids=["detached-outer", "nonstandalone", "linked-project-data-worktree"],
)
def test_project_data_alignment_is_skipped_when_it_does_not_apply(
    tmp_path, monkeypatch, build, reason: str
):
    result = _doctor(monkeypatch, build(tmp_path))

    [skipped] = _messages(result, "skip", "Booley Directory branch alignment")
    assert reason in skipped
    assert findings(result, "project-data.destination-branch-missing") == []
    assert findings(result, "project-data.destination-branch-unreadable") == []


def test_missing_destination_warning_can_be_waived(tmp_path, monkeypatch):
    root, booley_dir = _repositories(tmp_path, "main", "master")
    waiver = b'version = 1\n[[waiver]]\ncheck = "project-data.destination-branch-missing"\n'
    waiver += b'subject = "project-data"\nreason = "Branches differ."\npermanent = true\n'
    (booley_dir / "doctor-waivers.toml").write_bytes(waiver)

    result = _doctor(monkeypatch, root)

    [finding] = findings(result, "project-data.destination-branch-missing")
    assert finding.severity == "waived"


def _linked_worktree(tmp_path: Path, *, versioned: bool, paired: bool) -> Path:
    """A primary checkout with a linked worktree whose ``.booley`` is a copy or a pair."""
    root, booley_dir = _repositories(tmp_path, "main", "main")
    _identify(root)
    _git(root, "commit", "--allow-empty", "-m", "base")
    if versioned:
        _commit(booley_dir, "baseline.txt")
    else:
        shutil.rmtree(booley_dir / ".git")
    worktree = tmp_path / "linked"
    _git(root, "worktree", "add", "--detach", str(worktree), "HEAD")
    if paired:
        _git(booley_dir, "worktree", "add", "--detach", str(worktree / ".booley"), "HEAD")
    else:
        shutil.copytree(
            booley_dir, worktree / ".booley", ignore=shutil.ignore_patterns(".git", "tickets")
        )
    return worktree


@pytest.mark.parametrize(
    ("versioned", "paired", "warns"),
    [(True, False, True), (True, True, False), (False, False, False)],
    ids=["plain-copy-of-versioned", "paired-checkout", "snapshot-of-unversioned"],
)
def test_plain_copy_of_versioned_booley_directory_in_linked_worktree_warns(
    tmp_path, monkeypatch, versioned, paired, warns
):
    worktree = _linked_worktree(tmp_path, versioned=versioned, paired=paired)

    found = _warnings(_doctor(monkeypatch, worktree), "project-data.worktree-unpaired")

    assert bool(found) == warns
    if warns:
        assert "Paired Checkout" in found[0].message
        assert "booley worktree prepare" in found[0].fix


def _plant(path: Path, text: str = "planted\n"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_common_state_in_worktree_warns_on_planted_path(tmp_path, monkeypatch):
    worktree = _linked_worktree(tmp_path, versioned=False, paired=False)
    root = tmp_path / "project"
    planted = worktree / ".booley" / "runtime" / "session_activity" / "a"
    _plant(planted)
    _plant(worktree / ".booley" / "measurements" / ("a" * 40) / "r.json")
    # A main checkout full of Common state is where that state belongs.
    _plant(root / ".booley" / "runtime" / "session_activity" / "main")
    _plant(root / ".booley" / "findings.jsonl")

    result = _doctor(monkeypatch, root)

    [warning] = _warnings(result, "project-data.common-state-in-worktree")
    assert warning.subject == str(worktree / ".booley")
    assert "runtime/session_activity" in warning.message
    assert "measurements" in warning.message
    assert "findings.jsonl" not in warning.message
    assert "git worktree remove" in warning.fix
    assert planted.is_file()  # Doctor reports; it never deletes.
    assert _messages(result, "pass", "Common Booley State") == []


@pytest.mark.parametrize("real_directory", [True, False], ids=["clean-snapshot", "no-directory"])
def test_common_state_in_worktree_passes_without_stale_copies(
    tmp_path, monkeypatch, real_directory
):
    worktree = _linked_worktree(tmp_path, versioned=False, paired=False)
    if real_directory:
        _plant(worktree / ".booley" / "cores" / "ip.core")  # Versioned state is fine.
        _plant(worktree / ".booley" / "flow-reports" / "r.json")  # So is source-tied state.
    else:
        shutil.rmtree(worktree / ".booley")

    result = _doctor(monkeypatch, tmp_path / "project")

    assert _messages(result, "pass", "Common Booley State") == [
        f"no Common Booley State in {int(real_directory)} worktree Booley Directories"
    ]


def test_common_state_in_worktree_ignores_tracked_paths_in_paired_checkout(tmp_path, monkeypatch):
    worktree = _linked_worktree(tmp_path, versioned=True, paired=True)
    paired = worktree / ".booley"
    # Committed in the Versioned Booley Directory, so present in every Paired Checkout.
    _plant(paired / "goalsets" / "x.md")
    _plant(paired / "docker" / "Dockerfile")
    _identify(paired)
    _git(paired, "add", "-f", "goalsets", "docker")
    _git(paired, "commit", "-m", "common files")

    clean = _doctor(monkeypatch, tmp_path / "project")
    assert _messages(clean, "pass", "Common Booley State")
    assert _warnings(clean, "project-data.common-state-in-worktree") == []

    _plant(paired / "runtime" / "session_activity" / "a")
    [warning] = _warnings(
        _doctor(monkeypatch, tmp_path / "project"), "project-data.common-state-in-worktree"
    )
    assert "session_activity" in warning.message
    assert "goalsets" not in warning.message and "docker" not in warning.message


def _git_failure(stderr):
    return subprocess.CalledProcessError(128, ["git"], stderr=stderr)


@pytest.mark.parametrize(
    ("outcome", "skip_text"),
    [
        (_git_failure("fatal: bad index\n"), "bad index"),
        (_git_failure(""), "git exited 128"),
        (OSError("permission denied"), "permission denied"),
        (ValueError("bad utf-8"), "bad utf-8"),
        (FileNotFoundError(), None),  # a baseline worktree removed while scanning
        ([f"common{index}" for index in range(7)], None),
    ],
    ids=["git-stderr", "git-exit-code", "os-error", "value-error", "removed", "many-paths"],
)
def test_common_state_scan_outcomes(tmp_path, monkeypatch, outcome, skip_text):
    worktree = _linked_worktree(tmp_path, versioned=False, paired=False)

    def scan(_worktree, _booley_dir):
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    # No real collaborator: Git cannot be made to print non-UTF-8 paths, to fail after listing
    # a worktree, or to drop a worktree while Doctor scans it.
    monkeypatch.setattr(doctor, "untracked_common_paths", scan)

    result = _doctor(monkeypatch, tmp_path / "project")

    skipped = _messages(result, "skip", f"Common Booley State in {worktree / '.booley'}")
    warned = _warnings(result, "project-data.common-state-in-worktree")
    if skip_text:
        assert [skip_text in message for message in skipped] == [True]
    elif isinstance(outcome, list):
        assert "common4, and 2 more" in warned[0].message
        assert "common5" not in warned[0].message
    else:
        assert not skipped
    assert bool(_messages(result, "pass", "Common Booley State")) == (not warned)


def test_common_state_check_skips_when_the_project_is_the_booley_source(tmp_path, monkeypatch):
    def refuse(_root: Path) -> Path:
        raise doctor.SourceCheckoutProjectError("Booley's own source")

    monkeypatch.setattr(doctor, "resolve_common_booley_dir", refuse)
    # private: a whole run resolves the main Booley Directory unguarded in
    # `_run_project_phase` before this check, so this fault never reaches it there.
    reporter = doctor._Reporter.create()

    doctor._check_common_state_in_worktrees(tmp_path, reporter)

    [skipped] = reporter.findings or ()
    assert skipped.severity == "skip"
    assert "Booley's own source" in skipped.message


@pytest.mark.parametrize(
    ("stderr", "expected"),
    [("fatal: not a git repository\n", "not a git repository"), ("", "git exited 128")],
    ids=["git-stderr", "git-exit-code"],
)
def test_common_state_check_skips_when_listing_worktrees_fails(
    tmp_path, monkeypatch, stderr: str, expected: str
):
    _linked_worktree(tmp_path, versioned=False, paired=False)

    def fail(_root: Path) -> Path:
        raise _git_failure(stderr)

    monkeypatch.setattr(directory_migration_git, "primary_checkout", fail)

    result = _doctor(monkeypatch, tmp_path / "project")

    [skipped] = _messages(result, "skip", "Common Booley State in worktrees")
    assert expected in skipped
    assert _messages(result, "pass", "Common Booley State") == []


def test_tracked_paths_lookup_raises_with_git_stderr_on_failure(tmp_path: Path):
    with pytest.raises(subprocess.CalledProcessError) as raised:
        tracked_booley_paths(tmp_path, tmp_path / ".booley")

    assert raised.value.stderr


def _relocated_linked_worktree(tmp_path: Path, *, versioned: bool) -> Path:
    """A primary checkout with ``[project].dir = "state"`` and a linked worktree copying it."""
    root = tmp_path / "project"
    root.mkdir()
    _git(root, "init", "-q")
    _identify(root)
    booley_dir = _write_project(root)
    state = booley_dir.rename(root / "state")
    (root / "booley.toml").write_text('[project]\ndir = "state"\n', encoding="utf-8")
    _git(root, "add", "booley.toml")
    _git(root, "commit", "-qm", "base")
    if versioned:
        _git(state, "init", "-q")
        _identify(state)
        _git(state, "add", ".")
        _git(state, "commit", "-qm", "project")
    worktree = tmp_path / "linked"
    _git(root, "worktree", "add", "--detach", str(worktree), "HEAD")
    shutil.copytree(state, worktree / "state", ignore=shutil.ignore_patterns(".git", "tickets"))
    return worktree


def test_plain_copy_at_relocated_directory_warns(tmp_path, monkeypatch):
    worktree = _relocated_linked_worktree(tmp_path, versioned=True)

    [warning] = _warnings(_doctor(monkeypatch, worktree), "project-data.worktree-unpaired")

    assert str(worktree / "state") in warning.message


def test_common_state_scan_reads_the_relocated_directory(tmp_path, monkeypatch):
    worktree = _relocated_linked_worktree(tmp_path, versioned=False)
    _plant(worktree / "state" / "runtime" / "session_activity" / "a")
    _plant(worktree / ".booley" / "runtime" / "session_activity" / "ignored")

    result = _doctor(monkeypatch, tmp_path / "project")

    warnings = _warnings(result, "project-data.common-state-in-worktree")
    assert [f.subject for f in warnings] == [str(worktree / "state")]


def test_common_state_scan_skips_a_worktree_with_a_retired_layout(tmp_path, monkeypatch):
    worktree = _relocated_linked_worktree(tmp_path, versioned=False)
    (worktree / "booley.toml").write_text('[project]\ndir = ".booley_project"\n', encoding="utf-8")

    result = _doctor(monkeypatch, tmp_path / "project")

    assert _warnings(result, "project-data.common-state-in-worktree") == []
    assert _messages(result, "pass", "Common Booley State") == [
        "no Common Booley State in 0 worktree Booley Directories"
    ]

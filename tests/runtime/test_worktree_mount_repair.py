"""Worktree registrations naming a former Sandbox mount point: repair, prune, and report."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from tests.mount_change_support import respell_registration, simulate_mount_change
from tests.subprocess_budget import run_bounded_child

from booley.core import booley_dir
from booley.runtime import atomic_files, mount_history, worktree_mount_repair
from booley.runtime.worktree_mount_repair import repair_mount_registrations

pytestmark = pytest.mark.skipif(os.name == "nt", reason="Sandbox-only; POSIX paths")

_IDENTITY = ("-c", "user.name=T", "-c", "user.email=t@example.com")


def _git(cwd: Path, *args: str) -> str:
    result = run_bounded_child(["git", *_IDENTITY, *args], cwd=cwd, check=True)
    return result.stdout.strip()


class _Repo:
    def __init__(self, root: Path, booley: Path, former: Path) -> None:
        self.root = root
        self.booley = booley
        self.former = former

    def add(self, name: str, *, parent: Path | None = None, branch: bool = False) -> Path:
        path = (parent or self.booley / "worktrees") / name
        path.parent.mkdir(parents=True, exist_ok=True)
        args = ["worktree", "add", "-q", str(path)]
        _git(self.root, *args, *(["-b", f"b-{name.lstrip('.')}"] if branch else ["--detach"]))
        return path

    def admin(self, checkout: Path) -> Path:
        # Git names the administrative directory after the checkout, a leading dot as "-".
        name = (
            checkout.name.replace(".", "-", 1) if checkout.name.startswith(".") else checkout.name
        )
        return self.root / ".git" / "worktrees" / name

    def respell(self, checkout: Path, *, relative: bool = False) -> None:
        respell_registration(self.admin(checkout), self.booley, self.former, relative=relative)

    def repair(self) -> worktree_mount_repair.MountRepair:
        return repair_mount_registrations(self.root)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Repo:
    monkeypatch.delenv("BOOLEY_DIR", raising=False)
    booley_dir.reset_cache()
    root = tmp_path / "project"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / "a.txt").write_bytes(b"x\n")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "one")
    booley = root / ".booley"
    booley.mkdir()
    former = tmp_path / "former" / "deeper" / "mount"
    simulate_mount_change(monkeypatch, booley, former)
    return _Repo(root, booley.resolve(), former)


def _snapshot(directory: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(directory)): path.read_bytes()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


def test_pointers_naming_no_former_mount_stay_byte_identical(repo: _Repo, tmp_path: Path) -> None:
    outside = repo.add("elsewhere", parent=tmp_path / "other")
    before = _snapshot(repo.admin(outside))
    marker = (outside / ".git").read_bytes()

    result = repo.repair()

    assert result == worktree_mount_repair.MountRepair()
    assert _snapshot(repo.admin(outside)) == before
    assert (outside / ".git").read_bytes() == marker


def test_a_second_run_changes_nothing(repo: _Repo) -> None:
    checkout = repo.add("a")
    repo.respell(checkout)

    first = repo.repair()
    again = _snapshot(repo.admin(checkout))
    second = repo.repair()

    assert first.repaired == (repo.admin(checkout),)
    assert _git(checkout, "status", "--porcelain") == ""
    assert second == worktree_mount_repair.MountRepair()
    assert _snapshot(repo.admin(checkout)) == again


def test_config_pointers_across_a_former_mount_are_rebased(repo: _Repo) -> None:
    checkout = repo.add("a")
    (checkout / "hooks").mkdir()
    admin = repo.admin(checkout)
    config = admin / "config.worktree"
    config_args = ("config", "--file", str(config))
    moved = repo.former / "worktrees" / "a"
    _git(repo.root, *config_args, "core.hooksPath", str(moved / "hooks"))
    _git(repo.root, *config_args, "core.worktree", str(moved))
    _git(repo.root, *config_args, "core.bare", "false")
    repo.respell(checkout)

    result = repo.repair()

    assert result.repaired == (admin,)
    assert _git(repo.root, *config_args, "--get", "core.hooksPath") == str(checkout / "hooks")
    assert _git(repo.root, *config_args, "--get", "core.worktree") == str(checkout)
    assert _git(repo.root, *config_args, "--get", "core.bare") == "false"


def test_a_checkout_naming_another_registration_is_reported_and_untouched(repo: _Repo) -> None:
    first, second = repo.add("a"), repo.add("b")
    repo.respell(first)
    (first / ".git").write_bytes(f"gitdir: {repo.admin(second)}\n".encode())
    before = (_snapshot(repo.admin(first)), _snapshot(repo.admin(second)))

    result = repo.repair()

    assert [problem.registration for problem in result.problems] == [repo.admin(first)]
    assert "another registration" in result.problems[0].message()
    assert (_snapshot(repo.admin(first)), _snapshot(repo.admin(second))) == before
    assert (first / ".git").read_bytes() == f"gitdir: {repo.admin(second)}\n".encode()


def test_an_unreadable_checkout_gitfile_is_reported(repo: _Repo) -> None:
    checkout = repo.add("a")
    repo.respell(checkout)
    (checkout / ".git").write_bytes(b"not a gitfile\n")

    result = repo.repair()

    assert len(result.problems) == 1
    assert (checkout / ".git").read_bytes() == b"not a gitfile\n"


def test_a_locked_absent_baseline_is_kept_and_reported(repo: _Repo) -> None:
    baseline = repo.add(".baseline-wt-1-abc1234", parent=repo.booley)
    _git(repo.root, "worktree", "lock", str(baseline))
    repo.respell(baseline)
    shutil.rmtree(baseline)

    result = repo.repair()

    assert repo.admin(baseline).is_dir()
    assert result.pruned == ()
    assert "is missing" in result.problems[0].message()


def test_an_absent_unlocked_baseline_is_pruned(repo: _Repo) -> None:
    baseline = repo.add(".baseline-wt-1-abc1234", parent=repo.booley)
    repo.respell(baseline)
    shutil.rmtree(baseline)

    result = repo.repair()

    assert result.pruned == (repo.admin(baseline),)
    assert not repo.admin(baseline).exists()


def test_an_absent_baseline_on_a_branch_or_without_head_is_not_disposable(repo: _Repo) -> None:
    on_branch = repo.add(".baseline-wt-2-abc1234", parent=repo.booley, branch=True)
    headless = repo.add(".baseline-wt-3-abc1234", parent=repo.booley)
    for checkout in (on_branch, headless):
        repo.respell(checkout)
        shutil.rmtree(checkout)
    (repo.admin(headless) / "HEAD").unlink()

    result = repo.repair()

    assert len(result.problems) == 2
    assert repo.admin(on_branch).is_dir() and repo.admin(headless).is_dir()


def test_an_absent_ticket_worktree_is_reported_and_left_as_found(repo: _Repo) -> None:
    ticket = repo.add("ticket", branch=True)
    repo.respell(ticket)
    shutil.rmtree(ticket)
    before = _snapshot(repo.admin(ticket))

    result = repo.repair()

    assert len(result.problems) == 1
    assert _snapshot(repo.admin(ticket)) == before


def test_a_directory_that_lost_its_gitfile_is_kept(repo: _Repo) -> None:
    baseline = repo.add(".baseline-wt-1-abc1234", parent=repo.booley)
    repo.respell(baseline)
    (baseline / ".git").unlink()

    result = repo.repair()

    assert "lost its .git file" in result.problems[0].message()
    assert repo.admin(baseline).is_dir()


def test_a_registration_naming_no_former_mount_is_left_to_git(repo: _Repo, tmp_path: Path) -> None:
    outside = repo.add("elsewhere", parent=tmp_path / "other")
    shutil.rmtree(outside)

    assert repo.repair() == worktree_mount_repair.MountRepair()
    assert repo.admin(outside).is_dir()


def test_an_unreadable_pointer_is_reported_and_a_missing_one_skipped(repo: _Repo) -> None:
    broken, empty = repo.add("a"), repo.add("b")
    (repo.admin(broken) / "gitdir").unlink()
    (repo.admin(broken) / "gitdir").mkdir()
    (repo.admin(empty) / "gitdir").unlink()

    result = repo.repair()

    assert [problem.registration for problem in result.problems] == [repo.admin(broken)]
    assert "cannot read the pointer" in result.problems[0].message()


def test_a_failed_write_is_reported_and_the_other_registrations_still_run(
    repo: _Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = repo.add("a"), repo.add("b")
    repo.respell(first)
    repo.respell(second)
    real = worktree_mount_repair._rewrite

    def flaky(path: Path, content: bytes, *, keep_time: bool) -> None:
        if "/a/" in str(path) or path.parent.name == "a":
            raise OSError("disk full")
        real(path, content, keep_time=keep_time)

    monkeypatch.setattr(worktree_mount_repair, "_rewrite", flaky)

    result = repo.repair()

    assert "disk full" in result.problems[0].message()
    assert result.repaired == (repo.admin(second),)


def test_a_config_that_git_cannot_read_is_reported(repo: _Repo) -> None:
    checkout = repo.add("a")
    repo.respell(checkout)
    (repo.admin(checkout) / "config.worktree").write_bytes(b"[core\nbroken\n")

    result = repo.repair()

    assert "cannot rewrite its pointers" in result.problems[0].message()


def test_without_a_mount_history_nothing_runs(
    repo: _Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    (repo.booley / ".runtime" / "sandbox-mounts.json").unlink()
    monkeypatch.setattr(worktree_mount_repair, "common_git_dir", pytest.fail)

    assert repo.repair() == worktree_mount_repair.MountRepair()


def test_a_malformed_mount_history_raises(repo: _Repo) -> None:
    (repo.booley / ".runtime" / "sandbox-mounts.json").write_bytes(b"{")

    with pytest.raises(mount_history.MountHistoryError):
        repo.repair()


def test_a_project_without_git_has_nothing_to_repair(repo: _Repo, tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()

    assert repair_mount_registrations(plain) == worktree_mount_repair.MountRepair()


def test_rebase_moves_only_paths_under_a_former_mount(repo: _Repo) -> None:
    inside = repo.former / "x" / "y"

    assert mount_history.rebase(inside, [str(repo.former)]) == repo.booley / "x" / "y"
    assert mount_history.rebase(repo.root, [str(repo.former)]) is None


def test_atomic_replace_publishes_the_given_modification_time(tmp_path: Path) -> None:
    path = tmp_path / "file"
    path.write_bytes(b"old")

    atomic_files.atomic_replace_bytes(path, b"new", mtime_ns=1_000_000_000_000_000_000)

    assert path.read_bytes() == b"new"
    assert path.stat().st_mtime_ns == 1_000_000_000_000_000_000


def test_overlapping_former_mounts_repair_a_pointer_under_the_deeper_one(repo: _Repo) -> None:
    checkout = repo.add("a")
    repo.respell(checkout)
    history = repo.booley / ".runtime" / "sandbox-mounts.json"
    mounts = [str(repo.former.parents[1]), str(repo.former)]
    history.write_bytes(json.dumps({"schema": 1, "mounts": mounts}).encode())

    result = repo.repair()

    assert result.repaired == (repo.admin(checkout),)
    assert result.problems == ()
    assert _git(checkout, "rev-parse", "--show-toplevel") == str(checkout)


def test_a_hooks_path_whose_directory_does_not_exist_is_still_rebased(repo: _Repo) -> None:
    checkout = repo.add("a")
    config = repo.admin(checkout) / "config.worktree"
    hooks = repo.former / "worktrees" / "a" / "hooks"
    _git(repo.root, "config", "--file", str(config), "core.hooksPath", str(hooks))
    repo.respell(checkout)

    repo.repair()

    value = _git(repo.root, "config", "--file", str(config), "--get", "core.hooksPath")
    assert value == str(checkout / "hooks")


@pytest.mark.parametrize(
    "failure",
    [
        UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"),
        subprocess.TimeoutExpired("git config", 30),
    ],
)
def test_a_config_read_that_raises_is_reported_not_raised(
    repo: _Repo, monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    checkout = repo.add("a")
    repo.respell(checkout)
    (repo.admin(checkout) / "config.worktree").write_bytes(b"[core]\n")

    def fail(*_args: object, **_kwargs: object) -> None:
        raise failure

    monkeypatch.setattr(worktree_mount_repair, "_git_config", fail)

    result = repo.repair()

    assert "cannot rewrite its pointers" in result.problems[0].message()

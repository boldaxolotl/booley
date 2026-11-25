"""Tests for ``booley.runtime.git`` against real Git repositories and worktrees."""

from __future__ import annotations

import shlex
import subprocess
import sys
import time
from pathlib import Path

import pytest

from booley.runtime.agent_errors import BlockingError
from booley.runtime.git import (
    BOOLEY_EXCLUDE_HEADER,
    add_git_excludes,
    commit_scope,
    git_common_dir,
    git_exclude_entries,
    git_excludes_pending,
    git_run,
    is_linked_worktree,
    remove_git_excludes,
)
from tests.conftest import symlink_or_skip
from tests.git_support import hostile
from tests.harness.git_support import fake_powershell, git_stdout
from tests.pipe_holding_tree import heartbeat_tree, parent_argv, wait_until_heartbeat_stops
from tests.subprocess_budget import run_bounded_child
from tests.time_budget import scaled


def _init_repo(path: Path) -> Path:
    """Create a bare-minimum git repo with one commit. Returns its repository root."""
    run_bounded_child(["git", "init", str(path)], check=True)
    run_bounded_child(
        ["git", "-C", str(path), "config", "user.email", "test@test.com"], check=True
    )
    run_bounded_child(["git", "-C", str(path), "config", "user.name", "Test"], check=True)
    # Initial commit so HEAD exists
    (path / "init.txt").write_text("init", encoding="utf-8")
    run_bounded_child(["git", "-C", str(path), "add", "init.txt"], check=True)
    # A detached maintenance process would race later byte-for-byte repository snapshots.
    run_bounded_child(
        [
            "git",
            "-c",
            "maintenance.auto=false",
            "-c",
            "gc.auto=0",
            "-C",
            str(path),
            "commit",
            "-m",
            "init",
        ],
        check=True,
    )
    return path


def _make_worktree(repo: Path, wt_path: Path, branch: str = "test-branch") -> Path:
    """Create a linked worktree (has .git file, not .git directory)."""
    run_bounded_child(
        ["git", "-C", str(repo), "worktree", "add", str(wt_path), "-b", branch], check=True
    )
    return wt_path


@pytest.fixture
def wt(tmp_path: Path) -> Path:
    """A linked worktree, at ``tmp_path / "wt"``, of a one-commit repository at ``tmp_path``."""
    return _make_worktree(_init_repo(tmp_path), tmp_path / "wt")


def _committed(wt: Path) -> list[str]:
    return git_stdout(wt, "show", "--format=", "--name-only", "HEAD").splitlines()


def _snapshot(path: Path) -> dict[str, bytes]:
    return {str(p.relative_to(path)): p.read_bytes() for p in path.rglob("*") if p.is_file()}


@pytest.mark.parametrize(
    ("where", "linked"),
    [("wt", True), (".", False), ("wt/rtl", False), ("plain", False)],
    ids=["linked-root", "main-checkout", "linked-subdirectory", "plain-directory"],
)
def test_is_linked_worktree(wt: Path, where: str, linked: bool) -> None:
    (wt / "rtl").mkdir()
    (wt.parent / "plain").mkdir()
    assert is_linked_worktree(wt.parent / where) is linked


# ===========================================================================
# commit_scope
# ===========================================================================


@pytest.mark.parametrize(
    ("linked", "scope", "staged"),
    [(False, ["new.sv"], True), (True, [], True), (True, ["init.txt"], False)],
    ids=["main-checkout", "empty-scope", "nothing-to-commit"],
)
def test_commit_scope_without_work_changes_nothing(
    tmp_path: Path, linked: bool, scope: list[str], staged: bool
) -> None:
    """The main-checkout guard, an empty Scope and a clean tree leave HEAD and index alone."""
    repo = _init_repo(tmp_path)
    target = _make_worktree(repo, tmp_path / "wt") if linked else repo
    if staged:
        (target / "new.sv").write_text("wire x;\n", encoding="utf-8")
        git_stdout(target, "add", "new.sv")
    before = (git_stdout(target, "rev-parse", "HEAD"), git_stdout(target, "status", "--short"))

    commit_scope(target, scope, "feat: nothing to record")

    assert (
        git_stdout(target, "rev-parse", "HEAD"),
        git_stdout(target, "status", "--short"),
    ) == before


def test_literal_scope_with_pathspec_metacharacters(wt: Path) -> None:
    """Literal staging accepts real filenames that contain glob syntax."""
    literal_path = "rtl/mem[0].sv"
    (wt / "rtl").mkdir()
    (wt / literal_path).write_text("wire x;\n", encoding="utf-8")

    commit_scope(wt, [literal_path], "feat: add literal path", literal=True)

    assert _committed(wt) == [literal_path]


@pytest.mark.parametrize(
    ("stage", "left"),
    [
        (["add", "outside.sv"], "?? outside.sv"),
        (["rm", "--quiet", "init.txt"], " D init.txt"),
    ],
    ids=["added", "deleted"],
)
def test_pre_staged_outsider_is_left_uncommitted(wt: Path, stage: list[str], left: str) -> None:
    """Authorized work commits even when an unrelated addition or deletion was staged."""
    (wt / "authorized.sv").write_text("wire allowed;\n", encoding="utf-8")
    (wt / "outside.sv").write_text("wire needs_triage;\n", encoding="utf-8")
    git_stdout(wt, *stage)

    commit_scope(wt, ["authorized.sv"], "fix: preserve scoped work")

    assert _committed(wt) == ["authorized.sv"]
    status = run_bounded_child(["git", "-C", str(wt), "status", "--short"], check=True).stdout
    assert left in status.splitlines()


def test_unmatched_glob_scope_is_ignored(wt: Path) -> None:
    """Missing glob matches do not make git add fail."""
    (wt / "rtl").mkdir()
    (wt / "rtl" / "new.sv").write_text("wire x;\n", encoding="utf-8")

    commit_scope(wt, ["rtl/*.sv", "verif/lane2/*.sv"], "feat: add scoped rtl")

    assert _committed(wt) == ["rtl/new.sv"]


def test_git_add_failure_raises_blocking(wt: Path) -> None:
    with pytest.raises(BlockingError, match="git add failed"):
        commit_scope(wt, ["this_file_does_not_exist.sv"], "feat: ghost")


def test_unknown_scope_commits_every_change(wt: Path) -> None:
    """Wildcard scope ['*'] stages tracked edits and untracked files alike."""
    (wt / "init.txt").write_text("modified content\n", encoding="utf-8")
    (wt / "new.sv").write_text("wire x;\n", encoding="utf-8")

    commit_scope(wt, ["*"], "fix: wildcard commit")

    assert sorted(_committed(wt)) == ["init.txt", "new.sv"]


@pytest.mark.skipif(sys.platform == "darwin", reason="Booley has no Git process probe on macOS")
def test_active_git_owner_keeps_index_lock(wt: Path) -> None:
    """A lock with a matching live git process is left alone."""
    (wt / "new.sv").write_text("wire x;\n", encoding="utf-8")
    lock_path = Path(git_stdout(wt, "rev-parse", "--absolute-git-dir")) / "index.lock"
    lock_path.write_text("", encoding="utf-8")
    # A live Git process that names the worktree, as an interrupted add would.
    owner = subprocess.Popen(
        ["git", "-C", str(wt.resolve()), "hash-object", "--stdin"],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
    )
    try:
        with pytest.raises(BlockingError, match="git add failed"):
            commit_scope(wt, ["new.sv"], "feat: keep busy lock")
    finally:
        owner.kill()  # never leave the blocked Git process running, whatever the outcome
        owner.communicate()

    assert lock_path.exists()


# ===========================================================================
# Git excludes (ADR 0018 WS0)
# ===========================================================================


def _is_ignored(wt: Path, rel: str) -> bool:
    """True if git treats *rel* under *wt* as ignored/excluded."""
    result = run_bounded_child(["git", "-C", str(wt), "check-ignore", "-q", rel], check=False)
    return result.returncode == 0


class TestAddGitExcludes:
    def test_pending_inspection_is_read_only_and_tracks_reconciliation(self, tmp_path):
        repo = _init_repo(tmp_path / "repo")
        exclude = repo / ".git" / "info" / "exclude"
        before = exclude.read_bytes()

        assert git_excludes_pending(repo, [".devcontainer", ".booley"])
        assert exclude.read_bytes() == before

        add_git_excludes(repo, [".devcontainer", ".booley"])
        current = exclude.read_bytes()
        assert not git_excludes_pending(repo, [".devcontainer", ".booley"])
        assert exclude.read_bytes() == current

    def test_add_git_excludes_refuses_symlinked_exclude(self, tmp_path):
        repo = _init_repo(tmp_path / "repo")
        exclude = repo / ".git" / "info" / "exclude"
        destination = tmp_path / "outside-exclude"
        destination.write_bytes(b"# elsewhere\n")
        exclude.unlink()
        symlink_or_skip(exclude, destination)
        planted_target = exclude.readlink()

        assert add_git_excludes(repo, [".booley"]) is False

        assert destination.read_bytes() == b"# elsewhere\n"
        assert exclude.readlink() == planted_target
        assert git_excludes_pending(repo, [".booley"])

    def test_main_worktree_excludes_once_and_is_honored(self, tmp_path):
        repo = _init_repo(tmp_path / "repo")

        assert add_git_excludes(repo, [".devcontainer", ".booley"]) is True
        assert add_git_excludes(repo, [".devcontainer"]) is False

        body = (repo / ".git" / "info" / "exclude").read_text(encoding="utf-8")
        assert BOOLEY_EXCLUDE_HEADER in body
        assert body.count("/.devcontainer") == 1 and "/.booley" in body
        # The entries must actually be honored by git.
        assert _is_ignored(repo, ".devcontainer/devcontainer.json")
        assert _is_ignored(repo, ".booley/booley.toml")

    def test_later_entries_reuse_one_generated_block(self, tmp_path):
        repo = _init_repo(tmp_path / "repo")

        add_git_excludes(repo, [".devcontainer", ".booley", ".claude"])
        add_git_excludes(repo, [".booley-projected-*.core"])

        body = (repo / ".git" / "info" / "exclude").read_text(encoding="utf-8")
        assert body.count(BOOLEY_EXCLUDE_HEADER) == 1
        block = body.split(BOOLEY_EXCLUDE_HEADER, 1)[1].strip().splitlines()
        assert block == [
            "/.devcontainer",
            "/.booley",
            "/.claude",
            "/.booley-projected-*.core",
        ]

    def test_repairs_duplicate_generated_headers(self, tmp_path):
        repo = _init_repo(tmp_path / "repo")
        exclude = repo / ".git" / "info" / "exclude"
        exclude.write_text(
            f"{BOOLEY_EXCLUDE_HEADER}\n/.devcontainer\n\n"
            f"{BOOLEY_EXCLUDE_HEADER}\n/.booley-projected-*.core\n",
            encoding="utf-8",
        )

        assert add_git_excludes(repo, [".devcontainer"]) is True
        assert exclude.read_text(encoding="utf-8").count(BOOLEY_EXCLUDE_HEADER) == 1

    def test_linked_worktree_writes_to_common_dir_and_is_honored(self, wt: Path):
        """The crux: a linked worktree honors the COMMON-dir info/exclude only.

        Git treats ``info/`` as a shared path, so the per-worktree
        ``.git/worktrees/<id>/info/exclude`` is ignored. add_git_excludes must
        target ``$GIT_COMMON_DIR/info/exclude`` for the exclude to take effect.
        """
        assert add_git_excludes(wt, [".devcontainer"]) is True

        common_exclude = wt.parent / ".git" / "info" / "exclude"
        assert "/.devcontainer" in common_exclude.read_text(encoding="utf-8")
        assert _is_ignored(wt, ".devcontainer/devcontainer.json")


def test_git_helpers_keep_the_intended_linked_worktree_under_foreign_selectors(
    tmp_path, monkeypatch
):
    """Inherited GIT_DIR-style variables never redirect a helper to another repository."""
    main = _init_repo(tmp_path / "main")
    linked = _make_worktree(main, tmp_path / "linked")
    foreign = _init_repo(tmp_path / "foreign")
    lock = Path(git_stdout(linked, "rev-parse", "--absolute-git-dir")) / "index.lock"
    lock.write_bytes(b"stale fixture")
    before = _snapshot(foreign)
    for key, value in hostile(foreign).items():
        monkeypatch.setenv(key, value)

    assert git_common_dir(linked) == (main / ".git").resolve()
    assert git_run(linked, ["symbolic-ref", "--short", "HEAD"]).stdout.strip() == "test-branch"
    result = git_run(linked, ["add", "init.txt"])  # retried after removing the stale lock
    assert result.returncode == 0, result.stderr
    assert not lock.exists()
    assert _snapshot(foreign) == before


def test_remove_git_excludes_keeps_other_entries(tmp_path):
    """Only the named lines go; the header and sibling entries stay."""
    repo = _init_repo(tmp_path / "repo")
    add_git_excludes(repo, [".devcontainer", ".booley", ".claude"])
    exclude = repo / ".git" / "info" / "exclude"

    assert git_exclude_entries(repo, [".booley", ".missing"]) == ("/.booley",)
    remove_git_excludes(repo, ["/.booley"])

    body = exclude.read_text(encoding="utf-8")
    assert BOOLEY_EXCLUDE_HEADER in body
    assert "/.booley" not in body.splitlines()
    assert {"/.devcontainer", "/.claude"} <= set(body.splitlines())
    assert git_exclude_entries(repo, [".booley"]) == ()
    before = exclude.read_bytes()
    remove_git_excludes(repo, ["/.booley"])  # nothing left to remove: no rewrite
    assert exclude.read_bytes() == before


def test_git_exclude_helpers_without_git_or_exclude_file(tmp_path):
    """Outside a repository, or with no exclude file, every helper is a no-op."""
    plain = tmp_path / "plain"
    plain.mkdir()
    assert add_git_excludes(plain, [".booley"]) is False
    assert git_exclude_entries(plain, [".booley"]) == ()
    remove_git_excludes(plain, ["/.booley"])

    repo = _init_repo(tmp_path / "repo")
    exclude = repo / ".git" / "info" / "exclude"
    exclude.unlink()
    assert git_exclude_entries(repo, [".booley"]) == ()
    remove_git_excludes(repo, ["/.booley"])
    assert not exclude.exists()


def test_remove_git_excludes_refuses_symlinked_exclude(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    add_git_excludes(repo, [".booley"])
    exclude = repo / ".git" / "info" / "exclude"
    destination = tmp_path / "outside-exclude"
    destination.write_bytes(exclude.read_bytes())
    exclude.unlink()
    symlink_or_skip(exclude, destination)
    before = destination.read_bytes()

    with pytest.raises(OSError, match="symlink"):
        remove_git_excludes(repo, ["/.booley"])

    assert destination.read_bytes() == before


def test_git_exclude_entries_ignores_an_undecodable_exclude_file(tmp_path):
    """A non-UTF-8 info/exclude holds no removable footprint: no crash, no entries."""
    repo = _init_repo(tmp_path / "repo")
    (repo / ".git" / "info" / "exclude").write_bytes(b"/.booley\n\xff\xfe\n")
    assert git_exclude_entries(repo, [".booley"]) == ()


# ---------------------------------------------------------------------------
# Bounded capture (#3860)
# ---------------------------------------------------------------------------


def _hanging_alias(argv: list[str]) -> list[str]:
    """Git arguments running *argv* as a ``!`` shell alias, so Git is the hung command's parent."""
    # POSIX spellings for the interpreter and script, so Git for Windows ``sh`` accepts them.
    command = " ".join(
        shlex.quote(Path(part).as_posix() if index < 2 else part)
        for index, part in enumerate(argv)
    )
    return ["-c", f"alias.hang=!{command}", "hang"]


def test_git_run_timeout_kills_the_tree_and_returns_promptly(tmp_path: Path) -> None:
    """A hung Git tree (git -> sh -> python -> heartbeat) is killed at the timeout."""
    with heartbeat_tree(tmp_path) as paths:
        argv = parent_argv(tmp_path, sys.executable, paths, "live-until-stop")
        start = time.monotonic()
        with pytest.raises(subprocess.TimeoutExpired):
            git_run(tmp_path, _hanging_alias(argv), timeout=scaled(5))
        elapsed = time.monotonic() - start
        # Timeout plus the helper's documented Windows worst case (~32 s), inside the test budget.
        assert elapsed < scaled(40), f"git_run took {elapsed:.1f}s to give up"
        assert paths["heartbeat"].exists(), "the hung command never started"
        assert wait_until_heartbeat_stops(paths["heartbeat"]), "a descendant is still running"


def test_git_run_keeps_its_result_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Success, non-zero exit and a missing executable behave as before the bounded helper."""
    repo = _init_repo(tmp_path / "repo")
    ok = git_run(repo, ["rev-parse", "--is-inside-work-tree"])
    assert (ok.returncode, ok.stdout.strip()) == (0, "true")
    failed = git_run(repo, ["rev-parse", "--verify", "no-such-ref"])
    assert failed.returncode != 0
    assert isinstance(failed.stderr, str)
    monkeypatch.setenv("PATH", "")
    with pytest.raises(FileNotFoundError):
        git_run(repo, ["status"])


@pytest.mark.skipif(sys.platform == "win32", reason="needs a POSIX fake powershell")
@pytest.mark.parametrize(
    ("script", "mode", "kept"),
    [
        ("exec sleep 60", 0o755, True),
        ("exit 1", 0o755, True),
        ("exit 0", 0o644, True),
        ("exit 0", 0o755, False),
    ],
)
def test_windows_git_process_check_decides_the_index_lock(
    wt: Path, tmp_path: Path, monkeypatch, caplog, script: str, mode: int, kept: bool
) -> None:
    (wt / "new.sv").write_text("wire x;\n", encoding="utf-8")
    lock_path = Path(git_stdout(wt, "rev-parse", "--absolute-git-dir")) / "index.lock"
    lock_path.write_text("", encoding="utf-8")
    fake_powershell(tmp_path, monkeypatch, script, mode)
    monkeypatch.setattr(sys, "platform", "win32")
    if kept:
        with pytest.raises(BlockingError, match="git add failed"):
            commit_scope(wt, ["new.sv"], "feat: keep unchecked lock")
        assert "Could not inspect Git processes" in caplog.text
    else:
        commit_scope(wt, ["new.sv"], "feat: clear stale lock")
        assert _committed(wt) == ["new.sv"]
    assert lock_path.exists() is kept


@pytest.mark.skipif(
    sys.platform == "win32", reason="a fake `powershell` script cannot shadow powershell.exe"
)
def test_windows_git_process_probe_stops_a_hung_tree(tmp_path: Path, monkeypatch) -> None:
    from booley.runtime.git import _GitProcessCheckError, _iter_git_process_cmdlines_windows

    with heartbeat_tree(tmp_path) as paths:
        argv = parent_argv(tmp_path, sys.executable, paths, "live-until-stop")
        fake_powershell(tmp_path, monkeypatch, f"exec {shlex.join(argv)}")
        started = time.monotonic()

        with pytest.raises(_GitProcessCheckError):
            _iter_git_process_cmdlines_windows()

        assert time.monotonic() - started < scaled(38)
        assert wait_until_heartbeat_stops(paths["heartbeat"]), "the descendant was left running"

"""`booley worktree list`: branch, kind, changes, unmerged work, sessions, Goal, last use."""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

import pytest

from booley.harness import booley as tlr
from booley.harness import worktree_cmd
from tests.goals.conftest import git

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A Project whose main checkout holds one commit."""
    monkeypatch.delenv("BOOLEY_DIR", raising=False)
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    # A developer's global ignore file may hide `.booley`; these tests count what Git reports.
    no_excludes = tmp_path / "no-excludes"
    no_excludes.write_bytes(b"")
    git(root, "config", "core.excludesFile", str(no_excludes))
    (root / "base.txt").write_bytes(b"base\n")
    git(root, "add", "base.txt")
    git(root, "commit", "-qm", "base")
    return root


def _add(root: Path, name: str, *, detach: bool = False) -> Path:
    """Register `.booley/worktrees/<name>` on booley/<name> (or detached) at main's HEAD."""
    path = root / ".booley" / "worktrees" / name
    if detach:
        git(root, "worktree", "add", "-q", "--detach", str(path), "HEAD")
    else:
        git(root, "worktree", "add", "-q", "-b", f"booley/{name}", str(path), "HEAD")
    return path


def _commit(path: Path, name: str, content: bytes = b"x\n") -> None:
    (path / name).write_bytes(content)
    git(path, "add", name)
    git(path, "commit", "-qm", f"add {name}")


def _collect(root: Path, now: float | None = None):
    """Import lazily so a missing module fails inside the test, not at collection."""
    from booley.harness import worktree_list

    states, diagnostics = worktree_list.collect_worktrees(
        root, now=time.time() if now is None else now
    )
    return {state.name: state for state in states}, diagnostics


def test_list_reports_branch_kind_changes_and_unmerged_work(repo: Path) -> None:
    snapshot = _add(repo, "a")
    _commit(snapshot, "c.txt")
    (snapshot / "base.txt").write_bytes(b"edited\n")
    (snapshot / "new.txt").write_bytes(b"new\n")
    # The Snapshot's copied Booley Directory is untracked but is not the agent's work.
    (snapshot / ".booley").mkdir()
    (snapshot / ".booley" / "booley.toml").write_bytes(b"# copy\n")
    # A Versioned Booley Directory: the Main Booley Directory is its own repository.
    main_booley = repo / ".booley"
    git(main_booley, "init", "-q", "-b", "main")
    (main_booley / "booley.toml").write_bytes(b"# main\n")
    git(main_booley, "add", "booley.toml")
    git(main_booley, "commit", "-qm", "project")
    paired = _add(repo, "p")
    git(main_booley, "worktree", "add", "-q", "-b", "booley-worktree/p", str(paired / ".booley"))
    _commit(paired / ".booley", "paired.txt")
    (paired / ".booley" / "booley.toml").write_bytes(b"# dirty\n")

    states, _ = _collect(repo)

    a, p = states["a"], states["p"]
    assert (
        a.branch,
        a.kind,
        a.uncommitted,
        a.paired_uncommitted,
        a.unmerged,
        a.paired_unmerged,
    ) == (
        "booley/a",
        "Snapshot",
        2,
        None,
        1,
        None,
    )
    assert (
        p.branch,
        p.kind,
        p.uncommitted,
        p.paired_uncommitted,
        p.unmerged,
        p.paired_unmerged,
    ) == (
        "booley/p",
        "Paired Checkout",
        0,
        1,
        0,
        1,
    )


def test_squash_merged_branch_counts_as_merged(repo: Path) -> None:
    squashed = _add(repo, "sq")
    picked = _add(repo, "cp")
    _commit(squashed, "s1.txt")
    # Latin-1 bytes are not valid UTF-8: the patch-id pipeline must stay bytes-level.
    _commit(squashed, "s2.txt", b"caf\xe9\n")
    _commit(picked, "c.txt")
    states, _ = _collect(repo)
    assert (states["sq"].unmerged, states["cp"].unmerged) == (2, 1)

    git(repo, "merge", "--squash", "booley/sq")
    git(repo, "commit", "-qm", "squash of sq")
    git(repo, "cherry-pick", "booley/cp")
    states, _ = _collect(repo)
    assert (states["sq"].unmerged, states["cp"].unmerged) == (0, 0)

    _commit(squashed, "s3.txt")
    states, _ = _collect(repo)
    assert states["sq"].unmerged == 1


def test_squash_of_a_merged_in_side_branch_leaves_the_branchs_own_commit_unmerged(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A positional prefix of the date-ordered commits would swallow `c1` with the side commit."""
    own = _add(repo, "own")
    side = _add(repo, "side")
    monkeypatch.setenv("GIT_COMMITTER_DATE", "2026-01-01T00:00:00Z")
    _commit(own, "own.txt")
    monkeypatch.setenv("GIT_COMMITTER_DATE", "2026-01-02T00:00:00Z")
    _commit(side, "side.txt")
    git(own, "merge", "--no-ff", "-qm", "merge side", "booley/side")

    git(repo, "merge", "--squash", "booley/side")
    git(repo, "commit", "-qm", "squash of side")
    states, _ = _collect(repo)

    assert states["own"].unmerged == 1


def test_squash_of_a_branch_that_merged_main_in_counts_as_merged(repo: Path) -> None:
    merged = _add(repo, "mm")
    _commit(merged, "a.txt")
    _commit(merged, "b.txt")
    _commit(repo, "main-only.txt")
    git(merged, "merge", "-qm", "merge main", "main")
    states, _ = _collect(repo)
    assert states["mm"].unmerged == 2

    git(repo, "merge", "--squash", "booley/mm")
    git(repo, "commit", "-qm", "squash of mm")
    states, _ = _collect(repo)

    assert states["mm"].unmerged == 0


def test_a_goal_store_failure_is_a_diagnostic_not_a_crash(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.goals.store import GoalStore, GoalStoreError

    def fail(self: GoalStore, worktree: Path) -> None:
        raise GoalStoreError("two Goal Records claim this worktree")

    monkeypatch.setattr(GoalStore, "active_for_worktree", fail)
    _add(repo, "w")

    states, diagnostics = _collect(repo)

    assert states["w"].goal is None
    assert any("Goal Mode of worktree w is unavailable" in line for line in diagnostics)


def test_a_project_without_a_session_registry_has_no_registry_diagnostic(repo: Path) -> None:
    _add(repo, "w")

    _, diagnostics = _collect(repo)

    assert not any("session registry" in line for line in diagnostics)


def test_detached_worktree_is_listed_as_detached(repo: Path) -> None:
    from booley.harness import worktree_list

    old = _add(repo, "old", detach=True)
    _commit(old, "old.txt")

    states, _ = _collect(repo)

    assert (states["old"].branch, states["old"].unmerged) == (None, 1)
    rendered = worktree_list.render([states["old"]], now=time.time())
    assert rendered.splitlines()[1].split()[:2] == ["old", "detached"]


@pytest.mark.skipif(not Path("/proc/self/ns/pid").exists(), reason="session scope needs procfs")
def test_list_reports_live_session_active_goal_and_last_used(repo: Path) -> None:
    from booley.mcp.session_registry import Attribution, SessionRegistry, namespace
    from booley.runtime.handles import HandleStore
    from booley.runtime.worktree_usage import stamp_worktree
    from tests.goals.test_store import _enter

    busy = _add(repo, "busy")
    _add(repo, "idle")
    common = repo / ".booley"
    now = time.time()
    SessionRegistry(common, quiet_after=3600).upsert(
        Attribution("codex:one", "thread", str(busy), "key", namespace=namespace()),
        "sim",
        "completed",
        now=now,
    )
    label = HandleStore(common).reserve("codex:one", "codex")
    from booley.goals.store import GoalStore

    _enter(GoalStore(common), busy, "fix-20261006T101500Z")
    stamp_worktree(common, "busy")
    stamp = common / "worktrees" / ".last-used" / "busy"
    os.utime(stamp, (now - 120, now - 120))

    states, _ = _collect(repo, now)

    assert (states["busy"].sessions, states["busy"].goal) == ((label,), "fix-20261006T101500Z")
    assert states["busy"].last_used == pytest.approx(now - 120, abs=1)
    assert (states["idle"].sessions, states["idle"].goal, states["idle"].last_used) == (
        (),
        None,
        None,
    )


def test_list_command_prints_one_row_per_worktree(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _add(repo, "a")
    _commit(_add(repo, "old", detach=True), "old.txt")
    args = tlr._build_parser().parse_args(["worktree", "list"])

    assert worktree_cmd.run(args, repo) == 0

    assert capsys.readouterr().out.splitlines() == [
        "NAME  BRANCH    BOOLEY DIR  UNCOMMITTED  UNMERGED  SESSION  GOAL  LAST USED",
        "a     booley/a  Snapshot    0            0         -        -     never",
        "old   detached  Snapshot    0            1         -        -     never",
    ]


def test_registered_worktree_with_missing_directory_is_a_diagnostic_not_a_row(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from booley.harness import worktree_list

    shutil.rmtree(_add(repo, "gone"))

    assert worktree_list.run(repo) == 0

    captured = capsys.readouterr()
    assert captured.out == "No worktrees.\n"
    assert "WARNING: worktree gone is registered but its directory is missing" in captured.err


def test_list_outside_git_reports_one_error_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from booley.harness import worktree_list

    assert worktree_list.run(tmp_path) == 1

    err = capsys.readouterr().err.splitlines()
    assert len(err) == 1 and err[0].startswith("ERROR: git ")

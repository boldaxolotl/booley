"""`booley worktree rm`: refusals, evidence, ordering, submodules, and interrupted removals."""

from __future__ import annotations

import importlib
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import ModuleType

import pytest

from booley.harness import booley as tlr
from booley.harness import worktree_cmd
from booley.runtime.handles import reserve_session_handle
from booley.runtime.pid import ProcessIdentity, capture_process_identity
from booley.runtime.session_activity import process_session_key
from tests.goals.conftest import git

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")

NAMESPACE = "pid:[4026531836]"


def _module() -> ModuleType:
    """Import lazily so a missing module fails inside the test, not at collection."""
    return importlib.import_module("booley.harness.worktree_rm")


def _pin_namespace(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make Sessions of `NAMESPACE` visible, on hosts with or without procfs."""
    monkeypatch.setattr(_module(), "namespace", lambda: NAMESPACE)


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


def _versioned(root: Path) -> Path:
    """Make the Main Booley Directory its own repository, as a Versioned Booley Directory is."""
    booley_dir = root / ".booley"
    booley_dir.mkdir(exist_ok=True)
    git(booley_dir, "init", "-q", "-b", "main")
    (booley_dir / "booley.toml").write_bytes(b"# main\n")
    # Common Booley State is ignored in a real Booley Directory; `plans/` stays tracked.
    (booley_dir / ".gitignore").write_bytes(
        b"/handoffs/\n/reviewer-evidence/\n/flow-reports/\n/.runtime/\n/findings.jsonl\n"
        b"/setup-evidence/\n"
    )
    git(booley_dir, "add", "booley.toml", ".gitignore")
    git(booley_dir, "commit", "-qm", "project")
    return booley_dir


def _add(root: Path, name: str, *, paired: bool = False, detach: bool = False) -> Path:
    """Register `.booley/worktrees/<name>`, with a Paired Checkout when asked."""
    path = root / ".booley" / "worktrees" / name
    if detach:
        git(root, "worktree", "add", "-q", "--detach", str(path), "HEAD")
    else:
        git(root, "worktree", "add", "-q", "-b", f"booley/{name}", str(path), "HEAD")
    if paired:
        git(
            root / ".booley",
            "worktree",
            "add",
            "-q",
            "-b",
            f"booley-worktree/{name}",
            str(path / ".booley"),
        )
    return path


def _commit(path: Path, name: str, content: bytes = b"x\n") -> None:
    (path / name).write_bytes(content)
    git(path, "add", name)
    git(path, "commit", "-qm", f"add {name}")


def _rm(root: Path, name: str, *flags: str) -> int:
    _module()
    args = tlr._build_parser().parse_args(["worktree", "rm", name, *flags])
    return worktree_cmd.run(args, root)


def _registered(repository: Path) -> list[str]:
    listing = git(repository, "worktree", "list", "--porcelain")
    # Git prints forward slashes on Windows; Path() normalizes to the native form.
    return [str(Path(line[9:])) for line in listing.splitlines() if line.startswith("worktree ")]


def _branches(repository: Path) -> set[str]:
    return set(git(repository, "branch", "--format=%(refname:short)").split())


def _evidence_dirs(root: Path, name: str) -> list[Path]:
    return sorted((root / ".booley" / "worktrees" / ".evidence").glob(f"{name}-*"))


def _session(root: Path, work_dir: Path) -> None:
    from booley.mcp.session_registry import Attribution, SessionRegistry

    SessionRegistry(root / ".booley", quiet_after=3600).upsert(
        Attribution("codex:one", "thread", str(work_dir), "key", namespace=NAMESPACE),
        "sim",
        "completed",
        now=time.time(),
    )


def _busy(root: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Worktree `s3` with a Paired Checkout and every reason to refuse removal."""
    _pin_namespace(monkeypatch)
    _versioned(root)
    path = _add(root, "s3", paired=True)
    _commit(path, "outer.txt")
    (path / "base.txt").write_bytes(b"edited\n")
    nested = path / ".booley"
    _commit(nested, "paired.txt")
    (nested / "booley.toml").write_bytes(b"# dirty\n")
    (nested / "handoffs").mkdir()
    (nested / "handoffs" / "h.md").write_bytes(b"handoff\n")
    _session(root, path)
    from booley.goals.store import GoalStore
    from tests.goals.test_store import _enter

    _enter(GoalStore(root / ".booley"), path, "fix-20261006T101500Z")
    return path


def test_rm_refuses_and_names_every_reason(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _busy(repo, monkeypatch)
    registered = _registered(repo)

    assert _rm(repo, "s3") == 1

    err = capsys.readouterr().err
    for reason in (
        "refusing to remove worktree s3",
        "live Session",
        "active Goal Record fix-20261006T101500Z",
        "uncommitted change(s) in the worktree",
        "uncommitted change(s) in its Paired Checkout",
        "commit(s) on booley/s3 not in main",
        "commit(s) on booley-worktree/s3 not in main",
        "rerun with --force",
    ):
        assert reason in err
    assert path.is_dir() and (path / ".booley" / "handoffs" / "h.md").is_file()
    assert _registered(repo) == registered
    assert not _evidence_dirs(repo, "s3")


def test_force_removes_despite_every_reason(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _busy(repo, monkeypatch)

    assert _rm(repo, "s3", "--force") == 0

    out = capsys.readouterr().out
    assert "Removed worktree s3." in out
    assert "Branches kept: booley-worktree/s3, booley/s3" in out
    assert not path.exists()
    assert _registered(repo) == [str(repo)]
    assert _registered(repo / ".booley") == [str(repo / ".booley")]
    (kept,) = _evidence_dirs(repo, "s3")
    assert (kept / "handoffs" / "h.md").read_bytes() == b"handoff\n"
    assert "booley/s3" in _branches(repo) and "booley-worktree/s3" in _branches(repo / ".booley")


def test_rm_refuses_unmerged_commits_on_a_goal_branch(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _add(repo, "g")
    git(path, "switch", "-q", "-c", "booley/goal-fix-20261006T101500Z")
    _commit(path, "goal.txt")

    assert _rm(repo, "g") == 1

    assert "1 commit(s) on booley/goal-fix-20261006T101500Z not in main" in capsys.readouterr().err


def _hold_handle(root: Path, identity: ProcessIdentity) -> str:
    """Reserve the Session handle of *identity*'s process, as the agent's launch does."""
    return reserve_session_handle(root / ".booley", process_session_key(identity))


def test_rm_refuses_a_worktree_whose_handle_holder_is_alive(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Bug caught: a running agent that made no Booley tool call has no registry row, so
    # `rm` (and launch-time `gc`) would remove its worktree from under it.
    _pin_namespace(monkeypatch)
    identity = capture_process_identity(os.getpid())
    assert identity is not None
    assert _hold_handle(repo, identity) == "S1"  # before the worktree: a taken number is skipped
    path = _add(repo, "s1")

    assert _rm(repo, "s1") == 1

    assert "  - live Session S1\n" in capsys.readouterr().err
    assert path.is_dir()


def test_rm_ignores_a_handle_whose_holder_has_exited(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The reaped agent of a finished session must not block its own worktree's removal.
    _pin_namespace(monkeypatch)
    holder = subprocess.Popen([sys.executable, "-c", "input()"], stdin=subprocess.PIPE)
    try:
        identity = capture_process_identity(holder.pid)
        assert identity is not None
        assert _hold_handle(repo, identity) == "S1"
    finally:
        holder.communicate(b"\n", timeout=30)
    path = _add(repo, "s1")

    assert _rm(repo, "s1") == 0

    assert "Removed worktree s1." in capsys.readouterr().out
    assert not path.exists()


def test_rm_refuses_when_goal_state_is_unreadable(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from booley.goals.store import GoalStore, GoalStoreError

    def fail(self: GoalStore, worktree: Path) -> None:
        raise GoalStoreError("two Goal Records claim this worktree")

    monkeypatch.setattr(GoalStore, "active_for_worktree", fail)
    _add(repo, "w")

    assert _rm(repo, "w") == 1

    err = capsys.readouterr().err
    assert "Goal Mode state is unreadable: two Goal Records claim this worktree" in err


def test_rm_counts_a_session_in_the_paired_checkout(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _pin_namespace(monkeypatch)
    _versioned(repo)
    path = _add(repo, "p", paired=True)
    _session(repo, path / ".booley")

    assert _rm(repo, "p") == 1

    assert "live Session" in capsys.readouterr().err


def test_rm_refuses_an_unreadable_session_registry(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _pin_namespace(monkeypatch)
    _add(repo, "w")
    sessions = repo / ".booley" / "runtime" / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "broken.json").write_bytes(b"{not json")

    assert _rm(repo, "w") == 1

    assert "Session Registry is unreadable" in capsys.readouterr().err


def test_rm_refuses_a_git_locked_worktree_unless_forced(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _add(repo, "l")
    git(repo, "worktree", "lock", "--reason", "on a usb stick", str(path))

    assert _rm(repo, "l") == 1
    assert "locked by Git (on a usb stick)" in capsys.readouterr().err
    assert path.is_dir()

    assert _rm(repo, "l", "--force") == 0
    assert not path.exists() and _registered(repo) == [str(repo)]


def test_rm_refuses_a_broken_worktree_unless_forced(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _add(repo, "b")
    (path / ".git").unlink()

    def not_consulted(*_args: object) -> int:
        raise AssertionError("a broken checkout must not be counted through the main checkout")

    monkeypatch.setattr(_module(), "count_uncommitted", not_consulted)

    assert _rm(repo, "b") == 1
    assert "not a working Git checkout" in capsys.readouterr().err

    assert _rm(repo, "b", "--force") == 0
    assert not path.exists() and _registered(repo) == [str(repo)]


def test_rm_refuses_a_registration_whose_directory_is_gone(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    shutil.rmtree(_add(repo, "gone"))

    assert _rm(repo, "gone") == 1
    assert "registered, but its directory is missing" in capsys.readouterr().err

    assert _rm(repo, "gone", "--force") == 0
    assert _registered(repo) == [str(repo)]


def _submodule_project(root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Main checkout with a `rtl/ip` submodule, as the packaged worktree creation fills it."""
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "file")
    dependency = tmp_path / "dependency"
    dependency.mkdir()
    git(dependency, "init", "-q", "-b", "main")
    (dependency / "ip.sv").write_bytes(b"module ip;\nendmodule\n")
    git(dependency, "add", "ip.sv")
    git(dependency, "commit", "-qm", "ip")
    git(root, "-c", "protocol.file.allow=always", "submodule", "add", str(dependency), "rtl/ip")
    git(root, "commit", "-qm", "add ip")


def test_rm_refuses_a_submodule_commit_missing_from_main(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from booley.runtime.submodule_materialization import materialize_worktree_submodules

    _submodule_project(repo, tmp_path, monkeypatch)
    path = _add(repo, "sm")
    materialize_worktree_submodules(path)
    git(path / "rtl" / "ip", "commit", "--allow-empty", "-qm", "only here")
    git(path, "add", "rtl/ip")
    git(path, "commit", "-qm", "bump ip")
    git(repo, "merge", "-q", "booley/sm")

    assert _rm(repo, "sm") == 1

    err = capsys.readouterr().err
    assert "submodule rtl/ip: commit " in err and "is not in the main checkout's rtl/ip" in err
    assert "commit(s) on" not in err


def test_rm_refuses_a_populated_submodule_that_main_has_not_populated(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from booley.runtime.submodule_materialization import materialize_worktree_submodules

    _submodule_project(repo, tmp_path, monkeypatch)
    path = _add(repo, "sm")
    materialize_worktree_submodules(path)
    (repo / "rtl" / "ip" / ".git").unlink()

    assert _rm(repo, "sm") == 1
    assert "submodule rtl/ip" in capsys.readouterr().err


def test_populated_submodule_does_not_block_removal(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.runtime.submodule_materialization import materialize_worktree_submodules

    _submodule_project(repo, tmp_path, monkeypatch)
    path = _add(repo, "sm")
    materialize_worktree_submodules(path)
    assert (path / "rtl" / "ip" / ".git").exists()

    assert _rm(repo, "sm") == 0

    assert not path.exists() and _registered(repo) == [str(repo)]


def test_squash_merged_branch_does_not_block_removal(repo: Path) -> None:
    """Both the outer branch and the Paired Checkout's branch may be squash merged."""
    _versioned(repo)
    for name, paired in (("sq-outer", False), ("sq-paired", True)):
        path = _add(repo, name, paired=paired)
        checkout = path / ".booley" if paired else path
        _commit(checkout, "s1.txt")
        _commit(checkout, "s2.txt")
        upstream = repo / ".booley" if paired else repo
        branch = f"booley-worktree/{name}" if paired else f"booley/{name}"
        git(upstream, "merge", "--squash", branch)
        git(upstream, "commit", "-qm", f"squash of {name}")

        assert _rm(repo, name) == 0, name

        assert not path.exists()


def _evidence_files(booley_dir: Path) -> dict[str, bytes]:
    files = {
        "handoffs/h.md": b"handoff\n",
        "reviewer-evidence/r.json": b'{"ok": true}\n',
        "flow-reports/sim/1/artifacts/candidate/trace.fst": b"\x00\x01trace\xff",
        ".runtime/mcp-tool-reports/x/1/report.json": b"{}\n",
        "findings.jsonl": b'{"finding": 1}\n',
        "setup-evidence/attachments/rows/a.txt": b"attachment\n",
    }
    for relative, content in files.items():
        target = booley_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    return files


def test_evidence_moves_to_common_state(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A Snapshot's and a Paired Checkout's evidence both land in Common Booley State."""
    for name, paired in (("ev-snapshot", False), ("ev-paired", True)):
        if paired:
            # A Snapshot's Booley Directory is not versioned; the Paired Checkout's is.
            _versioned(repo)
        path = _add(repo, name, paired=paired)
        files = _evidence_files(path / ".booley")
        if paired:
            # Tracked files are on the kept branch: they are not evidence to move.
            (path / ".booley" / "plans").mkdir()
            _commit(path / ".booley", "plans/tracked.md")
            git(repo / ".booley", "merge", "-q", f"booley-worktree/{name}")

        assert _rm(repo, name) == 0, name

        (kept,) = _evidence_dirs(repo, name)
        assert {
            item.relative_to(kept).as_posix(): item.read_bytes()
            for item in kept.rglob("*")
            if item.is_file()
        } == files
        assert f"Evidence kept in {kept}" in capsys.readouterr().out
        assert not path.exists()


def test_paired_checkout_is_removed_before_the_outer_worktree(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    removal = _module()
    real_removal = removal.remove_worktree_registration
    _versioned(repo)
    path = _add(repo, "ord", paired=True)

    def interrupt_outer(repository: Path, worktree: Path) -> None:
        if worktree == path:
            raise KeyboardInterrupt
        real_removal(repository, worktree)

    monkeypatch.setattr(removal, "remove_worktree_registration", interrupt_outer)

    with pytest.raises(KeyboardInterrupt):
        _rm(repo, "ord")

    assert str(path / ".booley") not in _registered(repo / ".booley")
    assert str(path) in _registered(repo)
    assert "booley-worktree/ord" in _branches(repo / ".booley")


def test_rm_keeps_branches_and_other_registrations(repo: Path) -> None:
    _versioned(repo)
    _add(repo, "a", paired=True)
    other = _add(repo, "b")
    shutil.rmtree(other)

    assert _rm(repo, "a") == 0

    assert "booley/a" in _branches(repo)
    assert "booley-worktree/a" in _branches(repo / ".booley")
    # No prune: `b`'s directory is gone by hand and its registration is still there.
    assert str(other) in _registered(repo)


def test_rm_refuses_a_nested_worktree_even_with_force(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _add(repo, "host")
    nested = path / ".booley" / ".baseline-wt-1-abc"
    git(repo, "worktree", "add", "-q", "-b", "baseline", str(nested), "HEAD")
    registered = _registered(repo)

    for flags in ((), ("--force",)):
        assert _rm(repo, "host", *flags) == 1
        err = capsys.readouterr().err
        assert str(nested) in err and "Remove them first" in err

    assert path.is_dir() and nested.is_dir() and _registered(repo) == registered


def test_rm_reports_unknown_names_and_refuses_from_inside(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _add(repo, "w")

    assert _rm(repo, "nope") == 1
    assert "no worktree named nope" in capsys.readouterr().err

    monkeypatch.chdir(path)
    assert _rm(repo, "w", "--force") == 1
    assert "run booley worktree rm from the main checkout or another worktree" in (
        capsys.readouterr().err
    )
    assert path.is_dir()


def test_rm_works_from_another_worktree(repo: Path) -> None:
    keeper = _add(repo, "keeper")
    gone = _add(repo, "gone")
    _module()

    args = tlr._build_parser().parse_args(["worktree", "rm", "gone"])
    assert worktree_cmd.run(args, keeper) == 0

    assert keeper.is_dir() and not gone.exists()


def test_rm_reports_the_commit_of_a_detached_head(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _add(repo, "old", detach=True)

    assert _rm(repo, "old") == 0

    assert f"detached HEAD was {git(repo, 'rev-parse', 'HEAD')}" in capsys.readouterr().out


def _start_interrupted_removal(
    repo: Path, monkeypatch: pytest.MonkeyPatch, path: Path, name: str
) -> None:
    """Interrupt the outer removal after it damaged the worktree, as a killed `rm` would."""
    removal = _module()
    real_removal = removal.remove_worktree_registration

    def damage_then_die(repository: Path, worktree: Path) -> None:
        if worktree != path:
            real_removal(repository, worktree)
            return
        # Git was cut short after deleting some files; `rm` had dropped `.git` first.
        assert not (worktree / ".git").exists()
        (worktree / "base.txt").unlink()
        raise KeyboardInterrupt

    with monkeypatch.context() as patch:
        patch.setattr(removal, "remove_worktree_registration", damage_then_die)
        with pytest.raises(KeyboardInterrupt):
            _rm(repo, name)


def test_interrupted_outer_removal_completes_on_rerun(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.runtime.worktree_usage import stamp_worktree

    _versioned(repo)
    path = _add(repo, "cut", paired=True)
    other = _add(repo, "other")
    stamp_worktree(repo / ".booley", "cut")
    _start_interrupted_removal(repo, monkeypatch, path, "cut")
    record = repo / ".booley" / "worktrees" / ".removing" / "cut.json"
    assert record.is_file()

    assert _rm(repo, "cut") == 0

    assert not path.exists() and str(path) not in _registered(repo)
    assert not record.exists()
    assert not (repo / ".booley" / "worktrees" / ".last-used" / "cut").exists()
    assert str(other) in _registered(repo)


def test_interruption_before_any_deletion_rechecks(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _versioned(repo)
    path = _add(repo, "twice", paired=True)

    def die(_source: Path, _worktree: Path) -> None:
        raise KeyboardInterrupt

    with monkeypatch.context() as patch:
        patch.setattr(_module(), "remove_paired_checkout", die)
        with pytest.raises(KeyboardInterrupt):
            _rm(repo, "twice")
    (path / "base.txt").write_bytes(b"added since\n")

    assert _rm(repo, "twice") == 1

    assert "1 uncommitted change(s) in the worktree" in capsys.readouterr().err
    assert path.is_dir()


def test_interrupted_evidence_move_keeps_every_copy(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _add(repo, "ev")
    (path / ".booley" / "handoffs").mkdir(parents=True)
    (path / ".booley" / "handoffs" / "h.md").write_bytes(b"handoff\n")
    (path / ".booley" / "findings.jsonl").write_bytes(b'{"finding": 1}\n')
    real_move = _module()._move_entry
    moved: list[Path] = []

    def die_after_first(source: Path, destination: Path) -> None:
        if moved:
            raise KeyboardInterrupt
        moved.append(destination)
        real_move(source, destination)

    with monkeypatch.context() as patch:
        patch.setattr(_module(), "_move_entry", die_after_first)
        with pytest.raises(KeyboardInterrupt):
            _rm(repo, "ev")
    assert moved[0].name == "findings.jsonl"
    (path / ".booley" / "findings.jsonl").write_bytes(b'{"finding": 2}\n')
    (path / "base.txt").write_bytes(b"changed\n")

    assert _rm(repo, "ev") == 1
    assert ".evidence" in capsys.readouterr().err
    (path / "base.txt").write_bytes(b"base\n")

    assert _rm(repo, "ev") == 0

    first, second = _evidence_dirs(repo, "ev")
    assert (first / "findings.jsonl").read_bytes() == b'{"finding": 1}\n'
    assert (second / "findings.jsonl").read_bytes() == b'{"finding": 2}\n'
    assert (second / "handoffs" / "h.md").is_file()


def test_a_failed_evidence_move_removes_nothing(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _add(repo, "ev")
    _evidence_files(path / ".booley")

    def cross_device(_source: Path, _destination: Path) -> None:
        raise OSError("Invalid cross-device link")

    monkeypatch.setattr(_module(), "_move_entry", cross_device)

    assert _rm(repo, "ev") == 1

    assert "nothing was removed" in capsys.readouterr().err
    assert path.is_dir() and str(path) in _registered(repo)


def test_an_unreadable_removal_record_proves_nothing(repo: Path) -> None:
    """Garbage in the record never skips a check; a forced removal still deletes it."""
    path = _add(repo, "w")
    record = repo / ".booley" / "worktrees" / ".removing" / "w.json"
    record.parent.mkdir(parents=True)
    _commit(path, "unmerged.txt")
    for content in (b"{not json", b"[]", b'{"started": 5}'):
        record.write_bytes(content)

        assert _rm(repo, "w") == 1, content

    assert _rm(repo, "w", "--force") == 0
    assert not record.exists()


def test_a_leftover_record_of_a_removed_worktree_is_cleaned_up(repo: Path) -> None:
    record = repo / ".booley" / "worktrees" / ".removing" / "done.json"
    record.parent.mkdir(parents=True)
    record.write_bytes(b'{"schema": 1, "started": ["paired", "outer"]}\n')

    assert _rm(repo, "done") == 0
    assert not record.exists()


def test_partly_tracked_evidence_directories_keep_their_untracked_files(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A committed plan beside an untracked one: the untracked plan is still evidence."""
    for name, paired in (("snap", False), ("pair", True)):
        if paired:
            booley_dir = _versioned(repo)
            (booley_dir / "plans").mkdir()
            _commit(booley_dir, "plans/a.md")
        path = _add(repo, name, paired=paired)
        if not paired:
            (path / ".booley" / "plans").mkdir(parents=True)
            _commit(path, ".booley/plans/a.md")
        (path / ".booley" / "plans" / "b.md").write_bytes(b"precious\n")

        assert _rm(repo, name, "--force") == 0, name

        (kept,) = _evidence_dirs(repo, name)
        assert [
            item.relative_to(kept).as_posix() for item in kept.rglob("*") if item.is_file()
        ] == ["plans/b.md"]
    capsys.readouterr()


def test_rm_refuses_untracked_work_in_a_snapshots_booley_directory(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An untracked core is neither counted by `list` nor evidence; a copy is harmless."""
    path = _add(repo, "snap")
    (repo / ".booley" / "booley.toml").write_bytes(b"# main\n")
    (path / ".booley" / "cores").mkdir(parents=True)
    (path / ".booley" / "cores" / "new.core").write_bytes(b"work\n")
    (path / ".booley" / "booley.toml").write_bytes(b"# main\n")

    assert _rm(repo, "snap") == 1

    assert "1 untracked file(s) in its Booley Directory" in capsys.readouterr().err
    (path / ".booley" / "cores" / "new.core").unlink()
    assert _rm(repo, "snap") == 0


def test_rm_refuses_a_booley_directory_that_links_to_the_main_one(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Moving a linked Booley Directory would carry off the whole Project's state."""
    (repo / ".booley" / "handoffs").mkdir(parents=True)
    (repo / ".booley" / "handoffs" / "h.md").write_bytes(b"main\n")
    path = _add(repo, "linked")
    try:
        (path / ".booley").symlink_to(repo / ".booley", target_is_directory=True)
    except OSError:
        pytest.skip("symlinks need privileges on this host")

    assert _rm(repo, "linked", "--force") == 1

    assert "is a link or lies outside it" in capsys.readouterr().err
    assert (repo / ".booley" / "handoffs" / "h.md").is_file() and path.is_dir()


def test_rm_sees_git_configuration_hiding_changes(repo: Path) -> None:
    """`status.showUntrackedFiles=no` must not hide an untracked file from the check."""
    path = _add(repo, "quiet")
    git(repo, "config", "status.showUntrackedFiles", "no")
    (path / "notes.txt").write_bytes(b"work\n")

    assert _rm(repo, "quiet") == 1


def test_a_stale_record_never_excuses_an_intact_worktree(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A record of an earlier removal does not hide work of a worktree that reuses the name."""
    path = _add(repo, "reused")
    record = repo / ".booley" / "worktrees" / ".removing" / "reused.json"
    record.parent.mkdir(parents=True)
    record.write_bytes(b'{"schema": 1, "started": ["paired", "outer"]}\n')
    (path / ".booley" / "handoffs").mkdir(parents=True)
    (path / ".booley" / "handoffs" / "h.md").write_bytes(b"handoff\n")
    _commit(path, "unmerged.txt")

    assert _rm(repo, "reused") == 1
    assert "1 commit(s) on booley/reused not in" in capsys.readouterr().err

    assert _rm(repo, "reused", "--force") == 0
    (kept,) = _evidence_dirs(repo, "reused")
    assert (kept / "handoffs" / "h.md").is_file()


def test_a_lock_timeout_before_the_paired_removal_changes_nothing_for_good(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The record is written before the lock wait; handoffs added after still move."""
    from booley.core.file_lock import LockTimeoutError

    _versioned(repo)
    path = _add(repo, "locked", paired=True)

    def timeout(_source: Path) -> None:
        raise LockTimeoutError("busy")

    with monkeypatch.context() as patch:
        patch.setattr("booley.runtime.project_worktree_pairing._project_lock", timeout)
        assert _rm(repo, "locked") == 1
    assert (path / ".booley" / ".git").is_file()
    (path / ".booley" / "handoffs").mkdir()
    (path / ".booley" / "handoffs" / "h.md").write_bytes(b"added since\n")

    assert _rm(repo, "locked") == 0
    (kept,) = _evidence_dirs(repo, "locked")
    assert (kept / "handoffs" / "h.md").is_file()


def test_an_interrupted_paired_removal_completes_on_rerun(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _module()
    from booley.runtime import project_worktree_pairing as pairing

    real_removal = pairing.remove_worktree_registration
    _versioned(repo)
    path = _add(repo, "half", paired=True)

    def die_inside_paired(repository: Path, worktree: Path) -> None:
        if worktree == path / ".booley":
            (worktree / "booley.toml").unlink()
            raise KeyboardInterrupt
        real_removal(repository, worktree)

    with monkeypatch.context() as patch:
        patch.setattr(pairing, "remove_worktree_registration", die_inside_paired)
        with pytest.raises(KeyboardInterrupt):
            _rm(repo, "half")

    assert _rm(repo, "half") == 0
    assert not path.exists() and str(path / ".booley") not in _registered(repo / ".booley")


def test_a_snapshot_made_before_the_booley_directory_was_versioned_needs_no_force(
    repo: Path,
) -> None:
    path = _add(repo, "old")
    (path / ".booley").mkdir()
    (path / ".booley" / "booley.toml").write_bytes(b"# old\n")
    _versioned(repo)
    (repo / ".booley" / "booley.toml").write_bytes(b"# old\n")

    assert _rm(repo, "old") == 0
    assert not path.exists()


def test_rm_reports_a_value_error_instead_of_a_traceback(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _add(repo, "bad")

    def refuse(_repository: Path, _worktree: Path) -> None:
        raise ValueError("refusing to remove git root")

    monkeypatch.setattr(_module(), "remove_worktree_registration", refuse)

    assert _rm(repo, "bad") == 1
    assert "ERROR: refusing to remove git root" in capsys.readouterr().err


def test_earlier_evidence_of_a_similarly_named_worktree_is_not_listed(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _add(repo, "a")
    record = repo / ".booley" / "worktrees" / ".removing" / "a.json"
    record.parent.mkdir(parents=True)
    record.write_bytes(b'{"schema": 1, "started": []}\n')
    other = repo / ".booley" / "worktrees" / ".evidence" / "a-b-20261107T203255123456Z"
    other.mkdir(parents=True)
    _commit(path, "unmerged.txt")

    assert _rm(repo, "a") == 1

    assert "An earlier attempt" not in capsys.readouterr().err

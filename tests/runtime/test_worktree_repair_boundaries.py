"""Owner proof failures leave Ticket and retained acceptance state untouched."""

import stat
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from booley.core.project_dir import reset_cache
from booley.runtime import worktree_repair
from booley.runtime.worktree_repair import (
    WorktreeRepairError,
    repair_acceptance_worktrees,
    repair_ticket_workspace,
)


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    ).stdout.strip()


def _rewrite_gitfile(path: Path, content: str | bytes) -> None:
    """Keep hidden Git-for-Windows markers intact while corrupting fixture bytes."""
    path.chmod(path.stat().st_mode | stat.S_IWRITE)
    data = content.encode("utf-8") if isinstance(content, str) else content
    with path.open("r+b") as stream:
        stream.write(data)
        stream.truncate()


@pytest.fixture
def owner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    root = tmp_path / "root"
    root.mkdir()
    state = root / ".booley_project"
    state.mkdir()
    monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(state))
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    reset_cache()
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "Fixture")
    _git(root, "config", "user.email", "fixture@example.test")
    (root / "source").write_text("original\n")
    _git(root, "add", "source")
    _git(root, "commit", "-qm", "initial")
    yield root
    reset_cache()


def _checkout(root: Path, *, detached: bool = False) -> tuple[Path, Path]:
    namespace = ".runtime/acceptance-worktrees" if detached else "worktrees"
    checkout = root / ".booley_project" / namespace / "demo"
    mode = ("--detach",) if detached else ("-b", "ticket/demo")
    _git(root, "worktree", "add", *mode, str(checkout), "HEAD")
    gitfile = checkout / ".git"
    gitfile.chmod(gitfile.stat().st_mode | stat.S_IWRITE)
    admin = Path(gitfile.read_text().removeprefix("gitdir: ").strip())
    return checkout, admin


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("short-ref", "full Ticket ref"),
        ("missing-gitfile", "unreadable worktree metadata"),
        ("invalid-encoding", "unreadable worktree metadata"),
        ("foreign-common", "foreign common Git directory"),
        ("ambiguous-ref", "ambiguous owner registration"),
    ],
)
def test_ticket_proof_refuses_invalid_metadata_without_repair(
    owner: Path, mutation: str, message: str
) -> None:
    checkout, admin = _checkout(owner)
    ref = "refs/heads/ticket/demo"
    if mutation == "short-ref":
        ref = "ticket/demo"
    elif mutation == "missing-gitfile":
        (checkout / ".git").unlink()
    elif mutation == "invalid-encoding":
        _rewrite_gitfile(checkout / ".git", b"\xff")
    elif mutation == "foreign-common":
        (admin / "commondir").write_text("../../../foreign\n")
    else:
        duplicate = admin.parent / "duplicate"
        duplicate.mkdir()
        (duplicate / "HEAD").write_text(f"ref: {ref}\n")
    before = (admin / "gitdir").read_bytes()
    with pytest.raises(WorktreeRepairError, match=message):
        repair_ticket_workspace(owner, checkout, ref)
    assert (admin / "gitdir").read_bytes() == before
    assert (checkout / "source").read_text() == "original\n"


def test_unrelated_incomplete_registrations_do_not_block_ticket_reuse(owner: Path) -> None:
    checkout, admin = _checkout(owner)
    (admin.parent / "not-a-registration").write_text("unrelated\n")
    (admin.parent / "incomplete").mkdir()
    before = (checkout / ".git").read_bytes()
    repair_ticket_workspace(owner, checkout, "refs/heads/ticket/demo")
    assert (checkout / ".git").read_bytes() == before
    assert _git(owner, "config", "--get", "gc.worktreePruneExpire") == "never"


@pytest.mark.parametrize(
    "failure", [FileNotFoundError("Git unavailable"), subprocess.TimeoutExpired("git", 30)]
)
def test_ticket_repair_reports_git_transport_failure(
    owner: Path, monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    checkout, admin = _checkout(owner)
    before = (admin / "gitdir").read_bytes()

    def failed_transport(*args, **kwargs):
        raise failure

    monkeypatch.setattr(worktree_repair.subprocess, "run", failed_transport)
    with pytest.raises(WorktreeRepairError, match="worktree repair Git failed"):
        repair_ticket_workspace(owner, checkout, "refs/heads/ticket/demo")
    assert (admin / "gitdir").read_bytes() == before


def test_ticket_repair_reports_git_command_failure(owner: Path) -> None:
    checkout, admin = _checkout(owner)
    before = (checkout / ".git").read_bytes()
    (owner / ".git" / "HEAD").unlink()
    with pytest.raises(WorktreeRepairError, match="worktree repair Git failed"):
        repair_ticket_workspace(owner, checkout, "refs/heads/ticket/demo")
    assert (checkout / ".git").read_bytes() == before
    assert (admin / "HEAD").read_text().strip() == "ref: refs/heads/ticket/demo"


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        ("outside-journal", "outside its journal root"),
        ("malformed", "malformed retained candidate gitfile"),
        ("missing-head", "unreadable worktree metadata"),
        ("unrelated-commit", "worktree repair Git failed"),
    ],
)
def test_retained_acceptance_refuses_unproven_candidates(
    owner: Path, failure: str, message: str
) -> None:
    checkout, admin = _checkout(owner, detached=True)
    prepared = _git(owner, "rev-parse", "HEAD")
    if failure == "outside-journal":
        checkout = owner / "source"
    elif failure == "malformed":
        _rewrite_gitfile(checkout / ".git", "not a gitfile\n")
    elif failure == "missing-head":
        (admin / "HEAD").unlink()
    else:
        tree = _git(owner, "rev-parse", "HEAD^{tree}")
        prepared = _git(owner, "commit-tree", tree, "-m", "unrelated root")
    before = (admin / "gitdir").read_bytes()
    with pytest.raises(WorktreeRepairError, match=message):
        repair_acceptance_worktrees(owner, ((owner, checkout, prepared),))
    assert (admin / "gitdir").read_bytes() == before


def test_retained_detached_descendant_is_reused_without_losing_dirty_state(owner: Path) -> None:
    checkout, admin = _checkout(owner, detached=True)
    prepared = _git(checkout, "rev-parse", "HEAD")
    (checkout / "source").write_text("accepted change\n")
    _git(checkout, "add", "source")
    _git(checkout, "commit", "-qm", "candidate descendant")
    candidate = _git(checkout, "rev-parse", "HEAD")
    (checkout / "source").write_text("retained dirty state\n")
    before = (admin / "HEAD").read_bytes()
    repair_acceptance_worktrees(owner, ((owner, checkout, prepared),))
    assert (admin / "HEAD").read_bytes() == before
    assert _git(checkout, "rev-parse", "HEAD") == candidate
    assert (checkout / "source").read_text() == "retained dirty state\n"
    assert _git(owner, "config", "--get", "gc.worktreePruneExpire") == "never"


def test_ticket_repair_refuses_checkout_outside_selected_namespace(owner: Path) -> None:
    before = (owner / ".git" / "config").read_bytes()
    with pytest.raises(WorktreeRepairError, match="outside its selected worktree root"):
        repair_ticket_workspace(owner, owner, "refs/heads/ticket/demo")
    assert (owner / ".git" / "config").read_bytes() == before


def test_retained_acceptance_refuses_candidate_switched_to_branch(owner: Path) -> None:
    checkout, admin = _checkout(owner, detached=True)
    prepared = _git(checkout, "rev-parse", "HEAD")
    _git(checkout, "checkout", "-b", "unexpected-branch")
    before = (admin / "HEAD").read_bytes()
    with pytest.raises(WorktreeRepairError, match="no longer detached"):
        repair_acceptance_worktrees(owner, ((owner, checkout, prepared),))
    assert (admin / "HEAD").read_bytes() == before
    assert _git(checkout, "symbolic-ref", "HEAD") == "refs/heads/unexpected-branch"


def test_stale_ticket_metadata_requires_safe_session_before_repair(
    owner: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.runtime import session_runtime

    checkout, admin = _checkout(owner)
    _rewrite_gitfile(checkout / ".git", f"gitdir: /work/.git/worktrees/{admin.name}\n")
    before = (checkout / ".git").read_bytes()

    def unsafe_session(_root):
        raise session_runtime.SessionError("active Sandbox must stop before repair")

    monkeypatch.setattr(session_runtime, "assert_worktree_repair_safe", unsafe_session)
    with pytest.raises(WorktreeRepairError, match="active Sandbox must stop"):
        repair_ticket_workspace(owner, checkout, "refs/heads/ticket/demo")
    assert (checkout / ".git").read_bytes() == before


def test_retained_acceptance_in_external_state_preserves_owner_identity(
    owner: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "external-state"
    state.mkdir()
    monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(state))
    reset_cache()
    checkout = state / ".runtime/acceptance-worktrees/demo"
    prepared = _git(owner, "rev-parse", "HEAD")
    _git(owner, "worktree", "add", "--detach", str(checkout), prepared)
    before = (checkout / ".git").read_bytes()
    repair_acceptance_worktrees(owner, ((owner, checkout, prepared),))
    assert (checkout / ".git").read_bytes() == before
    assert (
        Path(_git(checkout, "rev-parse", "--git-common-dir")).resolve()
        == (owner / ".git").resolve()
    )


def test_ticket_owner_can_itself_be_a_linked_checkout(
    owner: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    linked_owner = tmp_path / "linked-owner"
    _git(owner, "worktree", "add", "-b", "owner-branch", str(linked_owner))
    state = linked_owner / ".booley_project"
    state.mkdir()
    monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(state))
    reset_cache()
    checkout, _admin = _checkout(linked_owner)
    before = (checkout / ".git").read_bytes()
    repair_ticket_workspace(linked_owner, checkout, "refs/heads/ticket/demo")
    assert (checkout / ".git").read_bytes() == before
    assert (
        Path(_git(checkout, "rev-parse", "--git-common-dir")).resolve()
        == (owner / ".git").resolve()
    )

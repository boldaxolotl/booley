"""Worktree paths follow the selected data directory's filesystem identity."""

import stat
from pathlib import Path

import pytest

from booley.core.project_dir import reset_cache


def _rewrite_gitfile(path: Path, content: str | bytes) -> None:
    """Keep hidden Git-for-Windows markers intact while corrupting fixture bytes."""
    path.chmod(path.stat().st_mode | stat.S_IWRITE)
    data = content.encode("utf-8") if isinstance(content, str) else content
    with path.open("r+b") as stream:
        stream.write(data)
        stream.truncate()


@pytest.fixture(autouse=True)
def isolated_project_selection(monkeypatch):
    from booley.runtime import session_runtime

    monkeypatch.setattr(session_runtime, "assert_worktree_repair_safe", lambda _root: None)
    reset_cache()
    yield
    reset_cache()


@pytest.mark.parametrize("external", [False, True])
def test_selected_worktree_state_identity(tmp_path: Path, monkeypatch, external: bool):
    from booley.runtime.worktree_paths import ticket_workspace_path

    root = tmp_path / "project"
    root.mkdir()
    local = root / ".booley_project"
    local.mkdir()
    selected = tmp_path / "external" if external else local
    selected.mkdir(exist_ok=True)
    monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(selected))
    reset_cache()
    assert ticket_workspace_path(root, "demo") == selected / "worktrees" / "demo"


def test_configured_local_directory_retains_selected_identity(tmp_path: Path, monkeypatch):
    from booley.runtime.worktree_paths import ticket_workspace_path

    root = tmp_path / "project"
    selected = root / "nested/state"
    selected.mkdir(parents=True)
    (root / "booley.toml").write_text('[project]\ndir = "nested/state"\n')
    alias = tmp_path / "alias"
    alias.symlink_to(selected, target_is_directory=True)
    monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(alias))
    reset_cache()
    assert ticket_workspace_path(root, "demo") == selected / "worktrees/demo"


def _git(root: Path, *args: str) -> str:
    import subprocess

    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=True, timeout=30
    ).stdout.strip()


@pytest.fixture
def linked_ticket(tmp_path: Path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    state = root / ".booley_project"
    state.mkdir()
    monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(state))
    reset_cache()
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "Fixture")
    _git(root, "config", "user.email", "fixture@example.test")
    (root / "source").write_text("original\n")
    _git(root, "add", "source")
    _git(root, "commit", "-qm", "initial")
    checkout = state / "worktrees/demo"
    _git(root, "worktree", "add", "-b", "ticket/demo", str(checkout))
    # Git marks linked-worktree gitfiles read-only on Windows. These fixtures
    # deliberately corrupt metadata before exercising repair.
    gitfile = checkout / ".git"
    gitfile.chmod(gitfile.stat().st_mode | stat.S_IWRITE)
    return root, checkout


def test_owner_proven_repair_preserves_dirty_index_and_replays(linked_ticket):
    from booley.runtime.worktree_repair import repair_ticket_workspace

    root, checkout = linked_ticket
    head = _git(checkout, "rev-parse", "HEAD")
    (checkout / "source").write_text("staged\n")
    _git(checkout, "add", "source")
    (checkout / "source").write_text("dirty\n")
    index = _git(checkout, "write-tree")
    pointer = (checkout / ".git").read_text().removeprefix("gitdir: ").strip()
    admin = Path(pointer)
    _rewrite_gitfile(checkout / ".git", f"gitdir: /work/.git/worktrees/{admin.name}\n")
    (admin / "gitdir").write_text("/booley-project/worktrees/demo/.git\n")
    repair_ticket_workspace(root, checkout, "refs/heads/ticket/demo")
    repair_ticket_workspace(root, checkout, "refs/heads/ticket/demo")
    assert _git(checkout, "rev-parse", "HEAD") == head
    assert _git(checkout, "write-tree") == index
    assert (checkout / "source").read_text() == "dirty\n"
    assert "work/" not in (checkout / ".git").read_text()
    assert "prunable" not in _git(root, "worktree", "list", "--porcelain")


def test_repair_refuses_foreign_or_unassociated_metadata(linked_ticket):
    from booley.runtime.worktree_repair import WorktreeRepairError, repair_ticket_workspace

    root, checkout = linked_ticket
    pointer = (checkout / ".git").read_text()
    with pytest.raises(WorktreeRepairError, match="worktree owner ref does not match"):
        repair_ticket_workspace(root, checkout, "refs/heads/foreign")
    assert (checkout / ".git").read_text() == pointer


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("malformed", "malformed worktree gitfile"),
        ("foreign-pointer", "foreign or unassociated worktree gitfile"),
        ("foreign-backlink", "foreign or unassociated reverse worktree link"),
        ("lost", "lost worktree registration"),
    ],
)
def test_repair_refuses_unproven_registration_without_mutation(
    linked_ticket: tuple[Path, Path], mutation: str, message: str
) -> None:
    from booley.runtime.worktree_repair import WorktreeRepairError, repair_ticket_workspace

    root, checkout = linked_ticket
    admin = Path((checkout / ".git").read_text().removeprefix("gitdir: ").strip())
    if mutation == "malformed":
        _rewrite_gitfile(checkout / ".git", "arbitrary metadata\n")
    elif mutation == "foreign-pointer":
        _rewrite_gitfile(checkout / ".git", f"gitdir: /foreign/.git/worktrees/{admin.name}\n")
    elif mutation == "foreign-backlink":
        (admin / "gitdir").write_text("/foreign/.git\n")
    else:
        _rewrite_gitfile(checkout / ".git", "gitdir: /work/.git/worktrees/lost-registration\n")
    before = (checkout / ".git").read_bytes()
    with pytest.raises(WorktreeRepairError, match=message):
        repair_ticket_workspace(root, checkout, "refs/heads/ticket/demo")
    assert (checkout / ".git").read_bytes() == before
    assert (checkout / "source").read_text() == "original\n"


def test_old_git_repair_omits_unsupported_mode_flags(monkeypatch):
    from booley.runtime import worktree_paths, worktree_repair

    monkeypatch.setattr(worktree_paths, "_git_supports_relative_paths", lambda: False)
    assert worktree_repair._metadata_flag(True) == ()
    assert worktree_repair._metadata_flag(False) == ()
    monkeypatch.setattr(worktree_paths, "_git_supports_relative_paths", lambda: True)
    assert worktree_repair._metadata_flag(True) == ("--relative-paths",)
    assert worktree_repair._metadata_flag(False) == ("--no-relative-paths",)


def test_paired_repair_preflights_both_owners_before_mutating_outer(linked_ticket):
    from booley.runtime.worktree_repair import WorktreeRepairError, repair_ticket_workspace

    root, checkout = linked_ticket
    project = root / ".booley_project"
    _git(project, "init", "-q")
    _git(project, "config", "user.name", "Fixture")
    _git(project, "config", "user.email", "fixture@example.test")
    (project / "project-source").write_text("project source\n")
    _git(project, "add", "project-source")
    _git(project, "commit", "-qm", "project initial")
    paired = checkout / ".booley_project"
    _git(project, "worktree", "add", "-b", "ticket/project", str(paired))
    gitfile = paired / ".git"
    gitfile.chmod(gitfile.stat().st_mode | stat.S_IWRITE)
    original_outer = (checkout / ".git").read_bytes()
    original_paired = (paired / ".git").read_bytes()
    admin_name = Path(original_paired.decode().removeprefix("gitdir: ").strip()).name
    _rewrite_gitfile(paired / ".git", f"gitdir: /foreign/.git/worktrees/{admin_name}\n")
    with pytest.raises(WorktreeRepairError, match="foreign or unassociated worktree gitfile"):
        repair_ticket_workspace(
            root, checkout, "refs/heads/ticket/demo", "refs/heads/ticket/project"
        )
    assert (checkout / ".git").read_bytes() == original_outer
    _rewrite_gitfile(paired / ".git", original_paired)
    repair_ticket_workspace(root, checkout, "refs/heads/ticket/demo", "refs/heads/ticket/project")
    assert _git(paired, "symbolic-ref", "HEAD") == "refs/heads/ticket/project"
    assert _git(paired, "config", "gc.worktreePruneExpire") == "never"
    assert _git(checkout, "config", "gc.worktreePruneExpire") == "never"


def test_unsupported_git_and_linked_root_use_explicit_creation_fallback(tmp_path, monkeypatch):
    from booley.runtime import worktree_paths

    root = tmp_path / "project"
    state = root / ".booley_project"
    state.mkdir(parents=True)
    monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(state))
    monkeypatch.setattr(worktree_paths, "_git_supports_relative_paths", lambda: False)
    assert worktree_paths.worktree_creation_config(root) == (
        "-c",
        "worktree.useRelativePaths=false",
    )
    monkeypatch.setattr(worktree_paths, "_git_supports_relative_paths", lambda: True)
    assert worktree_paths.worktree_creation_config(root) == ()
    (root / ".git").write_text("gitdir: /linked/owner\n")
    assert worktree_paths.worktree_creation_config(root) == (
        "-c",
        "worktree.useRelativePaths=false",
    )


def test_host_repairs_real_cross_mount_relative_backlink(linked_ticket):
    from booley.runtime.worktree_repair import repair_ticket_workspace

    root, checkout = linked_ticket
    admin = Path((checkout / ".git").read_text().removeprefix("gitdir: ").strip())
    _rewrite_gitfile(checkout / ".git", f"gitdir: ../../../work/.git/worktrees/{admin.name}\n")
    (admin / "gitdir").write_text(f"../../../../booley-project/worktrees/{checkout.name}/.git\n")
    repair_ticket_workspace(root, checkout, "refs/heads/ticket/demo")
    assert _git(checkout, "symbolic-ref", "HEAD") == "refs/heads/ticket/demo"
    assert "prunable" not in _git(root, "worktree", "list", "--porcelain")


def test_unrelated_incomplete_registration_does_not_block_owner_repair(linked_ticket):
    from booley.runtime.worktree_repair import repair_ticket_workspace

    root, checkout = linked_ticket
    (root / ".git/worktrees/incomplete").mkdir()
    repair_ticket_workspace(root, checkout, "refs/heads/ticket/demo")
    assert _git(checkout, "symbolic-ref", "HEAD") == "refs/heads/ticket/demo"


def test_host_repair_defers_before_any_metadata_change_with_active_legacy_sandbox(
    linked_ticket, monkeypatch
):
    from booley.runtime import session_runtime
    from booley.runtime.worktree_repair import WorktreeRepairError, repair_ticket_workspace

    root, checkout = linked_ticket
    admin = Path((checkout / ".git").read_text().removeprefix("gitdir: ").strip())
    _rewrite_gitfile(checkout / ".git", f"gitdir: /work/.git/worktrees/{admin.name}\n")
    (admin / "gitdir").write_text("/booley-project/worktrees/demo/.git\n")
    before = (checkout / ".git").read_bytes()

    def unsafe(_root):
        raise session_runtime.SessionError("running Sandbox uses incompatible layout")

    monkeypatch.setattr(session_runtime, "assert_worktree_repair_safe", unsafe)
    with pytest.raises(WorktreeRepairError, match="incompatible layout"):
        repair_ticket_workspace(root, checkout, "refs/heads/ticket/demo")
    assert (checkout / ".git").read_bytes() == before


def test_recorded_paired_metadata_missing_blocks_outer_repair(linked_ticket):
    from booley.runtime.worktree_repair import WorktreeRepairError, repair_ticket_workspace

    root, checkout = linked_ticket
    original = (checkout / ".git").read_bytes()
    with pytest.raises(WorktreeRepairError, match="paired Ticket worktree metadata is missing"):
        repair_ticket_workspace(
            root, checkout, "refs/heads/ticket/demo", "refs/heads/ticket/project"
        )
    assert (checkout / ".git").read_bytes() == original


def test_draft_ticket_does_not_load_executable_basis_during_init(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from booley.harness.setup.git_hooks import _repair_recorded_ticket
    from booley.ticket_board import scanner

    def unexpected(*_args):
        raise AssertionError("drafts have no executable basis")

    tio = SimpleNamespace(tickets_dir=tmp_path, inspect_ticket=unexpected)
    monkeypatch.setattr(
        scanner, "find_ticket_file", lambda *_args, **_kwargs: (tmp_path / "draft.md", "draft")
    )
    assert _repair_recorded_ticket(tmp_path, tio, "draft", tmp_path, []) == set()


def test_pending_amendment_reports_one_deferred_reason_not_false_missing_owner(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace

    from booley.harness.setup import git_hooks
    from booley.runtime import worktree_paths
    from booley.ticket_board import io

    data = tmp_path / ".booley_project"
    checkout = data / "worktrees/demo"
    checkout.mkdir(parents=True)
    (checkout / ".git").write_text("gitdir: known\n")
    tickets = data / "tickets"
    tickets.mkdir()
    (tickets / "demo.md").write_text("pending amendment\n")
    monkeypatch.setattr(worktree_paths, "relative_worktree_paths", lambda _root: True)
    monkeypatch.setattr(
        io, "TicketIO", lambda *_args, **_kwargs: SimpleNamespace(tickets_dir=tickets)
    )

    def pending(*_args):
        raise ValueError("amendment publication is pending; execution is not ready")

    monkeypatch.setattr(git_hooks, "_repair_recorded_ticket", pending)
    result = git_hooks._repair_live_ticket_worktrees(SimpleNamespace(project_root=tmp_path))
    assert result == ["demo: amendment publication is pending; execution is not ready"]


@pytest.mark.parametrize("slug", ["", ".", "..", "a/b"])
def test_ticket_workspace_rejects_invalid_names(tmp_path: Path, slug: str) -> None:
    from booley.runtime.worktree_paths import ticket_workspace_path

    with pytest.raises(ValueError, match="invalid Ticket Workspace name"):
        ticket_workspace_path(tmp_path, slug)


def test_external_state_never_enables_relative_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.runtime import worktree_paths

    root = tmp_path / "project"
    root.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(external))
    monkeypatch.setattr(worktree_paths, "_git_supports_relative_paths", lambda: True)
    reset_cache()
    assert not worktree_paths.relative_worktree_paths(root)
    assert worktree_paths.worktree_creation_config(root) == (
        "-c",
        "worktree.useRelativePaths=false",
    )


def test_git_version_transport_failure_keeps_absolute_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from booley.runtime import worktree_paths

    def unavailable(*_args, **_kwargs) -> None:
        raise OSError("Git unavailable")

    monkeypatch.setattr(worktree_paths.subprocess, "run", unavailable)
    assert not worktree_paths._git_supports_relative_paths()

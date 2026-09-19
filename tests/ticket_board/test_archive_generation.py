"""Archive must release only the Ticket's recorded generation resources."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.core.project_dir import reset_cache
from booley.ticket_board import archive as archive_module
from booley.ticket_board import archive_generation
from booley.ticket_board.archive import op_archive
from booley.ticket_board.cli_handlers import _cmd_archive
from booley.ticket_board.frontmatter import parse_frontmatter
from booley.ticket_board.io import TicketFileSpec, TicketIO
from booley.ticket_board.ticket_baseline import ticket_baseline_from_machine
from booley.ticket_board.workspace_ops import load_draft_generation
from tests.ticket_board.test_ticket_baseline import (
    _basis_project,
    _create_v2_ticket,
    _paired_basis_project,
)


def _git(repository: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repository,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return result.stdout.strip()


def _repository(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.email", "archive@example.test")
    _git(path, "config", "user.name", "Archive Test")
    (path / "committed.txt").write_text("baseline\n", encoding="utf-8")
    _git(path, "add", "committed.txt")
    _git(path, "commit", "-m", "baseline")


def _draft(
    tmp_path: Path, monkeypatch, *, paired: bool
) -> tuple[TicketIO, Path, Path | None, str]:
    root = tmp_path / "outer"
    _repository(root)
    data = root / ".booley_project"
    data.mkdir()
    if paired:
        _repository(data)
    monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(data))
    reset_cache()
    tickets = data / "tickets"
    ticket = tickets / "board" / "drafts" / "ticket.md"
    ticket.parent.mkdir(parents=True)
    ticket.write_text(
        "---\nsummary: Ticket\ntype: feature\nbranch: main\nscope: []\n"
        "on_success: [review]\n"
        "CRITERIA_MANDATORY: {REVIEW: {rtl: {bugs: done}}}\n"
        "---\n## Description\nCheck the Ticket.\n",
        encoding="utf-8",
    )
    token = "0123456789abcdef"
    descriptor = data / ".runtime" / "acceptance" / "drafts" / "ticket.json"
    descriptor.parent.mkdir(parents=True)
    descriptor.write_text(json.dumps({"generation": token}), encoding="utf-8")
    ref = f"booley-generation/{token}/ticket"
    outer = data / "worktrees" / "ticket"
    outer.parent.mkdir(parents=True)
    _git(root, "worktree", "add", "-b", ref, str(outer), "main")
    if paired:
        project_worktree = outer / ".booley_project"
        _git(data, "worktree", "add", "-b", ref, str(project_worktree), "main")
    return TicketIO(tickets, project_root=root), root, data if paired else None, ref


def test_archive_draft_releases_paired_generation(tmp_path: Path, monkeypatch) -> None:
    tio, outer, project, branch = _draft(tmp_path, monkeypatch, paired=True)
    assert project is not None
    unrelated = "booley-generation/ffffffffffffffff/ticket"
    unrelated_path = outer.parent / "unrelated"
    _git(outer, "worktree", "add", "-b", unrelated, str(unrelated_path), "main")
    _git(project, "branch", unrelated, "main")
    before = (
        _git(outer, "worktree", "list", "--porcelain"),
        _git(project, "worktree", "list", "--porcelain"),
    )
    assert branch in before[0] and branch in before[1]

    outcome = op_archive(tio, slug="ticket", force=True)

    assert outcome.failures == {}
    assert outcome.archived == ["Ticket"]
    assert _git(outer, "branch", "--list", branch) == ""
    assert _git(project, "branch", "--list", branch) == ""
    assert branch not in _git(outer, "worktree", "list", "--porcelain")
    assert branch not in _git(project, "worktree", "list", "--porcelain")
    assert _git(outer, "branch", "--list", unrelated)
    assert _git(project, "branch", "--list", unrelated)
    assert unrelated_path.is_dir()
    assert not (tio.tickets_dir / "board" / "drafts" / "ticket.md").exists()
    assert not (project / ".runtime" / "acceptance" / "drafts" / "ticket.json").exists()


def test_archive_cli_records_paired_refs_and_worktrees(tmp_path: Path, monkeypatch) -> None:
    tio, outer, project, branch = _draft(tmp_path, monkeypatch, paired=True)
    assert project is not None
    repositories = (outer, project)
    before_refs = [
        set(_git(repo, "for-each-ref", "--format=%(refname)", "refs/heads").splitlines())
        for repo in repositories
    ]
    before_worktrees = [_git(repo, "worktree", "list", "--porcelain") for repo in repositories]
    command = [
        sys.executable,
        "-m",
        "booley.ticket_board",
        "archive",
        "ticket",
        "--force",
    ]
    env = {
        **os.environ,
        "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src"),
        "TICKETS_DIR": str(tio.tickets_dir),
    }

    result = subprocess.run(
        command, cwd=outer, env=env, capture_output=True, text=True, timeout=60, check=False
    )

    after_refs = [
        set(_git(repo, "for-each-ref", "--format=%(refname)", "refs/heads").splitlines())
        for repo in repositories
    ]
    after_worktrees = [_git(repo, "worktree", "list", "--porcelain") for repo in repositories]
    recorded_ref = f"refs/heads/{branch}"
    assert result.returncode == 0, (command, result.stdout, result.stderr)
    assert "Archived 1 ticket(s)" in result.stdout
    for old_refs, new_refs, old_worktrees, new_worktrees in zip(
        before_refs, after_refs, before_worktrees, after_worktrees, strict=True
    ):
        assert recorded_ref in old_refs
        assert new_refs == old_refs - {recorded_ref}
        assert recorded_ref in old_worktrees
        assert recorded_ref not in new_worktrees


def test_archive_releases_unbound_paired_generation(tmp_path: Path, monkeypatch) -> None:
    tio, outer, project, branch = _draft(tmp_path, monkeypatch, paired=True)
    assert project is not None
    canonical = outer / ".booley_project" / "worktrees" / "ticket"
    _git(project, "worktree", "remove", "--force", str(canonical / ".booley_project"))
    _git(outer, "worktree", "remove", "--force", str(canonical))
    assert _git(outer, "branch", "--list", branch)
    assert _git(project, "branch", "--list", branch)

    outcome = op_archive(tio, slug="ticket", force=True)

    assert outcome.failures == {}
    assert outcome.archived == ["Ticket"]
    assert _git(outer, "branch", "--list", branch) == ""
    assert _git(project, "branch", "--list", branch) == ""


def test_archive_releases_recorded_legacy_resources(tmp_path: Path, monkeypatch) -> None:
    tio, outer, project, _branch = _draft(tmp_path, monkeypatch, paired=True)
    assert project is not None
    # find_ticket uses the slug as a feature_branch fallback; it does not own
    # an unrelated outer branch merely because the names happen to match.
    _git(outer, "branch", "ticket", "main")
    project_legacy = outer.parent / "project-legacy"
    _git(project, "worktree", "add", "-b", "booley-ticket/ticket", str(project_legacy), "main")

    outcome = op_archive(tio, slug="ticket", force=True)

    assert outcome.failures == {}
    assert _git(outer, "branch", "--list", "ticket")
    assert _git(project, "branch", "--list", "booley-ticket/ticket") == ""
    assert not project_legacy.exists()


def test_archive_refuses_unidentified_canonical_workspace(tmp_path: Path, monkeypatch) -> None:
    tio, outer, project, branch = _draft(tmp_path, monkeypatch, paired=False)
    assert project is None
    descriptor = outer / ".booley_project" / ".runtime" / "acceptance" / "drafts" / "ticket.json"
    descriptor.unlink()

    outcome = op_archive(tio, slug="ticket", force=True)

    assert "ticket" in outcome.failures
    assert (tio.tickets_dir / "board" / "drafts" / "ticket.md").exists()
    assert _git(outer, "branch", "--list", branch)


def test_archive_workspace_free_draft_without_descriptor(tmp_path: Path, monkeypatch) -> None:
    tio, outer, project, branch = _draft(tmp_path, monkeypatch, paired=False)
    assert project is None
    _git(outer, "worktree", "remove", "--force", str(outer / ".booley_project/worktrees/ticket"))
    _git(outer, "branch", "-D", branch)
    descriptor = outer / ".booley_project" / ".runtime" / "acceptance" / "drafts" / "ticket.json"
    descriptor.unlink()

    outcome = op_archive(tio, slug="ticket", force=True)

    assert outcome.failures == {}
    assert outcome.archived == ["Ticket"]


def test_archive_rejects_corrupt_draft_descriptor(tmp_path: Path, monkeypatch) -> None:
    tio, outer, project, branch = _draft(tmp_path, monkeypatch, paired=False)
    assert project is None
    descriptor = outer / ".booley_project" / ".runtime" / "acceptance" / "drafts" / "ticket.json"
    descriptor.write_text("bad descriptor", encoding="utf-8")

    outcome = op_archive(tio, slug="ticket", force=True)

    assert "descriptor" in outcome.failures["ticket"]
    assert (tio.tickets_dir / "board" / "drafts" / "ticket.md").exists()
    assert _git(outer, "branch", "--list", branch)


def test_archive_refuses_missing_paired_repository(tmp_path: Path, monkeypatch) -> None:
    tio, outer, project, branch = _draft(tmp_path, monkeypatch, paired=True)
    assert project is not None
    (project / ".git").rename(project / ".git-unavailable")

    outcome = op_archive(tio, slug="ticket", force=True)

    assert "source repository is unavailable" in outcome.failures["ticket"]
    assert (tio.tickets_dir / "board" / "drafts" / "ticket.md").exists()
    assert _git(outer, "branch", "--list", branch)


def test_archive_published_ticket_uses_machine_refs(tmp_path: Path, monkeypatch) -> None:
    reset_cache()
    monkeypatch.delenv("BOOLEY_PROJECT_DIR", raising=False)
    root, project, tio = _paired_basis_project(tmp_path)
    ticket = _create_v2_ticket(
        tio,
        "published",
        TicketFileSpec(
            summary="Published Ticket",
            ticket_type="feature",
            branch="main",
            scope=["README.md"],
            criteria={"mandatory": {"review_rtl_bugs": True}},
        ),
    )
    assert ticket is not None
    assert tio.enqueue_ticket("published")
    queued = project / "tickets" / "board" / "queue" / "published.md"
    fields, _ = parse_frontmatter(queued.read_text(encoding="utf-8"))
    basis = ticket_baseline_from_machine(fields["machine"])
    refs = {row.role: row.ticket_ref for row in basis.participants}
    old_token = load_draft_generation(root, "published")
    assert fields["machine"]["generation"] != old_token

    outcome = op_archive(tio, slug="published", force=True)

    assert outcome.archived == ["Published Ticket"]
    assert outcome.failures == {}
    assert not queued.exists()
    assert _git(root, "branch", "--list", refs["outer"].removeprefix("refs/heads/")) == ""
    assert _git(project, "branch", "--list", refs["project"].removeprefix("refs/heads/")) == ""
    assert refs["outer"] not in _git(root, "worktree", "list", "--porcelain")
    assert refs["project"] not in _git(project, "worktree", "list", "--porcelain")
    replacement = _create_v2_ticket(
        tio,
        "published",
        TicketFileSpec(
            summary="Replacement Ticket",
            ticket_type="feature",
            branch="main",
            scope=["README.md"],
            criteria={"mandatory": {"review_rtl_bugs": True}},
        ),
    )
    assert replacement is not None
    assert load_draft_generation(root, "published") != old_token


def test_archive_published_single_repository(tmp_path: Path, monkeypatch) -> None:
    reset_cache()
    monkeypatch.delenv("BOOLEY_PROJECT_DIR", raising=False)
    root, project, tio = _basis_project(tmp_path)
    ticket = _create_v2_ticket(
        tio,
        "single",
        TicketFileSpec(
            summary="Single Repository",
            ticket_type="feature",
            branch="main",
            scope=["README.md"],
            criteria={"mandatory": {"review_rtl_bugs": True}},
        ),
    )
    assert ticket is not None
    assert tio.enqueue_ticket("single")
    queued = project / "tickets" / "board" / "queue" / "single.md"
    fields, _ = parse_frontmatter(queued.read_text(encoding="utf-8"))
    ref = fields["machine"]["baseline"]["outer"]["ticket_ref"]

    outcome = op_archive(tio, slug="single", force=True)

    assert outcome.failures == {}
    assert outcome.archived == ["Single Repository"]
    assert _git(root, "branch", "--list", ref.removeprefix("refs/heads/")) == ""
    assert ref not in _git(root, "worktree", "list", "--porcelain")


@pytest.mark.parametrize("role", ["project", "outer"])
def test_archive_retries_after_worktree_failure(tmp_path: Path, monkeypatch, role: str) -> None:
    tio, outer, project, branch = _draft(tmp_path, monkeypatch, paired=True)
    assert project is not None
    original = archive_generation._require_git

    failed_repository = project if role == "project" else outer

    def fail_worktree_remove(repository: Path, *args: str) -> str:
        if repository == failed_repository and args[:2] == ("worktree", "remove"):
            raise archive_generation.ArchiveGenerationError(f"injected {role} worktree failure")
        return original(repository, *args)

    monkeypatch.setattr(archive_generation, "_require_git", fail_worktree_remove)
    failed = op_archive(tio, slug="ticket", force=True)
    assert f"injected {role} worktree failure" in failed.failures["ticket"]
    assert (tio.tickets_dir / "board" / "drafts" / "ticket.md").exists()
    assert _git(outer, "branch", "--list", branch)
    assert _git(project, "branch", "--list", branch)

    monkeypatch.setattr(archive_generation, "_require_git", original)
    retried = op_archive(tio, slug="ticket", force=True)
    assert retried.failures == {}
    assert retried.archived == ["Ticket"]


def test_archive_rejects_ref_movement_after_preflight(tmp_path: Path, monkeypatch) -> None:
    tio, outer, project, branch = _draft(tmp_path, monkeypatch, paired=False)
    assert project is None
    original = archive_generation._require_git
    changed = False

    def move_before_delete(repository: Path, *args: str) -> str:
        nonlocal changed
        if args[:2] == ("update-ref", "-d") and not changed:
            changed = True
            old = _git(outer, "rev-parse", branch)
            tree = _git(outer, "rev-parse", f"{old}^{{tree}}")
            new = _git(outer, "commit-tree", tree, "-p", old, "-m", "move ref")
            _git(outer, "update-ref", args[2], new)
        return original(repository, *args)

    monkeypatch.setattr(archive_generation, "_require_git", move_before_delete)
    failed = op_archive(tio, slug="ticket", force=True)
    assert "ticket" in failed.failures
    assert changed
    assert (tio.tickets_dir / "board" / "drafts" / "ticket.md").exists()
    assert _git(outer, "branch", "--list", branch)


def test_archive_retries_after_branch_deletion_failure(tmp_path: Path, monkeypatch) -> None:
    tio, outer, project, branch = _draft(tmp_path, monkeypatch, paired=True)
    assert project is not None
    original = archive_generation._require_git

    def fail_delete(repository: Path, *args: str) -> str:
        if repository == project and args[:2] == ("update-ref", "-d"):
            raise archive_generation.ArchiveGenerationError("injected branch deletion failure")
        return original(repository, *args)

    monkeypatch.setattr(archive_generation, "_require_git", fail_delete)
    failed = op_archive(tio, slug="ticket", force=True)
    assert "injected branch deletion failure" in failed.failures["ticket"]
    assert (tio.tickets_dir / "board" / "drafts" / "ticket.md").exists()
    assert _git(project, "branch", "--list", branch)

    monkeypatch.setattr(archive_generation, "_require_git", original)
    retried = op_archive(tio, slug="ticket", force=True)
    assert retried.failures == {}
    assert retried.archived == ["Ticket"]
    assert _git(outer, "branch", "--list", branch) == ""


def test_archive_resumes_log_cleanup_after_ticket_unlink(tmp_path: Path, monkeypatch) -> None:
    tio, outer, project, branch = _draft(tmp_path, monkeypatch, paired=False)
    assert project is None
    original = archive_module._cleanup_log_dir

    def fail_cleanup(_log_dir: Path, _keep_logs: bool) -> None:
        raise OSError("injected log cleanup failure")

    monkeypatch.setattr(archive_module, "_cleanup_log_dir", fail_cleanup)
    failed = op_archive(tio, slug="ticket", force=True)
    assert "injected log cleanup failure" in failed.failures["ticket"]
    assert not (tio.tickets_dir / "board" / "drafts" / "ticket.md").exists()
    assert _git(outer, "branch", "--list", branch) == ""

    monkeypatch.setattr(archive_module, "_cleanup_log_dir", original)
    retried = op_archive(tio, slug="ticket", force=True)
    assert retried.failures == {}
    assert retried.archived == ["Ticket"]


def test_done_sweep_resumes_after_ticket_unlink(tmp_path: Path, monkeypatch) -> None:
    reset_cache()
    monkeypatch.delenv("BOOLEY_PROJECT_DIR", raising=False)
    root, project, tio = _paired_basis_project(tmp_path)
    ticket = _create_v2_ticket(
        tio,
        "ticket",
        TicketFileSpec(summary="Ticket", ticket_type="feature", branch="main", scope=["README.md"]),
    )
    assert ticket is not None
    assert tio.enqueue_ticket("ticket")
    queued = project / "tickets" / "board" / "queue" / "ticket.md"
    done = project / "tickets" / "board" / "done"
    done.mkdir(parents=True)
    queued.rename(done / queued.name)
    original = archive_module._cleanup_log_dir

    def fail_cleanup(_log_dir: Path, _keep_logs: bool) -> None:
        raise OSError("injected log cleanup failure")

    monkeypatch.setattr(archive_module, "_cleanup_log_dir", fail_cleanup)
    failed = op_archive(tio)
    assert "injected log cleanup failure" in failed.failures["ticket"]
    assert not (done / "ticket.md").exists()

    monkeypatch.setattr(archive_module, "_cleanup_log_dir", original)
    retried = op_archive(tio)
    marker = root / ".booley_project" / ".runtime" / "acceptance" / "archive" / "ticket.json"
    assert retried.failures == {}
    assert retried.archived == ["Ticket"]
    assert json.loads(marker.read_text(encoding="utf-8"))["logs_cleaned"] is True


@pytest.mark.parametrize(
    "module_name,function_name,value",
    [
        ("enqueue_publication", "load_enqueue_journal", SimpleNamespace(state="prepared")),
        ("basis_publication", "load_basis_publication", SimpleNamespace()),
        ("basis_refresh", "load_basis_refresh", SimpleNamespace(state="building")),
        ("amendment", "pending_amendment", {"state": "prepared"}),
        ("draft_transition", "transition_pending", True),
        ("acceptance_journal", "acceptance_state", "prepared"),
    ],
)
def test_archive_refuses_pending_lifecycle_operation(
    tmp_path: Path, monkeypatch, module_name: str, function_name: str, value: object
) -> None:
    import importlib

    tio, outer, project, branch = _draft(tmp_path, monkeypatch, paired=True)
    assert project is not None
    module = importlib.import_module(f"booley.ticket_board.{module_name}")
    monkeypatch.setattr(module, function_name, lambda *_args: value)

    outcome = op_archive(tio, slug="ticket", force=True)

    assert "ticket" in outcome.failures
    assert (tio.tickets_dir / "board" / "drafts" / "ticket.md").exists()
    assert _git(outer, "branch", "--list", branch)
    assert _git(project, "branch", "--list", branch)


def test_completed_amendment_record_does_not_block_archive(tmp_path: Path, monkeypatch) -> None:
    from booley.ticket_board import amendment

    tio, _outer, _project, _branch = _draft(tmp_path, monkeypatch, paired=False)
    monkeypatch.setattr(amendment, "pending_amendment", lambda *_args: {"phase": "queued"})

    outcome = op_archive(tio, slug="ticket", force=True)

    assert outcome.failures == {}
    assert outcome.archived == ["Ticket"]


def test_archive_retries_transition_failure_once(tmp_path: Path, monkeypatch) -> None:
    tio, _outer, _project, _branch = _draft(tmp_path, monkeypatch, paired=False)
    original = tio._append_transition_unlocked

    def fail_transition(*_args: object) -> None:
        raise OSError("injected transition failure")

    monkeypatch.setattr(tio, "_append_transition_unlocked", fail_transition)
    failed = op_archive(tio, slug="ticket", force=True)
    assert "injected transition failure" in failed.failures["ticket"]
    assert (tio.tickets_dir / "board" / "drafts" / "ticket.md").exists()

    monkeypatch.setattr(tio, "_append_transition_unlocked", original)
    retried = op_archive(tio, slug="ticket", force=True)
    assert retried.failures == {}
    assert retried.archived == ["Ticket"]


def test_archive_retries_descriptor_failure_after_unlink(tmp_path: Path, monkeypatch) -> None:
    tio, outer, _project, _branch = _draft(tmp_path, monkeypatch, paired=False)
    descriptor = outer / ".booley_project" / ".runtime" / "acceptance" / "drafts" / "ticket.json"
    original = Path.unlink

    def fail_descriptor(path: Path, *args, **kwargs) -> None:
        if path == descriptor:
            raise OSError("injected descriptor failure")
        original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_descriptor)
    failed = op_archive(tio, slug="ticket", force=True)
    assert "injected descriptor failure" in failed.failures["ticket"]
    assert descriptor.exists()

    monkeypatch.setattr(Path, "unlink", original)
    retried = op_archive(tio, slug="ticket", force=True)
    assert retried.failures == {}
    assert retried.archived == ["Ticket"]
    assert not descriptor.exists()


@pytest.mark.parametrize("command", [_cmd_archive, "harness"])
def test_partial_done_sweep_fails_in_both_clis(
    tmp_path: Path, command, capsys, monkeypatch
) -> None:
    from booley.harness.booley import _cmd_board_archive

    reset_cache()
    monkeypatch.delenv("BOOLEY_PROJECT_DIR", raising=False)
    _root, project, tio = _paired_basis_project(tmp_path)
    ticket = _create_v2_ticket(
        tio,
        "a-good",
        TicketFileSpec(
            summary="Good Ticket",
            ticket_type="feature",
            branch="main",
            scope=["README.md"],
            criteria={"mandatory": {"review_rtl_bugs": True}},
        ),
    )
    assert ticket is not None
    assert tio.enqueue_ticket("a-good")
    queued = project / "tickets" / "board" / "queue" / "a-good.md"
    done = project / "tickets" / "board" / "done"
    done.mkdir(parents=True)
    queued.rename(done / queued.name)
    invalid = tio.tickets_dir / "board" / "done" / "z-invalid.md"
    invalid.write_text("invalid Ticket\n", encoding="utf-8")
    args = SimpleNamespace(slug=None, keep_logs=False, force=False)

    if command == "harness":
        result = _cmd_board_archive(args, tio)
    else:
        result = command(tio, args)

    captured = capsys.readouterr()
    assert result == 1
    assert "Good Ticket" in captured.out
    assert "z-invalid" in captured.err
    assert not (tio.tickets_dir / "board" / "done" / "a-good.md").exists()
    assert invalid.exists()

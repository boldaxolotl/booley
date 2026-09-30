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
from booley.ticket_board.board_layout import (
    StateRecord,
    read_state_record,
    state_record_path,
    ticket_document_path,
    write_state_record,
)
from booley.ticket_board.cli_handlers import _cmd_archive
from booley.ticket_board.frontmatter import parse_frontmatter
from booley.ticket_board.io import TicketFileSpec, TicketIO
from booley.ticket_board.lifecycle import TicketState
from booley.ticket_board.paths import human_log_file, ticket_log_dir
from booley.ticket_board.ticket_baseline import ticket_baseline_from_machine
from booley.ticket_board.ticket_history import read_closed_ticket
from booley.ticket_board.workspace_ops import load_draft_generation
from tests.ticket_board.test_ticket_baseline import (
    _basis_project,
    _create_v2_ticket,
    _paired_basis_project,
)

DRAFT_GENERATION = "0123456789abcdef"


def _mark_done(tio: TicketIO, slug: str) -> None:
    """Record *slug* as done, the way completion leaves it before it closes."""
    record = read_state_record(tio.tickets_dir, slug)
    assert record is not None
    write_state_record(tio.tickets_dir, slug, record.with_state(TicketState.DONE))


def _assert_live(tio: TicketIO, slug: str) -> None:
    """Assert *slug* is still on the Board: it never closed into Ticket History."""
    assert ticket_document_path(tio.tickets_dir, slug).exists()
    assert read_closed_ticket(tio.tickets_dir, slug) is None


def _marker(tio: TicketIO, slug: str) -> Path:
    """Return the resumable archive marker path for *slug*."""
    return archive_module._marker_path(Path(tio._project_root), slug)


def _assert_archived(tio: TicketIO, slug: str, generation: str) -> None:
    """Assert *slug* closed as archived: only its history record and logs remain."""
    closed = read_closed_ticket(tio.tickets_dir, slug)
    assert closed is not None
    assert closed.block.outcome is TicketState.ARCHIVED
    assert closed.block.generation == generation
    assert not ticket_document_path(tio.tickets_dir, slug).exists()
    assert not state_record_path(tio.tickets_dir, slug).exists()
    assert not _marker(tio, slug).exists()


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
    ticket = tickets / "board" / "ticket.md"
    ticket.parent.mkdir(parents=True)
    ticket.write_text(
        "---\nsummary: Ticket\ntype: feature\nbranch: main\nscope: []\n"
        "on_success: [review]\n"
        "CRITERIA_MANDATORY: {REVIEW: {rtl: {bugs: done}}}\n"
        "---\n## Description\nCheck the Ticket.\n",
        encoding="utf-8",
    )
    token = DRAFT_GENERATION
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

    outcome = op_archive(tio, slug="ticket")

    assert outcome.failures == {}
    assert outcome.archived == ["Ticket"]
    assert _git(outer, "branch", "--list", branch) == ""
    assert _git(project, "branch", "--list", branch) == ""
    assert branch not in _git(outer, "worktree", "list", "--porcelain")
    assert branch not in _git(project, "worktree", "list", "--porcelain")
    assert _git(outer, "branch", "--list", unrelated)
    assert _git(project, "branch", "--list", unrelated)
    assert unrelated_path.is_dir()
    _assert_archived(tio, "ticket", DRAFT_GENERATION)
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

    outcome = op_archive(tio, slug="ticket")

    assert outcome.failures == {}
    assert outcome.archived == ["Ticket"]
    assert _git(outer, "branch", "--list", branch) == ""
    assert _git(project, "branch", "--list", branch) == ""
    _assert_archived(tio, "ticket", DRAFT_GENERATION)


def test_archive_releases_recorded_legacy_resources(tmp_path: Path, monkeypatch) -> None:
    tio, outer, project, _branch = _draft(tmp_path, monkeypatch, paired=True)
    assert project is not None
    # find_ticket uses the slug as a feature_branch fallback; it does not own
    # an unrelated outer branch merely because the names happen to match.
    _git(outer, "branch", "ticket", "main")
    project_legacy = outer.parent / "project-legacy"
    _git(project, "worktree", "add", "-b", "booley-ticket/ticket", str(project_legacy), "main")

    outcome = op_archive(tio, slug="ticket")

    assert outcome.failures == {}
    assert _git(outer, "branch", "--list", "ticket")
    assert _git(project, "branch", "--list", "booley-ticket/ticket") == ""
    assert not project_legacy.exists()


def test_archive_refuses_unidentified_canonical_workspace(tmp_path: Path, monkeypatch) -> None:
    tio, outer, project, branch = _draft(tmp_path, monkeypatch, paired=False)
    assert project is None
    descriptor = outer / ".booley_project" / ".runtime" / "acceptance" / "drafts" / "ticket.json"
    descriptor.unlink()

    outcome = op_archive(tio, slug="ticket")

    assert "ticket" in outcome.failures
    _assert_live(tio, "ticket")
    assert _git(outer, "branch", "--list", branch)


def test_archive_refuses_unbound_generation_without_descriptor(
    tmp_path: Path, monkeypatch
) -> None:
    tio, outer, project, branch = _draft(tmp_path, monkeypatch, paired=False)
    assert project is None
    canonical = outer / ".booley_project" / "worktrees" / "ticket"
    _git(outer, "worktree", "remove", "--force", str(canonical))
    descriptor = outer / ".booley_project" / ".runtime" / "acceptance" / "drafts" / "ticket.json"
    descriptor.unlink()

    outcome = op_archive(tio, slug="ticket")

    assert "generation refs without a descriptor" in outcome.failures["ticket"]
    assert _git(outer, "branch", "--list", branch)
    _assert_live(tio, "ticket")


def test_archive_workspace_free_draft_without_descriptor(tmp_path: Path, monkeypatch) -> None:
    tio, outer, project, branch = _draft(tmp_path, monkeypatch, paired=False)
    assert project is None
    _git(outer, "worktree", "remove", "--force", str(outer / ".booley_project/worktrees/ticket"))
    _git(outer, "branch", "-D", branch)
    descriptor = outer / ".booley_project" / ".runtime" / "acceptance" / "drafts" / "ticket.json"
    descriptor.unlink()

    outcome = op_archive(tio, slug="ticket")

    assert outcome.failures == {}
    assert outcome.archived == ["Ticket"]
    # A draft that never materialized a workspace closes without a generation.
    _assert_archived(tio, "ticket", "")


def test_archive_rejects_corrupt_draft_descriptor(tmp_path: Path, monkeypatch) -> None:
    tio, outer, project, branch = _draft(tmp_path, monkeypatch, paired=False)
    assert project is None
    descriptor = outer / ".booley_project" / ".runtime" / "acceptance" / "drafts" / "ticket.json"
    descriptor.write_text("bad descriptor", encoding="utf-8")

    outcome = op_archive(tio, slug="ticket")

    assert "descriptor" in outcome.failures["ticket"]
    _assert_live(tio, "ticket")
    assert _git(outer, "branch", "--list", branch)


def test_archive_refuses_missing_paired_repository(tmp_path: Path, monkeypatch) -> None:
    tio, outer, project, branch = _draft(tmp_path, monkeypatch, paired=True)
    assert project is not None
    (project / ".git").rename(project / ".git-unavailable")

    outcome = op_archive(tio, slug="ticket")

    assert "source repository is unavailable" in outcome.failures["ticket"]
    _assert_live(tio, "ticket")
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
    queued = project / "tickets" / "board" / "published.md"
    fields, _ = parse_frontmatter(queued.read_text(encoding="utf-8"))
    basis = ticket_baseline_from_machine(fields["machine"])
    refs = {row.role: row.ticket_ref for row in basis.participants}
    old_token = load_draft_generation(root, "published")
    assert fields["machine"]["generation"] != old_token

    outcome = op_archive(tio, slug="published")

    assert outcome.archived == ["Published Ticket"]
    assert outcome.failures == {}
    assert not queued.exists()
    assert not state_record_path(tio.tickets_dir, "published").exists()
    assert _git(root, "branch", "--list", refs["outer"].removeprefix("refs/heads/")) == ""
    assert _git(project, "branch", "--list", refs["project"].removeprefix("refs/heads/")) == ""
    assert refs["outer"] not in _git(root, "worktree", "list", "--porcelain")
    assert refs["project"] not in _git(project, "worktree", "list", "--porcelain")
    _assert_archived(tio, "published", fields["machine"]["generation"])
    # Ticket History keeps the slug taken: a Closed Ticket is never reopened.
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
    assert replacement is None
    closed = read_closed_ticket(tio.tickets_dir, "published")
    assert closed is not None and "Published Ticket" in closed.document


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
    queued = project / "tickets" / "board" / "single.md"
    fields, _ = parse_frontmatter(queued.read_text(encoding="utf-8"))
    ref = fields["machine"]["baseline"]["outer"]["ticket_ref"]

    outcome = op_archive(tio, slug="single")

    assert outcome.failures == {}
    assert outcome.archived == ["Single Repository"]
    assert _git(root, "branch", "--list", ref.removeprefix("refs/heads/")) == ""
    assert ref not in _git(root, "worktree", "list", "--porcelain")
    _assert_archived(tio, "single", fields["machine"]["generation"])


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
    failed = op_archive(tio, slug="ticket")
    assert f"injected {role} worktree failure" in failed.failures["ticket"]
    _assert_live(tio, "ticket")
    assert _git(outer, "branch", "--list", branch)
    assert _git(project, "branch", "--list", branch)

    monkeypatch.setattr(archive_generation, "_require_git", original)
    retried = op_archive(tio, slug="ticket")
    assert retried.failures == {}
    assert retried.archived == ["Ticket"]
    _assert_archived(tio, "ticket", DRAFT_GENERATION)


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
    failed = op_archive(tio, slug="ticket")
    assert "ticket" in failed.failures
    assert changed
    _assert_live(tio, "ticket")
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
    failed = op_archive(tio, slug="ticket")
    assert "injected branch deletion failure" in failed.failures["ticket"]
    _assert_live(tio, "ticket")
    assert _git(project, "branch", "--list", branch)

    monkeypatch.setattr(archive_generation, "_require_git", original)
    retried = op_archive(tio, slug="ticket")
    assert retried.failures == {}
    assert retried.archived == ["Ticket"]
    _assert_archived(tio, "ticket", DRAFT_GENERATION)
    assert _git(outer, "branch", "--list", branch) == ""


def _crash_at(monkeypatch, tio: TicketIO, checkpoint: str) -> None:
    """Make the next archive fail at *checkpoint*, leaving its marker behind."""

    def fail(*_args: object, **_kwargs: object) -> None:
        raise OSError(f"injected {checkpoint} failure")

    targets = {
        "transition": (tio, "_append_transition_unlocked"),
        "close": (archive_module, "close_ticket"),
        "session-cleanup": (archive_module, "_cleanup_session_files"),
        "marker-removal": (archive_module, "durable_unlink"),
    }
    monkeypatch.setattr(*targets[checkpoint], fail)


@pytest.mark.parametrize(
    "checkpoint,closed",
    [
        ("transition", False),
        ("close", False),
        ("session-cleanup", True),
        ("marker-removal", True),
    ],
)
def test_bare_archive_resumes_crash_at_each_checkpoint(
    tmp_path: Path, monkeypatch, checkpoint: str, closed: bool
) -> None:
    tio, outer, project, branch = _draft(tmp_path, monkeypatch, paired=False)
    assert project is None
    # The conftest hides host-global ignore rules, so Git tracks Ticket History.
    # Opting out of the Stealth policy keeps the unredacted subject asserted below.
    # (Do not set ``core.excludesFile`` to ``os.devnull`` here: with ``nul`` as
    # the excludes file, ``git status`` exited 128 on the Windows CI runner.)
    config = outer / ".booley_project" / "booley.toml"
    config.parent.mkdir(exist_ok=True)
    config.write_text("[stealth]\nenabled = false\n", encoding="utf-8")
    log_dir = ticket_log_dir(tio.logs_dir, "ticket")
    log_dir.mkdir(parents=True)
    (log_dir / "run.log").write_text("evidence\n", encoding="utf-8")
    (log_dir / "developer.session_id").write_text("stale\n", encoding="utf-8")
    document = ticket_document_path(tio.tickets_dir, "ticket")

    with monkeypatch.context() as crash:
        _crash_at(crash, tio, checkpoint)
        failed = op_archive(tio, slug="ticket")

    assert f"injected {checkpoint} failure" in failed.failures["ticket"]
    assert failed.archived == []
    assert _marker(tio, "ticket").exists()
    assert document.exists() is not closed
    assert (read_closed_ticket(tio.tickets_dir, "ticket") is not None) is closed
    # Every checkpoint follows the release, so none leaves the Ticket's refs behind.
    assert _git(outer, "branch", "--list", branch) == ""

    resumed = op_archive(tio)

    assert resumed.failures == {}
    assert resumed.archived == ["Ticket"]
    _assert_archived(tio, "ticket", DRAFT_GENERATION)
    assert (log_dir / "run.log").read_text(encoding="utf-8") == "evidence\n"
    assert not (log_dir / "developer.session_id").exists()
    transitions = human_log_file(tio.logs_dir, "ticket", "transitions.log")
    assert transitions.read_text(encoding="utf-8").count("user archived") == 1
    assert _git(outer, "log", "-1", "--format=%s") == "chore(ticket): close Ticket (archived)"
    assert _git(outer, "status", "--porcelain", "--", ".booley_project/tickets/history") == ""


@pytest.mark.parametrize("released", [False, True])
def test_archive_resumes_crash_around_workspace_release(
    tmp_path: Path, monkeypatch, released: bool
) -> None:
    """A crash before or after the release leaves a marker that resumes it."""
    tio, outer, _project, branch = _draft(tmp_path, monkeypatch, paired=False)
    original = archive_module._release_workspaces

    def crash(*args: object) -> None:
        if released:
            original(*args)
        raise OSError("injected release crash")

    with monkeypatch.context() as patch:
        patch.setattr(archive_module, "_release_workspaces", crash)
        failed = op_archive(tio, slug="ticket")

    assert "injected release crash" in failed.failures["ticket"]
    _assert_live(tio, "ticket")
    marker = json.loads(_marker(tio, "ticket").read_text(encoding="utf-8"))
    assert marker["released"] is False
    assert bool(_git(outer, "branch", "--list", branch)) is not released

    resumed = op_archive(tio)

    assert resumed.failures == {}
    assert resumed.archived == ["Ticket"]
    _assert_archived(tio, "ticket", DRAFT_GENERATION)
    assert _git(outer, "branch", "--list", branch) == ""
    assert not (outer / ".booley_project" / "worktrees" / "ticket").exists()


def test_archive_resumes_marker_without_released_key(tmp_path: Path, monkeypatch) -> None:
    """A marker without ``released`` resumes by releasing, which is idempotent."""
    tio, outer, _project, branch = _draft(tmp_path, monkeypatch, paired=False)
    with monkeypatch.context() as crash:
        _crash_at(crash, tio, "transition")
        assert "ticket" in op_archive(tio, slug="ticket").failures
    marker_path = _marker(tio, "ticket")
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    del marker["released"]
    marker_path.write_text(json.dumps(marker), encoding="utf-8")

    resumed = op_archive(tio)

    assert resumed.failures == {}
    _assert_archived(tio, "ticket", DRAFT_GENERATION)
    assert _git(outer, "branch", "--list", branch) == ""


def test_archive_refuses_owner_that_claims_after_unlocked_check(
    tmp_path: Path, monkeypatch
) -> None:
    """A runner claiming between the unlocked check and the lock still wins."""
    tio, outer, _project, branch = _draft(tmp_path, monkeypatch, paired=False)
    owner = subprocess.Popen(["sleep", "30"])
    original = archive_module._refuse_live_owner

    def claim_after_check(tio_arg: TicketIO, slug: str) -> None:
        original(tio_arg, slug)
        record = StateRecord.fresh(TicketState.RUNNING, execution_owner_pid=owner.pid)
        write_state_record(tio_arg.tickets_dir, slug, record)

    try:
        monkeypatch.setattr(archive_module, "_refuse_live_owner", claim_after_check)
        outcome = op_archive(tio, slug="ticket")
    finally:
        owner.kill()
        owner.wait(timeout=10)

    assert f"live process {owner.pid}" in outcome.failures["ticket"]
    _assert_live(tio, "ticket")
    assert not _marker(tio, "ticket").exists()
    assert _git(outer, "branch", "--list", branch)
    assert (outer / ".booley_project" / "worktrees" / "ticket").exists()


def test_archive_refuses_done_ticket(tmp_path: Path, monkeypatch) -> None:
    reset_cache()
    monkeypatch.delenv("BOOLEY_PROJECT_DIR", raising=False)
    root, project, tio = _paired_basis_project(tmp_path)
    ticket = _create_v2_ticket(
        tio,
        "ticket",
        TicketFileSpec(
            summary="Ticket", ticket_type="feature", branch="main", scope=["README.md"]
        ),
    )
    assert ticket is not None
    assert tio.enqueue_ticket("ticket")
    queued = project / "tickets" / "board" / "ticket.md"
    fields, _ = parse_frontmatter(queued.read_text(encoding="utf-8"))
    ref = fields["machine"]["baseline"]["outer"]["ticket_ref"]
    _mark_done(tio, queued.stem)
    document = queued.read_bytes()

    outcome = op_archive(tio, slug="ticket")

    assert outcome.archived == []
    assert "closes when its completion finishes" in outcome.failures["ticket"]
    assert queued.read_bytes() == document
    record = read_state_record(tio.tickets_dir, "ticket")
    assert record is not None and record.state is TicketState.DONE
    assert read_closed_ticket(tio.tickets_dir, "ticket") is None
    assert not _marker(tio, "ticket").exists()
    assert _git(root, "branch", "--list", ref.removeprefix("refs/heads/"))


def test_archive_refuses_already_closed_ticket(tmp_path: Path, monkeypatch) -> None:
    tio, _outer, _project, _branch = _draft(tmp_path, monkeypatch, paired=False)
    assert op_archive(tio, slug="ticket").failures == {}
    history = read_closed_ticket(tio.tickets_dir, "ticket")
    assert history is not None
    before = history.path.read_bytes()

    again = op_archive(tio, slug="ticket")

    assert again.archived == []
    assert "already closed (archived)" in again.failures["ticket"]
    assert history.path.read_bytes() == before
    assert not _marker(tio, "ticket").exists()


@pytest.mark.parametrize(
    "field,bad_value,message",
    [
        ("unknown", True, "archive marker is invalid"),
        ("step", 1, "step must be a non-empty string"),
        ("digest", "bad", "archive marker identity is invalid"),
    ],
)
def test_resume_reports_corrupt_recovery_marker(
    tmp_path: Path, monkeypatch, field: str, bad_value: object, message: str
) -> None:
    tio, _outer, _project, _branch = _draft(tmp_path, monkeypatch, paired=False)
    with monkeypatch.context() as crash:
        _crash_at(crash, tio, "close")
        assert "ticket" in op_archive(tio, slug="ticket").failures
    marker_path = _marker(tio, "ticket")
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    marker[field] = bad_value
    marker_path.write_text(json.dumps(marker), encoding="utf-8")

    outcome = op_archive(tio)

    assert message in outcome.failures["ticket"]
    assert outcome.archived == []
    assert ticket_document_path(tio.tickets_dir, "ticket").exists()
    assert read_closed_ticket(tio.tickets_dir, "ticket") is None


def test_bare_archive_without_markers_archives_nothing(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    tio, outer, _project, branch = _draft(tmp_path, monkeypatch, paired=False)
    document = ticket_document_path(tio.tickets_dir, "ticket")
    before = document.read_bytes()
    args = SimpleNamespace(slug=None, keep_logs=False, force=False)

    result = _cmd_archive(tio, args)

    assert result == 0
    assert (
        "No interrupted archives to resume; name a Ticket to archive it."
        in capsys.readouterr().out
    )
    assert document.read_bytes() == before
    assert read_closed_ticket(tio.tickets_dir, "ticket") is None
    assert _git(outer, "branch", "--list", branch)


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

    outcome = op_archive(tio, slug="ticket")

    assert "ticket" in outcome.failures
    _assert_live(tio, "ticket")
    assert _git(outer, "branch", "--list", branch)
    assert _git(project, "branch", "--list", branch)


def test_completed_amendment_record_does_not_block_archive(tmp_path: Path, monkeypatch) -> None:
    from booley.ticket_board import amendment

    tio, _outer, _project, _branch = _draft(tmp_path, monkeypatch, paired=False)
    monkeypatch.setattr(amendment, "pending_amendment", lambda *_args: {"phase": "queued"})

    outcome = op_archive(tio, slug="ticket")

    assert outcome.failures == {}
    assert outcome.archived == ["Ticket"]
    _assert_archived(tio, "ticket", DRAFT_GENERATION)


def test_archive_retries_transition_failure_once(tmp_path: Path, monkeypatch) -> None:
    tio, _outer, _project, _branch = _draft(tmp_path, monkeypatch, paired=False)
    original = tio._append_transition_unlocked

    def fail_transition(*_args: object) -> None:
        raise OSError("injected transition failure")

    monkeypatch.setattr(tio, "_append_transition_unlocked", fail_transition)
    failed = op_archive(tio, slug="ticket")
    assert "injected transition failure" in failed.failures["ticket"]
    _assert_live(tio, "ticket")

    monkeypatch.setattr(tio, "_append_transition_unlocked", original)
    retried = op_archive(tio, slug="ticket")
    assert retried.failures == {}
    assert retried.archived == ["Ticket"]
    _assert_archived(tio, "ticket", DRAFT_GENERATION)


def test_archive_retries_descriptor_failure_after_close(tmp_path: Path, monkeypatch) -> None:
    tio, outer, _project, _branch = _draft(tmp_path, monkeypatch, paired=False)
    descriptor = outer / ".booley_project" / ".runtime" / "acceptance" / "drafts" / "ticket.json"
    original = Path.unlink

    def fail_descriptor(path: Path, *args, **kwargs) -> None:
        if path == descriptor:
            raise OSError("injected descriptor failure")
        original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_descriptor)
    failed = op_archive(tio, slug="ticket")
    assert "injected descriptor failure" in failed.failures["ticket"]
    assert descriptor.exists()
    # The Ticket already closed; only descriptor retirement is left to resume.
    assert read_closed_ticket(tio.tickets_dir, "ticket") is not None
    assert _marker(tio, "ticket").exists()

    monkeypatch.setattr(Path, "unlink", original)
    retried = op_archive(tio, slug="ticket")
    assert retried.failures == {}
    assert retried.archived == ["Ticket"]
    _assert_archived(tio, "ticket", DRAFT_GENERATION)
    assert not descriptor.exists()


@pytest.mark.parametrize("command", [_cmd_archive, "harness"])
def test_partial_resume_fails_in_both_clis(tmp_path: Path, command, capsys, monkeypatch) -> None:
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
    queued = project / "tickets" / "board" / "a-good.md"
    fields, _ = parse_frontmatter(queued.read_text(encoding="utf-8"))
    with monkeypatch.context() as crash:
        _crash_at(crash, tio, "close")
        assert "a-good" in op_archive(tio, slug="a-good").failures
    assert queued.exists()
    invalid = _marker(tio, "z-invalid")
    invalid.write_text("not json\n", encoding="utf-8")
    args = SimpleNamespace(slug=None, keep_logs=False, force=False)

    if command == "harness":
        result = _cmd_board_archive(args, tio)
    else:
        result = command(tio, args)

    captured = capsys.readouterr()
    assert result == 1
    assert "Good Ticket" in captured.out
    assert "z-invalid" in captured.err
    _assert_archived(tio, "a-good", fields["machine"]["generation"])
    assert invalid.exists()

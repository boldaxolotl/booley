"""Closing Tickets into Ticket History and committing the record (ADR 0065, phase 3)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from booley.runtime.project_dir import reset_cache
from booley.ticket_board import history_publication
from booley.ticket_board.board_layout import (
    StateRecord,
    history_document_path,
    read_state_record,
    state_record_path,
    ticket_document_path,
    write_state_record,
)
from booley.ticket_board.history_publication import (
    HistoryCommitError,
    commit_history_record,
    pending_history_commits,
    recover_ticket_history,
)
from booley.ticket_board.io import TicketIO
from booley.ticket_board.lifecycle import TicketState
from booley.ticket_board.operations import op_promote_waiting
from booley.ticket_board.ticket_history import (
    ClosedBlock,
    TicketHistoryError,
    close_ticket,
    closed_outcomes,
    done_slugs,
    interrupted_closings,
    parse_closed_document,
    read_closed_ticket,
    with_closed_block,
)
from booley.ticket_board.workspace_ops import prepare_converted_ticket_baseline

from .conftest import _make_tio, make_closed_ticket, make_ticket_file, place_ticket

_DOCUMENT = "---\nsummary: Close me\ntype: feature\n---\n## Description\nWork.\n"
_GENERATION = "0123456789abcdef0123456789abcdef"


def _block(outcome: TicketState = TicketState.DONE) -> ClosedBlock:
    return ClosedBlock(outcome, "2026-09-29T12:00:00Z", _GENERATION)


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True, timeout=30
    )
    return result.stdout.strip()


def _repository(tmp_path: Path) -> tuple[Path, TicketIO]:
    """A Git repository whose working tree contains the tickets dir."""
    tio = _make_tio(tmp_path)
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.name", "Test")
    _git(tmp_path, "config", "user.email", "test@example.invalid")
    (tmp_path / "README").write_text("project\n", encoding="utf-8")
    _policy(tmp_path, "enabled = false")
    _git(tmp_path, "add", "README")
    _git(tmp_path, "commit", "-qm", "baseline")
    return tmp_path, tio


def _policy(root: Path, stealth: str) -> None:
    """Write the Project's ``[stealth]`` commit policy."""
    config = root / ".booley_project" / "booley.toml"
    config.parent.mkdir(exist_ok=True)
    config.write_text(f"[stealth]\n{stealth}\n", encoding="utf-8")


def _close(tio: TicketIO, slug: str, outcome: TicketState = TicketState.DONE) -> None:
    make_ticket_file(tio, "review", slug)
    close_ticket(tio.tickets_dir, slug, _block(outcome))


def _commit(tio: TicketIO, slug: str) -> bool:
    return commit_history_record(tio.tickets_dir, slug, policy_root=tio._project_root)


def _record_path(slug: str) -> str:
    return f"tickets/history/{slug}.md"


# The closed block ---------------------------------------------------------------


class TestClosedBlock:
    def test_round_trips_and_strips_back_to_the_original_document(self):
        text = with_closed_block(_DOCUMENT, _block())

        document, block = parse_closed_document(text)

        assert document == _DOCUMENT
        assert block == _block()
        assert text.index("closed:") > text.index("summary:")

    def test_document_without_frontmatter_gets_one(self):
        text = with_closed_block("## Description\nNo frontmatter.\n", _block())

        document, block = parse_closed_document(text)

        assert text.startswith("---\nclosed:\n")
        assert document == "## Description\nNo frontmatter.\n"
        assert block.outcome is TicketState.DONE

    def test_authored_closed_key_cannot_shadow_the_block(self):
        draft = "---\nsummary: x\nclosed: yes please\n---\nbody\n"

        document, block = parse_closed_document(with_closed_block(draft, _block()))

        assert document == draft
        assert block.generation == _GENERATION

    @pytest.mark.parametrize(
        ("outcome", "date", "generation"),
        [
            (TicketState.REVIEW, "2026-09-29T12:00:00Z", ""),
            (TicketState.DONE, "2026-09-29 12:00", ""),
            (TicketState.DONE, "2026-09-29T12:00:00Z", "not-hex"),
        ],
    )
    def test_rejects_invalid_fields(self, outcome, date, generation):
        with pytest.raises(TicketHistoryError):
            ClosedBlock(outcome, date, generation)

    def test_malformed_block_fails_closed(self, tio):
        path = history_document_path(tio.tickets_dir, "bad")
        path.parent.mkdir(parents=True)
        path.write_text("---\nsummary: x\nclosed:\n  outcome: maybe\n---\n", encoding="utf-8")

        with pytest.raises(TicketHistoryError, match="outcome"):
            read_closed_ticket(tio.tickets_dir, "bad")
        with pytest.raises(TicketHistoryError):
            done_slugs(tio.tickets_dir)


# Closing and its crash windows ----------------------------------------------------


class TestCloseTicket:
    def test_moves_document_to_history_and_drops_board_and_record(self, tio):
        board = make_ticket_file(tio, "review", "feat")
        original = board.read_text(encoding="utf-8")

        recorded = close_ticket(tio.tickets_dir, "feat", _block())

        closed = read_closed_ticket(tio.tickets_dir, "feat")
        assert recorded == _block()
        assert closed is not None and closed.document == original
        assert not board.exists()
        assert read_state_record(tio.tickets_dir, "feat") is None
        assert closed_outcomes(tio.tickets_dir) == {"feat": TicketState.DONE}

    def test_crash_after_history_write_is_finished_by_recovery(self, tio):
        make_ticket_file(tio, "review", "feat")
        history = history_document_path(tio.tickets_dir, "feat")
        history.parent.mkdir(parents=True)
        board_text = ticket_document_path(tio.tickets_dir, "feat").read_text(encoding="utf-8")
        history.write_text(with_closed_block(board_text, _block()), encoding="utf-8")

        assert interrupted_closings(tio.tickets_dir) == ["feat"]
        recover_ticket_history(tio)

        assert interrupted_closings(tio.tickets_dir) == []
        assert not ticket_document_path(tio.tickets_dir, "feat").exists()
        assert not state_record_path(tio.tickets_dir, "feat").exists()

    def test_crash_after_board_removal_leaves_only_the_record(self, tio):
        _close(tio, "feat")
        write_state_record(tio.tickets_dir, "feat", StateRecord.fresh(TicketState.DONE))

        assert interrupted_closings(tio.tickets_dir) == ["feat"]
        recover_ticket_history(tio)

        assert not state_record_path(tio.tickets_dir, "feat").exists()

    def test_history_wins_over_a_later_close(self, tio):
        _close(tio, "feat", TicketState.ARCHIVED)
        make_ticket_file(tio, "review", "feat")  # a stale board copy after a crash

        recorded = close_ticket(tio.tickets_dir, "feat", _block(TicketState.DONE))

        assert recorded.outcome is TicketState.ARCHIVED
        closed = read_closed_ticket(tio.tickets_dir, "feat")
        assert closed is not None and closed.closed.outcome is TicketState.ARCHIVED
        assert not ticket_document_path(tio.tickets_dir, "feat").exists()

    def test_close_without_any_document_fails(self, tio):
        with pytest.raises(TicketHistoryError, match="no board document"):
            close_ticket(tio.tickets_dir, "ghost", _block())


# Slug uniqueness ---------------------------------------------------------------------


def _draft(summary: str) -> str:
    return (
        "---\n"
        f"summary: {summary}\n"
        "type: feature\n"
        "branch: main\n"
        "scope: [README.md]\n"
        "on_success: [review]\n"
        "CRITERIA_MANDATORY:\n"
        "  REVIEW: {rtl: {bugs: done}}\n"
        "---\n\n## Description\n\nWork.\n"
    )


def test_create_refuses_a_slug_taken_by_a_closed_ticket(tio, capsys):
    make_closed_ticket(tio, "taken", outcome="archived")

    assert tio.create_ticket_document("taken", _draft("Reuse")) is None
    assert "already closed" in capsys.readouterr().err
    assert not ticket_document_path(tio.tickets_dir, "taken").exists()
    assert tio.create_ticket_document("fresh", _draft("New work")) is not None


# Dependencies ---------------------------------------------------------------------------


class TestDependenciesFromHistory:
    def test_only_done_history_satisfies_dependencies(self, tio):
        make_closed_ticket(tio, "shipped")
        make_closed_ticket(tio, "abandoned", outcome="archived")
        make_ticket_file(tio, "done", "finishing")  # done but not yet closed

        assert done_slugs(tio.tickets_dir) == {"shipped"}

    def test_waiting_ticket_is_promoted_once_its_dependency_closed_done(
        self, tmp_path, monkeypatch
    ):
        project, board = _git_project(tmp_path, monkeypatch)
        _enqueue(project, board, "child", ["dep"])
        assert _state(board, "child") is TicketState.WAITING
        make_ticket_file(board, "review", "dep")
        assert op_promote_waiting(board) == []  # a live dependency is not done yet

        close_ticket(board.tickets_dir, "dep", _block(TicketState.DONE))
        promoted = op_promote_waiting(board)

        assert [row["slug"] for row in promoted] == ["child"]
        assert _state(board, "child") is TicketState.QUEUED

    def test_archived_dependency_blocks_the_waiting_ticket_idempotently(
        self, tmp_path, monkeypatch
    ):
        project, board = _git_project(tmp_path, monkeypatch)
        make_closed_ticket(board, "dep", outcome="archived")
        _enqueue(project, board, "child", ["dep"])

        assert op_promote_waiting(board) == []
        record = read_state_record(board.tickets_dir, "child")
        assert record.state is TicketState.BLOCKED
        assert record.runtime["blocked_reason"] == "dependency-archived"

        assert op_promote_waiting(board) == []
        assert read_state_record(board.tickets_dir, "child") == record

    def test_promotion_during_an_unfinished_archive_waits_then_blocks(self, tmp_path, monkeypatch):
        project, board = _git_project(tmp_path, monkeypatch)
        _enqueue(project, board, "child", ["dep"])
        make_ticket_file(board, "blocked", "dep")

        # The dependency is mid-archive: not yet in history, so still live.
        assert op_promote_waiting(board) == []
        assert _state(board, "child") is TicketState.WAITING

        close_ticket(board.tickets_dir, "dep", _block(TicketState.ARCHIVED))
        op_promote_waiting(board)
        op_promote_waiting(board)

        assert _state(board, "child") is TicketState.BLOCKED


def _git_project(tmp_path: Path, monkeypatch) -> tuple[Path, TicketIO]:
    """The smoke-test Project as a Git repository, so Tickets can enqueue for real."""
    fixture = Path(__file__).resolve().parents[1] / "fixtures" / "ticket_mode_smoke"
    project = tmp_path / "project"
    shutil.copytree(fixture, project)
    monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(project / ".booley_project"))
    reset_cache()
    _git(project, "init", "-q", "-b", "main")
    _git(project, "config", "user.name", "Test")
    _git(project, "config", "user.email", "test@example.invalid")
    _git(project, "add", ".")
    _git(project, "add", "-f", ".booley_project/booley.toml", ".booley_project/.gitignore")
    _git(project, "commit", "-qm", "baseline")
    return project, TicketIO(project / ".booley_project" / "tickets", project_root=project)


def _enqueue(project: Path, board: TicketIO, slug: str, dependencies: list[str]) -> None:
    document = _draft(slug).replace("on_success:", f"dependencies: {dependencies}\non_success:")
    created = board.create_ticket_document(slug, document)
    assert created is not None
    prepare_converted_ticket_baseline(project, created, slug)
    assert board.enqueue_ticket(slug)


def _state(board: TicketIO, slug: str) -> TicketState | None:
    record = read_state_record(board.tickets_dir, slug)
    return None if record is None else record.state


# The history commit ---------------------------------------------------------------------


class TestHistoryCommit:
    def test_commits_only_the_record_and_keeps_staged_user_work(self, tmp_path):
        root, tio = _repository(tmp_path)
        (root / "work.txt").write_text("staged\n", encoding="utf-8")
        _git(root, "add", "work.txt")
        _close(tio, "feat")

        assert pending_history_commits(tio.tickets_dir) == ["feat"]
        assert _commit(tio, "feat") is True

        assert _git(root, "log", "-1", "--format=%s") == "chore(feat): close Ticket (done)"
        assert _git(root, "show", "--name-only", "--format=", "HEAD") == _record_path("feat")
        assert _git(root, "diff", "--cached", "--name-only") == "work.txt"
        assert pending_history_commits(tio.tickets_dir) == []

    def test_retry_is_idempotent(self, tmp_path):
        root, tio = _repository(tmp_path)
        _close(tio, "feat", TicketState.ARCHIVED)
        _commit(tio, "feat")
        head = _git(root, "rev-parse", "HEAD")

        assert _commit(tio, "feat") is True
        assert _git(root, "rev-parse", "HEAD") == head

    def test_crash_before_the_branch_moves_leaves_it_pending(self, tmp_path, monkeypatch):
        root, tio = _repository(tmp_path)
        _close(tio, "feat")
        head = _git(root, "rev-parse", "HEAD")

        def crash(*_args):
            raise HistoryCommitError("crash after commit-tree")

        monkeypatch.setattr(history_publication, "_compare_and_swap", crash)
        with pytest.raises(HistoryCommitError):
            _commit(tio, "feat")
        assert _git(root, "rev-parse", "HEAD") == head
        assert pending_history_commits(tio.tickets_dir) == ["feat"]

        monkeypatch.undo()
        recover_ticket_history(tio)
        assert _git(root, "rev-list", "--count", "HEAD") == "2"
        assert pending_history_commits(tio.tickets_dir) == []

    def test_crash_after_staging_commits_once_on_retry(self, tmp_path, monkeypatch):
        root, tio = _repository(tmp_path)
        _close(tio, "feat")

        def crash(*_args):
            raise HistoryCommitError("crash after staging")

        monkeypatch.setattr(history_publication, "_build_commit", crash)
        with pytest.raises(HistoryCommitError):
            _commit(tio, "feat")
        assert _git(root, "diff", "--cached", "--name-only") == _record_path("feat")
        assert pending_history_commits(tio.tickets_dir) == ["feat"]

        monkeypatch.undo()
        recover_ticket_history(tio)
        assert _git(root, "rev-list", "--count", "HEAD") == "2"
        assert _git(root, "status", "--porcelain", "--", "tickets/history") == ""

    def test_user_commit_after_staging_carries_the_record(self, tmp_path, monkeypatch):
        root, tio = _repository(tmp_path)
        _close(tio, "feat")
        real_build = history_publication._build_commit

        def user_commits_first(repository, parent, path, blob, message):
            _git(root, "commit", "-qm", "user commit")  # takes the staged record along
            return real_build(repository, parent, path, blob, message)

        monkeypatch.setattr(history_publication, "_build_commit", user_commits_first)
        assert _commit(tio, "feat") is True

        assert _git(root, "log", "-1", "--format=%s") == "user commit"
        assert pending_history_commits(tio.tickets_dir) == []
        assert _git(root, "status", "--porcelain", "--", "tickets/history") == ""

    def test_unstaged_committed_record_is_not_restaged(self, tmp_path):
        root, tio = _repository(tmp_path)
        _close(tio, "feat")
        _commit(tio, "feat")
        _git(root, "rm", "-q", "--cached", _record_path("feat"))

        assert pending_history_commits(tio.tickets_dir) == []
        recover_ticket_history(tio)
        assert _git(root, "diff", "--cached", "--name-only") == _record_path("feat")

    def test_branch_that_moved_is_rebuilt_on_its_new_head(self, tmp_path, monkeypatch):
        root, tio = _repository(tmp_path)
        _close(tio, "feat")
        real_build = history_publication._build_commit
        moved = []

        def build_then_move(repository, parent, path, blob, message):
            commit = real_build(repository, parent, path, blob, message)
            if not moved:
                # Another writer moves the branch without the user's index.
                tree = _git(root, "rev-parse", "HEAD^{tree}")
                other = _git(
                    root, "commit-tree", tree, "-p", "HEAD", "-m", "concurrent user commit"
                )
                _git(root, "update-ref", "refs/heads/main", other)
                moved.append(True)
            return commit

        monkeypatch.setattr(history_publication, "_build_commit", build_then_move)
        assert _commit(tio, "feat") is True

        assert _git(root, "log", "--format=%s", "-2").splitlines() == [
            "chore(feat): close Ticket (done)",
            "concurrent user commit",
        ]

    def test_detached_head_keeps_the_ticket_closed_and_pending(self, tmp_path, capsys):
        root, tio = _repository(tmp_path)
        _git(root, "checkout", "-q", "--detach")
        _close(tio, "feat")

        recover_ticket_history(tio)

        assert "detached HEAD" in capsys.readouterr().err
        assert read_closed_ticket(tio.tickets_dir, "feat") is not None
        assert pending_history_commits(tio.tickets_dir) == ["feat"]

    def test_record_committed_with_other_content_is_not_overwritten(self, tmp_path):
        _root, tio = _repository(tmp_path)
        _close(tio, "feat")
        _commit(tio, "feat")
        history = history_document_path(tio.tickets_dir, "feat")
        history.write_text(history.read_text(encoding="utf-8") + "edited\n", encoding="utf-8")

        assert pending_history_commits(tio.tickets_dir) == []
        with pytest.raises(HistoryCommitError, match="other content"):
            _commit(tio, "feat")

    def test_ignored_or_untracked_history_needs_no_commit(self, tmp_path):
        root, tio = _repository(tmp_path)
        (root / ".gitignore").write_text("tickets/\n", encoding="utf-8")
        _close(tio, "feat")

        assert pending_history_commits(tio.tickets_dir) == []
        assert _commit(tio, "feat") is False

    def test_commit_policy_banned_phrase_refuses_the_commit(self, tmp_path):
        root, tio = _repository(tmp_path)
        _policy(root, 'banned_words = ["forbidden"]')
        _close(tio, "forbidden-work")

        with pytest.raises(HistoryCommitError, match="commit policy"):
            _commit(tio, "forbidden-work")
        assert pending_history_commits(tio.tickets_dir) == ["forbidden-work"]

    def test_commit_policy_identity_allowlist_refuses_the_commit(self, tmp_path):
        root, tio = _repository(tmp_path)
        _policy(root, 'banned_words = []\nallowed_authors = ["*@allowed.invalid"]')
        _close(tio, "feat")

        with pytest.raises(HistoryCommitError, match="allowed_authors"):
            _commit(tio, "feat")
        _git(root, "config", "user.email", "dev@allowed.invalid")
        assert _commit(tio, "feat") is True

    def test_waits_while_an_acceptance_holds_the_publication_lock(self, tmp_path):
        from booley.ticket_board.acceptance_journal import publication_idle

        root, tio = _repository(tmp_path)
        _close(tio, "feat")
        head = _git(root, "rev-parse", "HEAD")

        with publication_idle(root), pytest.raises(HistoryCommitError, match="publishing"):
            _commit(tio, "feat")
        assert _git(root, "rev-parse", "HEAD") == head
        assert _commit(tio, "feat") is True

    def test_no_repository_needs_no_commit(self, tio):
        _close(tio, "feat")

        assert pending_history_commits(tio.tickets_dir) == []
        assert _commit(tio, "feat") is False


def test_ticket_status_reports_the_closed_outcome(tmp_path, monkeypatch):
    from booley.harness._ticket_ops import DirectTicketOps

    tio = _make_tio(tmp_path)
    monkeypatch.setattr(DirectTicketOps, "_tio", staticmethod(lambda _root: tio))
    _close(tio, "shipped")
    _close(tio, "dropped", TicketState.ARCHIVED)

    ops = DirectTicketOps()
    assert ops.ticket_status(tmp_path, "shipped") == "done"
    assert ops.ticket_status(tmp_path, "dropped.md") == "archived"
    assert ops.ticket_status(tmp_path, "unknown") == ""


# Review fixes -----------------------------------------------------------------------------


def test_non_record_file_in_history_is_ignored_not_fatal(tio):
    readme = history_document_path(tio.tickets_dir, "README")
    readme.parent.mkdir(parents=True)
    readme.write_text("# Ticket History\n", encoding="utf-8")
    make_ticket_file(tio, "queue", "README")  # a live Ticket that happens to share the name
    make_closed_ticket(tio, "shipped")

    assert done_slugs(tio.tickets_dir) == {"shipped"}
    assert read_closed_ticket(tio.tickets_dir, "README") is None
    assert interrupted_closings(tio.tickets_dir) == []
    recover_ticket_history(tio)
    assert ticket_document_path(tio.tickets_dir, "README").exists()


def test_malformed_record_fails_the_cli_with_an_error_not_a_traceback(tio, monkeypatch, capsys):
    from booley.ticket_board import cli

    path = history_document_path(tio.tickets_dir, "bad")
    path.parent.mkdir(parents=True)
    path.write_text("---\nclosed:\n  outcome: maybe\n---\n", encoding="utf-8")
    monkeypatch.setattr(cli, "detect_tickets_dir", lambda: tio.tickets_dir)

    assert cli.main(["classify"]) == 2
    assert "bad.md is invalid" in capsys.readouterr().err


def test_move_ticket_to_archived_is_refused(tio, capsys):
    make_ticket_file(tio, "blocked", "feat")

    assert tio.move_ticket_file("feat", TicketState.ARCHIVED) is False
    assert "board archive feat" in capsys.readouterr().err
    assert read_state_record(tio.tickets_dir, "feat").state is TicketState.BLOCKED


def test_read_only_cli_commands_never_recover(tio, monkeypatch):
    from booley.ticket_board import cli

    calls = []
    monkeypatch.setattr(cli, "detect_tickets_dir", lambda: tio.tickets_dir)
    monkeypatch.setattr(cli, "recover_ticket_history", calls.append)

    cli.main(["classify"])
    assert calls == []
    cli.main(["promote-waiting"])
    assert len(calls) == 1


def test_lost_done_close_is_finished_by_the_next_promotion(tmp_path, monkeypatch):
    project, board = _git_project(tmp_path, monkeypatch)
    _enqueue(project, board, "feat", [])
    # Completion finished (no Acceptance Journal: approve without merge), then
    # the close was lost to a crash.
    record = read_state_record(board.tickets_dir, "feat")
    write_state_record(board.tickets_dir, "feat", record.with_state(TicketState.DONE))

    op_promote_waiting(board)

    closed = read_closed_ticket(board.tickets_dir, "feat")
    assert closed is not None and closed.closed.outcome is TicketState.DONE
    assert len(closed.closed.generation) == 32
    assert not ticket_document_path(board.tickets_dir, "feat").exists()


def test_done_ticket_with_unfinished_cleanup_is_not_closed(tio):
    from booley.runtime.project_dir import runtime_dir

    make_ticket_file(tio, "done", "feat")
    journal = runtime_dir(tio._project_root) / "acceptance" / "cleanup-only" / "feat" / "b.json"
    journal.parent.mkdir(parents=True)
    journal.write_text('{"state": "approved"}', encoding="utf-8")

    op_promote_waiting(tio)

    assert read_closed_ticket(tio.tickets_dir, "feat") is None
    assert read_state_record(tio.tickets_dir, "feat").state is TicketState.DONE


def test_retired_archive_flags_only_earn_a_note(tio, capsys):
    from booley.ticket_board.archive import run_archive_command

    place_ticket(tio.tickets_dir, "feat", "drafts", _draft("Abandoned"))

    assert run_archive_command(tio, "feat", force=True, keep_logs=True) == 0
    err = capsys.readouterr().err
    assert "--force has no effect" in err and "--keep-logs has no effect" in err
    assert read_closed_ticket(tio.tickets_dir, "feat").closed.outcome is TicketState.ARCHIVED

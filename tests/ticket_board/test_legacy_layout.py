"""Detection of a Ticket Board still in the pre-ADR-0065 layout (the legacy guard)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from booley.ticket_board import legacy_layout
from booley.ticket_board.legacy_layout import (
    MIGRATION_GUIDE,
    LegacyBoardLayoutError,
    TrackedStateCheckError,
    legacy_layout_problems,
    legacy_state_files,
    require_current_layout,
    tracked_live_state_files,
)
from booley.ticket_board.lifecycle import TicketState


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout


def _current_tree(tickets_dir: Path) -> Path:
    for name in ("board", "state", "history", "logs", "locks"):
        (tickets_dir / name).mkdir(parents=True, exist_ok=True)
    return tickets_dir


def _repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q")
    _git(root, "config", "core.excludesFile", "/dev/null")
    return root


# Legacy state directories -------------------------------------------------------


def test_current_layout_has_no_problems(tmp_path: Path) -> None:
    tickets_dir = _current_tree(tmp_path / "tickets")
    (tickets_dir / "board" / "a.md").write_text("# a\n", encoding="utf-8")
    (tickets_dir / "state" / "a.json").write_text("{}\n", encoding="utf-8")

    assert legacy_state_files(tickets_dir) == []
    assert legacy_layout_problems(tickets_dir) == []
    require_current_layout(tickets_dir)


def test_missing_tickets_dir_has_no_problems(tmp_path: Path) -> None:
    assert legacy_layout_problems(tmp_path / "absent") == []


@pytest.mark.parametrize("state", list(TicketState))
def test_every_old_state_directory_is_detected(tmp_path: Path, state: TicketState) -> None:
    tickets_dir = _current_tree(tmp_path / "tickets")
    old = tickets_dir / "board" / state.dir_name
    old.mkdir()
    (old / "x.md").write_text("# x\n", encoding="utf-8")

    assert legacy_state_files(tickets_dir) == [old / "x.md"]


def test_files_nested_in_old_state_directories_count(tmp_path: Path) -> None:
    tickets_dir = _current_tree(tmp_path / "tickets")
    nested = tickets_dir / "board" / "done" / "sub"
    nested.mkdir(parents=True)
    (nested / "y.md").write_text("# y\n", encoding="utf-8")
    (tickets_dir / "board" / "queue").mkdir()
    (tickets_dir / "board" / "queue" / ".gitkeep").write_text("", encoding="utf-8")

    assert legacy_state_files(tickets_dir) == [
        tickets_dir / "board" / "done" / "sub" / "y.md",
        tickets_dir / "board" / "queue" / ".gitkeep",
    ]


def test_empty_old_state_directories_are_harmless(tmp_path: Path) -> None:
    """A leftover empty directory holds no Ticket, so it cannot hide one."""
    tickets_dir = _current_tree(tmp_path / "tickets")
    (tickets_dir / "board" / "queue").mkdir()
    (tickets_dir / "board" / "active" / "empty").mkdir(parents=True)

    assert legacy_layout_problems(tickets_dir) == []


def test_unknown_board_subdirectory_is_not_legacy(tmp_path: Path) -> None:
    tickets_dir = _current_tree(tmp_path / "tickets")
    (tickets_dir / "board" / "notes").mkdir()
    (tickets_dir / "board" / "notes" / "x.md").write_text("# x\n", encoding="utf-8")

    assert legacy_state_files(tickets_dir) == []


def test_legacy_directories_raise_with_fix_and_guide(tmp_path: Path) -> None:
    tickets_dir = _current_tree(tmp_path / "tickets")
    (tickets_dir / "board" / "queue").mkdir()
    (tickets_dir / "board" / "queue" / "x.md").write_text("# x\n", encoding="utf-8")
    (tickets_dir / "board" / "done").mkdir()
    (tickets_dir / "board" / "done" / "y.md").write_text("# y\n", encoding="utf-8")

    with pytest.raises(LegacyBoardLayoutError) as caught:
        require_current_layout(tickets_dir)

    message = str(caught.value)
    assert "2 file(s)" in message
    assert "board/done/, board/queue/" in message
    assert MIGRATION_GUIDE in message
    assert "CHANGELOG" in message


# Tracked live state ---------------------------------------------------------------


def test_untracked_board_and_state_pass(tmp_path: Path) -> None:
    root = _repo(tmp_path / "repo")
    tickets_dir = _current_tree(root / ".booley_project" / "tickets")
    (tickets_dir / "board" / "a.md").write_text("# a\n", encoding="utf-8")
    (tickets_dir / "history" / "b.md").write_text("# b\n", encoding="utf-8")
    _git(root, "add", str(tickets_dir / "history" / "b.md"))

    assert tracked_live_state_files(tickets_dir) == []


def test_tracked_board_and_state_files_are_reported(tmp_path: Path) -> None:
    root = _repo(tmp_path / "repo")
    tickets_dir = _current_tree(root / ".booley_project" / "tickets")
    (tickets_dir / "board" / "queue").mkdir()
    (tickets_dir / "board" / "queue" / "a.md").write_text("# a\n", encoding="utf-8")
    (tickets_dir / "state" / "b.json").write_text("{}\n", encoding="utf-8")
    (tickets_dir / "history" / "c.md").write_text("# c\n", encoding="utf-8")
    _git(root, "add", "-A")

    assert tracked_live_state_files(tickets_dir) == ["board/queue/a.md", "state/b.json"]


def test_tracked_file_deleted_from_disk_still_counts(tmp_path: Path) -> None:
    """The index entry, not the working file, is what the next commit would keep."""
    root = _repo(tmp_path / "repo")
    tickets_dir = _current_tree(root / "tickets")
    (tickets_dir / "board" / "a.md").write_text("# a\n", encoding="utf-8")
    _git(root, "add", "-A")
    (tickets_dir / "board" / "a.md").unlink()

    assert tracked_live_state_files(tickets_dir) == ["board/a.md"]


def test_tracked_files_raise_with_git_rm_cached_fix(tmp_path: Path) -> None:
    root = _repo(tmp_path / "repo")
    tickets_dir = _current_tree(root / "tickets")
    (tickets_dir / "board" / "a.md").write_text("# a\n", encoding="utf-8")
    _git(root, "add", "-A")

    with pytest.raises(LegacyBoardLayoutError) as caught:
        require_current_layout(tickets_dir)

    message = str(caught.value)
    assert "Git tracks 1 file(s)" in message
    assert f"git -C {tickets_dir} rm -r --cached --ignore-unmatch -- board state" in message
    assert MIGRATION_GUIDE in message


def test_printed_fix_untracks_an_old_board_that_never_tracked_state(tmp_path: Path) -> None:
    """Old boards tracked board/ only; the fix must not fail on the unmatched state/."""
    root = _repo(tmp_path / "my repo")
    tickets_dir = _current_tree(root / "tickets")
    (tickets_dir / "board" / "a.md").write_text("# a\n", encoding="utf-8")
    _git(root, "add", "-A")
    (problem,) = legacy_layout_problems(tickets_dir)
    command = problem.fix.removesuffix(" && commit the removal")

    subprocess.run(command, shell=True, cwd=root, check=True, capture_output=True)

    assert tracked_live_state_files(tickets_dir) == []
    assert (tickets_dir / "board" / "a.md").is_file()


def test_printed_fix_untracks_in_a_stealth_project_data_repository(tmp_path: Path) -> None:
    """A stealth .booley_project is its own repository nested in the Project's.

    The fix is run from the Project root, where plain ``git`` means the outer
    repository; it must still untrack the files the inner one tracks.
    """
    root = _repo(tmp_path / "project")
    project_data = _repo(root / ".booley_project")
    tickets_dir = _current_tree(project_data / "tickets")
    (tickets_dir / "board" / "a.md").write_text("# a\n", encoding="utf-8")
    _git(project_data, "add", "-A")
    (problem,) = legacy_layout_problems(tickets_dir)
    command = problem.fix.removesuffix(" && commit the removal")

    subprocess.run(command, shell=True, cwd=root, check=True, capture_output=True)

    assert tracked_live_state_files(tickets_dir) == []
    assert (tickets_dir / "board" / "a.md").is_file()


def test_tickets_dir_outside_any_repository_passes(tmp_path: Path) -> None:
    tickets_dir = _current_tree(tmp_path / "tickets")
    (tickets_dir / "board" / "a.md").write_text("# a\n", encoding="utf-8")

    assert tracked_live_state_files(tickets_dir) == []


def test_missing_git_executable_does_not_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Without a git executable no index can track anything; legacy directories still block."""
    tickets_dir = _current_tree(tmp_path / "tickets")

    def missing_git(*_args: object, **_kwargs: object) -> None:
        raise FileNotFoundError("git")

    monkeypatch.setattr(legacy_layout.subprocess, "run", missing_git)

    assert tracked_live_state_files(tickets_dir) == []
    assert "Cannot check tracked Ticket state" in caplog.text


def test_not_a_repository_is_quiet(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    tickets_dir = _current_tree(tmp_path / "tickets")

    assert tracked_live_state_files(tickets_dir) == []
    assert caplog.text == ""


@pytest.mark.parametrize(
    "failure",
    [None, PermissionError("permission denied"), subprocess.TimeoutExpired("git", 10)],
    ids=["exit-status", "os-error", "timeout"],
)
def test_git_failure_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: Exception | None
) -> None:
    """A board Git cannot inspect is refused, never passed as untracked."""
    tickets_dir = _current_tree(tmp_path / "tickets")

    def run_git(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if failure is not None:
            raise failure
        return subprocess.CompletedProcess([], 129, stdout="", stderr="fatal: bad index")

    monkeypatch.setattr(legacy_layout.subprocess, "run", run_git)

    with pytest.raises(TrackedStateCheckError):
        tracked_live_state_files(tickets_dir)
    (problem,) = legacy_layout_problems(tickets_dir)
    assert problem.summary.startswith("cannot check whether Git tracks live Ticket state")
    assert problem.migration is False
    with pytest.raises(LegacyBoardLayoutError, match="cannot be checked") as caught:
        require_current_layout(tickets_dir)
    assert "manual migration" not in str(caught.value)


def test_both_problems_are_reported_together(tmp_path: Path) -> None:
    root = _repo(tmp_path / "repo")
    tickets_dir = _current_tree(root / "tickets")
    (tickets_dir / "board" / "queue").mkdir()
    (tickets_dir / "board" / "queue" / "a.md").write_text("# a\n", encoding="utf-8")
    _git(root, "add", "-A")

    problems = legacy_layout_problems(tickets_dir)

    assert len(problems) == 2
    assert problems[0].summary.startswith("board/queue/ holds 1 file(s)")
    assert problems[1].summary.startswith("Git tracks 1 file(s)")


# Commands refuse ------------------------------------------------------------------


def _legacy_board(tickets_dir: Path) -> Path:
    """A current tree plus one Ticket stranded in the old ``board/queue/``."""
    _current_tree(tickets_dir)
    (tickets_dir / "board" / "queue").mkdir()
    (tickets_dir / "board" / "queue" / "feat.md").write_text("# feat\n", encoding="utf-8")
    return tickets_dir


@pytest.mark.parametrize("argv", [["board"], ["classify"], ["promote-waiting"]])
def test_ticket_board_cli_refuses_before_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
) -> None:
    from booley.ticket_board import cli, operations

    tickets_dir = _legacy_board(tmp_path / "tickets")
    recovered: list[object] = []
    monkeypatch.setattr(cli, "detect_tickets_dir", lambda: tickets_dir)
    monkeypatch.setattr(operations, "reconcile_board", recovered.append)

    assert cli.main(argv) == 2
    err = capsys.readouterr().err
    assert "board/queue/ holds 1 file(s)" in err
    assert MIGRATION_GUIDE in err
    assert recovered == []


@pytest.mark.parametrize("board_command", [None, "show", "move"])
def test_booley_board_refuses_before_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    board_command: str | None,
) -> None:
    from argparse import Namespace

    from booley.harness import booley as tlr
    from booley.ticket_board import operations

    tickets_dir = _legacy_board(tmp_path / "tickets")
    monkeypatch.setenv("TICKETS_DIR", str(tickets_dir))
    recovered: list[object] = []
    monkeypatch.setattr(operations, "reconcile_board", recovered.append)
    args = Namespace(board_command=board_command, slug="feat", target="queue", feedback=None)

    assert tlr._cmd_board(args, tmp_path) == 2
    assert MIGRATION_GUIDE in capsys.readouterr().err
    assert recovered == []


@pytest.mark.parametrize("extra", [[], ["--dry-run"], ["--ticket", "feat", "--check-ready"]])
def test_booley_run_refuses_at_startup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    extra: list[str],
) -> None:
    from booley.harness import booley as tlr

    tickets_dir = _legacy_board(tmp_path / "tickets")
    monkeypatch.setenv("TICKETS_DIR", str(tickets_dir))
    args = tlr._build_parser().parse_args(["run", *extra])
    monkeypatch.setattr(tlr, "_parse_cli", lambda: args)
    monkeypatch.setattr(tlr, "_enforce_runtime_location", lambda _command: None)
    monkeypatch.setattr(tlr, "_host_install_authority_error", lambda _command: None)
    monkeypatch.setattr(tlr, "find_project_root", lambda: tmp_path)
    monkeypatch.setattr(tlr, "_reject_source_project_command", lambda *_args: None)
    monkeypatch.setattr(tlr.runtime_context, "ensure_proxy_env", lambda: False)
    monkeypatch.setattr(tlr, "_handle_early_exits", lambda *_args: None)

    def no_runtime(*_args: object) -> None:
        raise AssertionError("the runner must not start on a legacy board")

    monkeypatch.setattr(tlr, "_setup_runtime", no_runtime)
    monkeypatch.setattr(tlr, "_check_ticket_readiness", no_runtime)
    monkeypatch.setattr(tlr, "_preview_ticket_run", no_runtime)

    assert tlr.main() == 2
    assert "board/queue/ holds 1 file(s)" in capsys.readouterr().err


def test_doctor_fails_once_per_problem(tmp_path: Path) -> None:
    from booley.harness import doctor

    root = _repo(tmp_path / "repo")
    tickets_dir = _legacy_board(root / "tickets")
    _git(root, "add", "-A")
    events: list[tuple[str, str, str]] = []

    doctor._check_ticket_board_layout(
        root,
        lambda message: events.append(("pass", message, "")),
        lambda message, fix: events.append(("fail", message, fix)),
    )

    assert [kind for kind, _, _ in events] == ["fail", "fail"]
    assert "board/queue/ holds 1 file(s)" in events[0][1]
    assert "rm -r --cached --ignore-unmatch -- board state" in events[1][2]
    assert all(MIGRATION_GUIDE in fix for _, _, fix in events)
    assert tickets_dir.is_dir()


def test_doctor_passes_current_layout(tmp_path: Path) -> None:
    from booley.harness import doctor

    _current_tree(tmp_path / "tickets")
    events: list[tuple[str, str]] = []

    doctor._check_ticket_board_layout(
        tmp_path,
        lambda message: events.append(("pass", message)),
        lambda message, _fix: events.append(("fail", message)),
    )

    assert events == [("pass", "Ticket Board uses state records")]

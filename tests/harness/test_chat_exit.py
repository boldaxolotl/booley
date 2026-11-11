"""The exit step of a worktree session: what the prompt lists, keep or discard, no-terminal keeps.

The tests drive the real `exit_step` on a real temporary Git Project, with the terminal test
patched and standard input replaced by a string. Real forks, ptys and agents are in
`test_chat_terminal.py`.
"""

from __future__ import annotations

import importlib
import io
import os
import shutil
import signal
import sys
import time
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from booley.harness.worktree_rm import WorktreeRemovalBlockedError, WorktreeRemovalError
from booley.runtime.worktree_usage import last_used
from tests.goals.conftest import git
from tests.harness.test_worktree_rm import _add, _commit, _pin_namespace, _versioned

pytestmark = [
    pytest.mark.skipif(shutil.which("git") is None, reason="needs git"),
    pytest.mark.skipif(sys.platform == "win32", reason="the exit step follows a POSIX fork"),
]

DAY = 86400
_KEEP_OR_DISCARD = "Keep or discard worktree s1? [K/d] "


def _chat_exit() -> ModuleType:
    """Import lazily so a missing module fails inside the test, not at collection."""
    return importlib.import_module("booley.harness.chat_exit")


def _child_exit(status: int = 0, *, forwarded: bool = False) -> Any:
    """How the agent ended: the raw wait status, and whether Booley relayed a hang-up."""
    return importlib.import_module("booley.runtime.foreground_child").ChildExit(status, forwarded)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A Project whose main checkout holds one commit; Sessions of the test namespace count."""
    monkeypatch.delenv("BOOLEY_DIR", raising=False)
    _pin_namespace(monkeypatch)
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


class _Terminal:
    """Whether the exit step finds a terminal, and what the user types at its questions."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._monkeypatch = monkeypatch
        self.attached = True
        self.stderr_is_tty = True
        real_isatty = os.isatty
        monkeypatch.setattr(
            os, "isatty", lambda fd: self.stderr_is_tty if fd == 2 else real_isatty(fd)
        )
        self.type("")

    def type(self, text: str) -> None:
        """Make *text* the user's input."""
        self._monkeypatch.setattr(sys, "stdin", io.StringIO(text))

    def end(self, repo: Path, name: str = "s1", end: Any = None) -> list[str]:
        """Run the exit step as `booley chat` does once the agent of worktree *name* ended.

        Returns one entry per release of the main-checkout lock.
        """
        chat_exit = _chat_exit()
        self._monkeypatch.setattr(_chat_exit(), "foreground_tty", lambda: self.attached)
        released: list[str] = []
        created = chat_exit.CreatedWorktree()
        try:
            created.record(name)
            chat_exit.exit_step(
                repo, created, lambda: released.append("released"), end or _child_exit()
            )
        finally:
            created.close()
        return released


@pytest.fixture
def terminal(monkeypatch: pytest.MonkeyPatch) -> _Terminal:
    return _Terminal(monkeypatch)


def _stamp(repo: Path, name: str, days: float) -> None:
    stamp = repo / ".booley" / "worktrees" / ".last-used" / name
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.write_bytes(b"")
    then = time.time() - days * DAY
    os.utime(stamp, (then, then))


def _dirty(path: Path) -> None:
    """One uncommitted change that discard would lose."""
    (path / "base.txt").write_bytes(b"edited\n")


def _kept_line(path: Path, name: str = "s1") -> str:
    return f"Kept worktree {name}: {path}"


def test_prompt_lists_uncommitted_and_unmerged_work_per_branch(
    repo: Path, terminal: _Terminal, capsys: pytest.CaptureFixture[str]
) -> None:
    # Bug caught: Paired Checkout changes or `booley-worktree/<name>` commits omitted, counts
    # or branch names wrong, extra lines in the list.
    _versioned(repo)
    path = _add(repo, "s3", paired=True)
    _commit(path, "a.txt")
    _commit(path, "b.txt")
    _dirty(path)
    nested = path / ".booley"
    _commit(nested, "paired.txt")
    (nested / "booley.toml").write_bytes(b"# dirty\n")
    terminal.type("\n")

    terminal.end(repo, "s3")

    assert capsys.readouterr().err == (
        f"Session S3 has ended. Worktree s3: {path}\n"
        "Work not in main yet:\n"
        "  - 1 uncommitted change(s) in the worktree\n"
        "  - 2 commit(s) on booley/s3 not in main\n"
        "  - 1 uncommitted change(s) in its Paired Checkout\n"
        "  - 1 commit(s) on booley-worktree/s3 not in main\n"
        "Discard deletes the uncommitted changes and keeps the branches. "
        "Booley never merges: merge what you need first.\n"
        f"Keep or discard worktree s3? [K/d] {_kept_line(path, 's3')}\n"
    )


def test_prompt_for_clean_worktree_says_nothing_would_be_lost(
    repo: Path, terminal: _Terminal, capsys: pytest.CaptureFixture[str]
) -> None:
    # Bug caught: a clean worktree reported as losing work, or a blocker (a Git lock) listed
    # among the losses.
    path = _add(repo, "s1")
    git(repo, "worktree", "lock", "--reason", "test", str(path))
    terminal.type("\n")

    terminal.end(repo)

    err = capsys.readouterr().err
    assert err == (
        f"Session S1 has ended. Worktree s1: {path}\n"
        "Nothing would be lost: no uncommitted changes, no commits outside main.\n"
        f"{_KEEP_OR_DISCARD}{_kept_line(path)}\n"
    )


def test_ctrl_c_while_checking_losses_keeps_without_traceback(
    repo: Path,
    terminal: _Terminal,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Bug caught: a KeyboardInterrupt from the loss check escaping the exit step and
    # killing Booley with a traceback.
    path = _add(repo, "s1")

    def interrupted(_root: Path, _name: str) -> list[str]:
        raise KeyboardInterrupt

    monkeypatch.setattr(_chat_exit(), "discard_losses", interrupted)

    terminal.end(repo)

    assert capsys.readouterr().err == f"{_kept_line(path)}\n"
    assert path.is_dir()


def test_failing_loss_check_keeps_without_asking(
    repo: Path,
    terminal: _Terminal,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = _add(repo, "s1")

    def broken(_root: Path, _name: str) -> list[str]:
        raise WorktreeRemovalError("git said no")

    monkeypatch.setattr(_chat_exit(), "discard_losses", broken)
    terminal.type("d\n")

    terminal.end(repo)

    assert capsys.readouterr().err == (
        f"Cannot tell what discard would lose: git said no\n{_kept_line(path)}\n"
    )
    assert path.is_dir()


def test_discard_removes_clean_worktree_and_keeps_its_branch(
    repo: Path, terminal: _Terminal, capsys: pytest.CaptureFixture[str]
) -> None:
    # Bug caught: discard not removing the worktree, or deleting `booley/<name>` with it.
    path = _add(repo, "s1")
    terminal.type("d\n")

    terminal.end(repo)

    err = capsys.readouterr().err
    assert "Removed worktree s1.\nBranches kept: booley/s1\n" in err
    assert "Kept worktree" not in err
    assert not path.exists()
    assert "booley/s1" in git(repo, "branch", "--format=%(refname:short)").split()


def test_refused_discard_asks_again_then_forces(
    repo: Path, terminal: _Terminal, capsys: pytest.CaptureFixture[str]
) -> None:
    # Bug caught: forcing without the second question, or not forcing after the user said yes.
    path = _add(repo, "s1")
    _dirty(path)
    terminal.type("discard\ny\n")

    terminal.end(repo)

    err = capsys.readouterr().err
    assert (
        "Worktree s1 cannot be removed safely:\n  - 1 uncommitted change(s) in the worktree\n"
        "Remove it anyway and lose this work? [y/N] "
    ) in err
    assert "Removed worktree s1." in err
    assert not path.exists()


def test_refused_discard_declined_keeps_the_worktree(
    repo: Path, terminal: _Terminal, capsys: pytest.CaptureFixture[str]
) -> None:
    # Bug caught: treating a refusal, or a "no", as permission to force the removal.
    path = _add(repo, "s1")
    _dirty(path)
    terminal.type("d\nn\n")

    terminal.end(repo)

    assert _kept_line(path) in capsys.readouterr().err
    assert (path / "base.txt").read_bytes() == b"edited\n"


@pytest.mark.parametrize("answer", ["", "\n", "maybe\n"])
def test_refused_discard_without_a_yes_keeps_the_worktree(
    repo: Path, terminal: _Terminal, capsys: pytest.CaptureFixture[str], answer: str
) -> None:
    path = _add(repo, "s1")
    _dirty(path)
    terminal.type("d\n" + answer)

    terminal.end(repo)

    assert _kept_line(path) in capsys.readouterr().err
    assert path.is_dir()


def test_ctrl_c_at_the_second_question_keeps_the_worktree(
    repo: Path,
    terminal: _Terminal,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = _add(repo, "s1")
    _dirty(path)
    answers = iter(["d\n"])

    class _Typist(io.StringIO):
        def readline(self, size: int = -1, /) -> str:
            try:
                return next(answers)
            except StopIteration:
                raise KeyboardInterrupt from None

    monkeypatch.setattr(sys, "stdin", _Typist())

    terminal.end(repo)

    assert _kept_line(path) in capsys.readouterr().err
    assert path.is_dir()


def test_keep_is_the_default_answer(
    repo: Path, terminal: _Terminal, capsys: pytest.CaptureFixture[str]
) -> None:
    # Bug caught: Enter (or end of input) discarding the worktree.
    path = _add(repo, "s1")

    for answer in ("\n", "k\n", "keep\n", "", "KEEP\n"):
        terminal.type(answer)
        terminal.end(repo)
        assert capsys.readouterr().err.endswith(f"{_KEEP_OR_DISCARD}{_kept_line(path)}\n"), answer
        assert path.is_dir(), answer


def test_unrecognized_answers_are_asked_again_then_keep(
    repo: Path, terminal: _Terminal, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _add(repo, "s1")
    terminal.type("what\nhuh\nno idea\nd\n")

    terminal.end(repo)

    err = capsys.readouterr().err
    assert err.count(_KEEP_OR_DISCARD) == 3
    assert err.count("Answer k to keep the worktree or d to discard it.") == 3
    assert _kept_line(path) in err
    assert path.is_dir()


def test_a_removal_that_cannot_be_forced_is_reported_and_kept(
    repo: Path,
    terminal: _Terminal,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = _add(repo, "s1")

    def blocked(_root: Path, _name: str, *, force: bool) -> None:
        raise WorktreeRemovalBlockedError("its Booley Directory is a link")

    monkeypatch.setattr(_chat_exit(), "remove_worktree", blocked)
    terminal.type("d\n")

    terminal.end(repo)

    assert capsys.readouterr().err.endswith(
        f"ERROR: its Booley Directory is a link\n{_kept_line(path)}\n"
    )


def test_ctrl_c_during_the_removal_says_how_to_finish_it(
    repo: Path,
    terminal: _Terminal,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _add(repo, "s1")

    def interrupted(_root: Path, _name: str, *, force: bool) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(_chat_exit(), "remove_worktree", interrupted)
    terminal.type("d\n")

    terminal.end(repo)

    err = capsys.readouterr().err
    assert err.endswith("Removal interrupted; run booley worktree rm s1 to finish it.\n")
    assert "Kept worktree" not in err


def test_no_terminal_keeps_without_prompt(
    repo: Path, terminal: _Terminal, capsys: pytest.CaptureFixture[str]
) -> None:
    # Bug caught: prompting (and blocking) when the session ran on a pipe.
    path = _add(repo, "s1")
    terminal.attached = False
    terminal.type("d\n")

    terminal.end(repo)

    assert capsys.readouterr().err == f"{_kept_line(path)}\n"
    assert path.is_dir()
    assert sys.stdin.read() == "d\n", "nothing may read from the user's input"


def test_stderr_off_the_terminal_keeps_without_prompt(
    repo: Path, terminal: _Terminal, capsys: pytest.CaptureFixture[str]
) -> None:
    # Bug caught: prompting into a `2>log` file and waiting silently for an answer.
    path = _add(repo, "s1")
    terminal.stderr_is_tty = False
    terminal.type("d\n")

    terminal.end(repo)

    assert capsys.readouterr().err == f"{_kept_line(path)}\n"
    assert path.is_dir()


def test_forwarded_hangup_keeps_without_prompt(
    repo: Path, terminal: _Terminal, capsys: pytest.CaptureFixture[str]
) -> None:
    # Bug caught: prompting after SIGHUP or SIGTERM, on a terminal that is going away.
    path = _add(repo, "s1")
    terminal.type("d\n")

    terminal.end(repo, end=_child_exit(forwarded=True))

    assert capsys.readouterr().err == f"{_kept_line(path)}\n"
    assert path.is_dir()


@pytest.mark.parametrize("status", [int(signal.SIGKILL), int(signal.SIGSEGV) | 0x80])
def test_an_agent_that_did_not_exit_by_itself_keeps_without_prompt(
    repo: Path, terminal: _Terminal, capsys: pytest.CaptureFixture[str], status: int
) -> None:
    path = _add(repo, "s1")
    terminal.type("d\n")

    terminal.end(repo, end=_child_exit(status))

    assert capsys.readouterr().err == f"{_kept_line(path)}\n"
    assert path.is_dir()


def test_a_session_end_frees_the_lock_and_counts_as_a_use(repo: Path, terminal: _Terminal) -> None:
    # Bug caught: another launch's `gc` removing the worktree while the prompt is open, or
    # an `always` launch keeping main locked while its prompt waits.
    _add(repo, "s1")
    _stamp(repo, "s1", 10)
    terminal.attached = False

    released = terminal.end(repo)

    assert released == ["released"]
    stamp = last_used(repo / ".booley", "s1")
    assert stamp is not None and time.time() - stamp < 60


def test_nothing_happens_when_the_child_made_no_worktree(
    repo: Path, terminal: _Terminal, capsys: pytest.CaptureFixture[str]
) -> None:
    chat_exit = _chat_exit()
    released: list[str] = []
    created = chat_exit.CreatedWorktree()
    try:
        chat_exit.exit_step(repo, created, lambda: released.append("released"), _child_exit())
    finally:
        created.close()

    assert (released, capsys.readouterr().err) == ([], "")


@pytest.mark.parametrize("failure", [ValueError, OSError])
def test_a_closed_terminal_loses_the_messages_without_failing(
    repo: Path, terminal: _Terminal, monkeypatch: pytest.MonkeyPatch, failure: type[Exception]
) -> None:
    # Bug caught: the exit step raising from a print on a terminal that is gone.
    path = _add(repo, "s1")

    class _Gone(io.StringIO):
        def write(self, _text: str, /) -> int:
            raise failure("closed")

    monkeypatch.setattr(sys, "stderr", _Gone())
    terminal.type("\n")

    terminal.end(repo)

    assert path.is_dir()


def test_a_hung_up_stderr_leaves_nothing_buffered_for_the_exit_flush(
    repo: Path, terminal: _Terminal, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Bug caught: text that could not reach a closed terminal stayed in Python's buffer, so
    # the interpreter replaced the agent's exit code with 120 when it flushed at exit.
    _add(repo, "s1")
    read_end, write_end = os.pipe()
    os.close(read_end)  # nobody reads any more: every write fails
    hung_up = os.fdopen(write_end, "w", encoding="utf-8")
    monkeypatch.setattr(sys, "stderr", hung_up)
    terminal.attached = False

    try:
        terminal.end(repo)
        hung_up.flush()  # raises when the failed line was left in the buffer
    finally:
        monkeypatch.undo()
        hung_up.close()


def test_a_closed_stderr_loses_the_messages_without_failing(
    repo: Path,
    terminal: _Terminal,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # `booley chat 2>&-` leaves sys.stderr as None; `print(file=None)` would use stdout.
    _add(repo, "s1")
    monkeypatch.setattr(sys, "stderr", None)
    terminal.attached = False

    terminal.end(repo)

    assert capsys.readouterr().out == ""


def test_created_worktree_reports_only_what_the_child_recorded() -> None:
    created = _chat_exit().CreatedWorktree()
    try:
        assert created.name() is None
        created.record("s7")
        assert created.name() == "s7"
    finally:
        created.close()
        created.close()  # closing twice is harmless
